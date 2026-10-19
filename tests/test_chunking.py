"""Offline regressions for the hard character cap and semantic chunk boundaries."""

import contextlib
import io
import json
import random
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

from pdf_translator.translation import TranslationError, TranslationOptions, plan_translation, translate_file
from pdf_translator.translation.chunking import split_batch
from pdf_translator.translation.document import MARKER, parse_document, render_document
from pdf_translator.translation.planning import make_request
from pdf_translator.translation.ports import BatchTooLarge, ProviderIdentity

try:
    import markdown_it
except ImportError:
    markdown_it = None

IDENTITY = ProviderIdentity("offline", "v1")


def sentence(length, label="The"):
    return label + " " + "x" * (length - len(label) - 2) + "."


class ChunkingCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("Network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    def plan(self, text, *, limit=256, markdown=False):
        self.source = self.root / ("book.md" if markdown else "book.txt")
        self.source.write_text(text, encoding="utf-8")
        self.options = TranslationOptions(model="offline", max_chunk_chars=limit,
                                          requests_per_minute=1000, tokens_per_minute=10000000)
        plan = plan_translation(self.source, self.options, provider=IDENTITY)
        self.check_invariants(plan, limit)
        return plan

    def check_invariants(self, plan, limit):
        self.assertEqual(tuple(s for b in plan.batches for s in b), plan.segments)
        self.assertEqual(len({s.id for s in plan.segments}), len(plan.segments))
        restored = defaultdict(str)
        for segment in plan.segments:
            restored[segment.unit_id] += segment.text + segment.separator
        for unit in plan.document.units:
            if any(c.isalpha() for c in MARKER.sub("", unit.text)):
                self.assertEqual(restored[unit.id], unit.text)
        for batch in plan.batches:
            self.assertTrue(batch)
            self.assertLessEqual(len("\n\n".join(s.text for s in batch)), limit)
            request = make_request(plan, batch, self.options)
            self.assertEqual([i.id for i in request.items], [s.id for s in batch])

    def texts(self, plan):
        return [[s.text for s in batch] for batch in plan.batches]


class TextChunkingTests(ChunkingCase):
    def test_default_and_maximum_are_exactly_16000(self):
        self.assertEqual(TranslationOptions(model="offline").max_chunk_chars, 16000)
        self.assertEqual(TranslationOptions(model="offline").max_output_tokens, 32768)
        self.assertEqual(TranslationOptions(model="offline").tokens_per_minute, 50000)
        self.assertLessEqual(12000 + 32768, TranslationOptions(model="offline").tokens_per_minute)
        for invalid in (16001, 20000, 255, True):
            with self.subTest(value=invalid), self.assertRaises(TranslationError):
                TranslationOptions(model="offline", max_chunk_chars=invalid)
        text = sentence(8000) + " " + sentence(7999)
        plan = self.plan(text, limit=16000)
        self.assertEqual(len(plan.batches), 1)
        self.assertEqual(len(plan.batches[0][0].text), 16000)
        plan = self.plan(text + " " + sentence(100), limit=16000)
        self.assertEqual(len(plan.batches), 2)

    def test_spaces_and_separators_count_toward_cap(self):
        plan = self.plan(sentence(128) + "\n\n" + sentence(127))
        self.assertEqual(len(plan.batches), 2)  # 128 + 2 + 127 > 256
        self.assertEqual(len(self.plan(sentence(127) + "\n\n" + sentence(127)).batches), 1)

    def test_sentence_fallback_is_lossless_and_never_uses_a_word_break(self):
        text = " ".join(sentence(73, f"Part{i}") for i in range(15))
        plan = self.plan(text)
        self.assertGreater(len(plan.batches), 1)
        self.assertTrue(all(s.text.endswith(".") for s in plan.segments))
        self.assertTrue(all(s.separator == " " for s in plan.segments[:-1]))

    def test_oversized_sentence_even_with_spaces_is_an_explicit_error(self):
        with self.assertRaisesRegex(TranslationError, "одно предложение.*256"):
            self.plan("A long sentence " + "has many words " * 25 + ".")
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_abbreviations_initials_decimals_and_quoted_attribution(self):
        one = 'Dr. Smith met Mr. Jones in the U.S. Army at 3.14 p.m. "Wait!" he said. '
        plan = self.plan(one * 12)
        self.assertTrue(all(s.text.endswith('he said.') for s in plan.segments))

    def test_txt_does_not_guess_heading_hierarchy(self):
        paragraphs = [sentence(90), "# This is literal text", sentence(90), sentence(90)]
        plan = self.plan("\n\n".join(paragraphs))
        self.assertEqual([len(b) for b in plan.batches], [3, 1])
        self.assertTrue(any("TXT" in str(w) for w in plan.document.warnings))

    def test_same_plan_is_deterministic(self):
        first = self.plan("\n\n".join(sentence(90, f"P{i}") for i in range(17)))
        again = plan_translation(self.source, self.options, provider=IDENTITY)
        self.assertEqual(first, again)
        self.assertEqual(first.manifest["contract_version"], 3)
        self.assertIn("chunking_policy", first.manifest)

    def test_cli_uses_16000_by_default_and_rejects_larger_caps(self):
        from pdf_translator.translation.cli import main
        self.plan("\n\n".join(sentence(75) for _ in range(100)), limit=16000)
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.source)]), 0)
        self.assertIn("1 пакетов", output.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main([str(self.source), "--max-chunk-chars", "16001"]), 1)
        self.assertEqual(list(self.root.iterdir()), [self.source])


@unittest.skipUnless(markdown_it, "Install translation/dev extra for Markdown")
class MarkdownChunkingTests(ChunkingCase):
    def test_backoff_to_last_completed_subsection_not_a_partial_one(self):
        text = "# Chapter\n\n" + "\n\n".join(f"## {name}\n\n{sentence(100, name)}"
                                                      for name in ("One", "Two", "Three"))
        plan = self.plan(text, markdown=True)
        self.assertEqual([b[0] for b in self.texts(plan)], ["Chapter", "Three"])
        self.assertTrue(plan.batches[0][-1].text.startswith("Two "))

    def test_oversized_parent_uses_nested_subsections(self):
        text = "# Book\n\n## Large\n\n" + sentence(45) + "\n\n" + "\n\n".join(
            f"### {name}\n\n{sentence(110, name)}" for name in ("First", "Second", "Third"))
        plan = self.plan(text, markdown=True)
        self.assertEqual([b[0] for b in self.texts(plan)], ["Book", "Second"])
        self.assertTrue(plan.batches[0][-1].text.startswith("First "))

    def test_short_section_not_filled_with_start_of_oversized_next_section(self):
        text = "## Small\n\n" + sentence(45) + "\n\n## Large\n\n" + "\n\n".join([sentence(75)] * 8)
        plan = self.plan(text, markdown=True)
        self.assertEqual(len(plan.batches[0]), 2)
        self.assertEqual(plan.batches[1][0].text, "Large")

    def test_oversized_section_falls_back_to_whole_paragraphs(self):
        plan = self.plan("## Large\n\n" + "\n\n".join([sentence(90)] * 5), markdown=True)
        self.assertEqual([len(b) for b in plan.batches], [3, 2, 1])
        self.assertEqual(len(plan.segments), 6)

    def test_heading_stays_with_start_of_long_first_paragraph(self):
        text = "## Heading title\n\n" + sentence(124) + " " + sentence(125)
        plan = self.plan(text, markdown=True)
        self.assertEqual(len(plan.batches), 2)
        self.assertEqual([len(b) for b in plan.batches], [2, 1])
        self.assertEqual(plan.batches[0][-1].text, sentence(124))

    def test_consecutive_parent_child_headings_are_never_orphaned(self):
        text = "## Previous\n\n" + sentence(210) + "\n\n# Parent\n\n## Child\n\n" + sentence(100)
        plan = self.plan(text, markdown=True)
        self.assertEqual([b[0] for b in self.texts(plan)], ["Previous", "Parent"])
        self.assertEqual(len(plan.batches[1]), 3)

    def test_numeric_only_headings_still_define_section_boundaries(self):
        text = "## One\n\n" + sentence(90) + "\n\n## 2\n\n" + sentence(90) + "\n\n" + sentence(90)
        plan = self.plan(text, markdown=True)
        self.assertEqual(len(plan.batches[0]), 2)
        self.assertEqual(plan.batches[1][0].text, sentence(90))

    def test_empty_numeric_containers_and_initial_numeric_heading(self):
        plan = self.plan("## 1\n\n" + sentence(100) + "\n\n> ## 123\n\n## 2", markdown=True)
        self.assertEqual(len(plan.segments), 1)
        self.assertEqual(len(plan.batches), 1)

    def test_heading_attachment_cannot_exceed_hard_cap(self):
        with self.assertRaisesRegex(TranslationError, "Заголовок и начало"):
            self.plan("## " + "H" * 40 + "\n\n" + sentence(230), markdown=True)

    def test_no_independent_24_item_limit(self):
        text = "## Items\n\n" + "\n\n".join(f"Paragraph {i}." for i in range(60))
        plan = self.plan(text, limit=16000, markdown=True)
        self.assertEqual(len(plan.batches), 1)
        self.assertEqual(len(plan.batches[0]), 61)

    def test_seeded_mixed_documents_preserve_all_text_and_headings(self):
        rng = random.Random(16000)
        for sample in range(100):
            blocks = ["# Book"]
            for section in range(rng.randint(2, 8)):
                blocks += ["#" * rng.randint(2, 5) + f" Section {section}", sentence(rng.randint(30, 100))]
                for _ in range(rng.randint(1, 4)):
                    kind = rng.randrange(4)
                    if kind == 0:
                        blocks.append("> ### Aside\n>\n> " + sentence(rng.randint(30, 100)))
                    elif kind == 1:
                        blocks.append("\n".join("- " + sentence(rng.randint(20, 70)) for _ in range(4)))
                    elif kind == 2:
                        blocks.append("| A | B |\n| --- | --- |\n| Alpha | Beta |\n| Gamma | Delta |")
                    else:
                        blocks.append(" ".join(sentence(rng.randint(30, 100)) for _ in range(4)))
            with self.subTest(sample=sample):
                plan = self.plan("\n\n".join(blocks), limit=rng.choice((256, 400, 700)), markdown=True)
                kinds = {u.id: u.kind for u in plan.document.units}
                for batch in plan.batches:
                    self.assertNotEqual(kinds[batch[-1].unit_id], "heading")

    def test_fitting_containers_move_whole_to_next_batch(self):
        cases = (
            ("| A | B |\n| --- | --- |\n| " + sentence(55) + " | " + sentence(55) + " |", "table"),
            ("- " + sentence(60) + "\n- " + sentence(60), "list"),
            ("> **Title**\n>\n> " + sentence(55) + "\n>\n> " + sentence(55), "quote"),
        )
        for container, kind in cases:
            with self.subTest(kind=kind):
                plan = self.plan(sentence(150) + "\n\n" + container, markdown=True)
                self.assertEqual(len(plan.batches[0]), 1)
                self.assertEqual(len(plan.batches), 2)
                self.assertFalse(plan.document.warnings)

    def test_large_table_splits_between_rows_and_roundtrips(self):
        text = "| Name | Meaning |\n| --- | --- |\n" + "\n".join(
            f"| Row{i} | {sentence(80)} |" for i in range(12))
        plan = self.plan(text, markdown=True)
        locations = {s.unit_id: i for i, batch in enumerate(plan.batches) for s in batch}
        for row in plan.document.nodes[0].rows:
            self.assertEqual(len({locations[u] for u in row}), 1)
        self.assertTrue(any("table" in str(w) for w in plan.document.warnings))
        md, txt = render_document(plan.document, {})
        self.assertEqual(len(parse_rows(txt)), 13)
        self.assertIn("| Row11 |", md)

    def test_large_table_row_can_split_at_cells_if_necessary(self):
        text = "| A | B | C |\n| --- | --- | --- |\n| " + " | ".join([sentence(110)] * 3) + " |"
        plan = self.plan(text, markdown=True)
        self.assertGreater(len(plan.batches), 1)
        self.assertTrue(any("table" in str(w) for w in plan.document.warnings))

    def test_fitting_table_can_split_if_its_heading_must_fit_too(self):
        text = "## Table heading\n\n| A | B |\n| --- | --- |\n| " + sentence(121) + " | " + sentence(120) + " |"
        plan = self.plan(text, markdown=True)
        self.assertEqual(plan.batches[0][0].text, "Table heading")
        self.assertGreater(len(plan.batches[0]), 1)
        self.assertTrue(any("table" in str(w) for w in plan.document.warnings))

    def test_large_list_keeps_fitting_items_with_nested_paragraphs_together(self):
        text = "\n\n".join("- " + sentence(80, f"Item{i}") + "\n\n  " + sentence(80)
                              for i in range(5))
        plan = self.plan(text, markdown=True)
        self.assertEqual([len(b) for b in plan.batches], [2] * 5)
        self.assertTrue(any("list" in str(w) for w in plan.document.warnings))

    def test_large_sidebar_keeps_its_paragraphs_and_heading(self):
        text = "> ### Aside\n>\n" + "\n>\n".join("> " + sentence(100) for _ in range(5))
        plan = self.plan(text, markdown=True)
        self.assertEqual(plan.batches[0][0].text, "Aside")
        self.assertEqual([len(b) for b in plan.batches], [3, 2, 1])
        self.assertTrue(any("quote" in str(w) for w in plan.document.warnings))

    def test_sidebar_heading_does_not_become_global_section_boundary(self):
        text = sentence(60) + "\n\n> ### Aside\n>\n> " + sentence(70) + "\n\n" + "\n\n".join([sentence(60)] * 3)
        plan = self.plan(text, markdown=True)
        self.assertEqual(len(plan.batches[0]), 4)

    def test_protected_markers_survive_sentence_cuts_and_rendering(self):
        text = "## Test\n\n**" + " ".join([sentence(100)] * 6) + "** and [link](https://example.com)."
        plan = self.plan(text, markdown=True)
        originals = {u.id: u.text for u in plan.document.units}
        assembled = defaultdict(str)
        for segment in plan.segments:
            assembled[segment.unit_id] += segment.text + segment.separator
        self.assertEqual(dict(assembled), originals)
        self.assertEqual(render_document(plan.document, assembled), render_document(plan.document, {}))

    def test_token_backoff_uses_sections_and_does_not_orphan_heading(self):
        text = "## First\n\n" + sentence(70) + "\n\n## Second\n\n" + sentence(70) + "\n\n## Third\n\n" + sentence(70)
        plan = self.plan(text, limit=16000, markdown=True)
        left, right = split_batch(plan.document, plan.batches[0])
        self.assertEqual([s.text for s in left], ["First", sentence(70)])
        self.assertEqual(right[0].text, "Second")
        plan = self.plan("## Title\n\n" + sentence(100), markdown=True)
        with self.assertRaisesRegex(TranslationError, "безопасно"):
            split_batch(plan.document, plan.batches[0])

    def test_old_planning_contract_refused_without_mutating_checkpoint(self):
        self.plan("## Title\n\n" + sentence(100), markdown=True)
        class Echo:
            identity = IDENTITY

            def prepare(self, options):
                return self

            def translate(self, request, *, executor):
                return {i.id: i.text for i in request.items}
        output = self.root / "result"
        result = translate_file(self.source, output_dir=output, options=self.options,
                                provider=Echo(), allow_network=True)
        checkpoint = Path(result.checkpoint)
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        state["manifest"]["contract_version"] = 2
        checkpoint.write_text(json.dumps(state), encoding="utf-8")
        before = checkpoint.read_bytes()
        with self.assertRaisesRegex(TranslationError, "Журнал относится"):
            translate_file(self.source, output_dir=output, options=self.options,
                           provider=Echo(), allow_network=True, resume=True, overwrite=True)
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_token_backoff_runs_end_to_end_without_heading_only_requests(self):
        self.plan("\n\n".join(f"## Section {i}\n\n{sentence(70)}" for i in range(3)),
                  limit=16000, markdown=True)
        class LimitedEcho:
            identity = IDENTITY

            def __init__(self):
                self.accepted = []

            def prepare(self, options):
                return self

            def translate(self, request, *, executor):
                if len(request.items) > 2:
                    raise BatchTooLarge("Offline input limit")
                self.accepted.append(request)
                return executor.call(lambda: {i.id: i.text for i in request.items}, label="offline")
        provider = LimitedEcho()
        result = translate_file(self.source, output_dir=self.root / "result", options=self.options,
                                provider=provider, allow_network=True)
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(provider.accepted), 3)
        self.assertTrue(all([i.kind for i in r.items] == ["heading", "paragraph"] for r in provider.accepted))
        self.assertEqual(Path(result.markdown_path).read_text(encoding="utf-8"),
                         render_document(parse_document(self.source.read_text(encoding="utf-8"), markdown=True), {})[0])


def parse_rows(text):
    return [line for line in text.splitlines() if "\t" in line]
