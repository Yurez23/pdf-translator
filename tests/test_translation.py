"""Offline translation contracts: structure, validation, cache and CLI safety."""

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

from pdf_translator.translation import TranslationError, TranslationOptions, plan_translation, translate_file
from pdf_translator.translation.cli import main
from pdf_translator.translation.document import MARKER, parse_document, render_document
from pdf_translator.translation.models import ModelLimits, ProviderError, TranslationSegment
from pdf_translator.translation.planning import make_request, normalize_glossary, validate_translations, validate_text
from pdf_translator.translation.gemini import GeminiProvider
from pdf_translator.translation.gemini_format import decode_response, make_prompt
from pdf_translator.translation.ports import ProviderIdentity
from pdf_translator.translation.runner import RequestController
from pdf_translator.translation.storage import checkpoint_lock, load_checkpoint, save_checkpoint

try:
    import markdown_it
except ImportError:
    markdown_it = None

OPTIONS = TranslationOptions(model="gemini-test", requests_per_minute=1000, tokens_per_minute=10000000,
                             max_requests=100, max_retries=0)


class FakeProvider(GeminiProvider):
    """Offline Gemini adapter double; independent non-LLM adapters tested below."""
    def __init__(self):
        super().__init__()
        self.generated = []
        self.prompts = []
        self.counted = 0
        self.limits_called = 0
        self.failure = None
        self.response = None

    def prepare(self, options):
        self.options = options
        self._client = object()
        self._limits = None

    def close(self):
        pass

    def model_limits(self):
        self.limits_called += 1
        return ModelLimits(100000, 32000)

    def count_tokens(self, prompt):
        self.counted += 1
        return 100

    def generate(self, prompt, max_output_tokens):
        self.prompts.append(prompt)
        data = json.loads(prompt.split("INPUT DATA:\n", 1)[1])
        self.generated.extend(item["id"] for item in data["segments"])
        if self.failure:
            raise self.failure
        if self.response is not None:
            return self.response
        return json.dumps({"translations": [{"id": item["id"], "text": "Перевод: " + item["text"]}
                                            for item in data["segments"]]}, ensure_ascii=False)


plan_translation = partial(plan_translation, provider=FakeProvider().identity)


def validate_response(raw, batch):
    return validate_translations(decode_response(raw), batch)


class LocalCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "book.txt"
        self.source.write_text("An ordinary paragraph.\n\nAnother paragraph with 20 points.", encoding="utf-8")
        self.out = self.root / "translated"
        # A failing network attempt is always a test failure, never a live call.
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("Network forbidden in tests"))
            guard.start()
            self.addCleanup(guard.stop)

    def run_translation(self, **kwargs):
        kwargs.setdefault("provider", FakeProvider())
        kwargs.setdefault("options", OPTIONS)
        return translate_file(self.source, output_dir=self.out, allow_network=True, **kwargs)

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main([str(self.source), "--model", "gemini-test", "--output-dir", str(self.out), *args])
        return code, output.getvalue(), errors.getvalue()


class TranslationPlanningTests(LocalCase):
    def test_timeout_default_boundaries_and_checkpoint_compatibility(self):
        self.assertEqual(TranslationOptions(model="offline").timeout_seconds, 300)
        first = plan_translation(self.source, OPTIONS).fingerprint
        for value in (1, 60, 120, 300, 299.5):
            options = replace(OPTIONS, timeout_seconds=value)
            self.assertEqual(first, plan_translation(self.source, options).fingerprint)
        for value in (0, 300.01, float("inf"), float("-inf"), float("nan"), True, "300"):
            with self.subTest(value=value), self.assertRaises(TranslationError):
                replace(OPTIONS, timeout_seconds=value)

    def test_options_reject_bad_values(self):
        for values in ({"model": ""}, {"model": "model with whitespace"}, {"model": "a\x00b"},
                       {"model": "a\x7fb"}, {"model": "a\u200bb"},
                       {"max_requests": 0}, {"max_requests": True}, {"timeout_seconds": float("nan")},
                       {"max_retries": 10}, {"source_language": "same language"}, {"target_language": "EN"}):
            with self.subTest(values=values), self.assertRaises(TranslationError):
                replace(OPTIONS, **values)

    def test_plan_is_deterministic_and_has_no_writes(self):
        first = plan_translation(self.source, OPTIONS)
        self.assertEqual(first.fingerprint, plan_translation(self.source, OPTIONS).fingerprint)
        self.assertFalse(self.out.exists())
        self.assertEqual(len(first.document.units), 2)

    def test_glossary_validation(self):
        self.assertEqual(normalize_glossary({" Purpose ": " Цель "}), {"Purpose": "Цель"})
        for value in ([], {"": "x"}, {"a": 1}, {"a\nb": "c"}, {"a": ""}, {" a": "x", "a ": "y"}):
            with self.subTest(value=value), self.assertRaises(TranslationError):
                normalize_glossary(value)

    def test_semantic_settings_change_fingerprint_but_pacing_does_not(self):
        first = plan_translation(self.source, OPTIONS).fingerprint
        for settings in ({"model": "gemini-other"}, {"target_language": "DE"}, {"context_chars": 20},
                         {"max_chunk_chars": 1000}, {"max_output_tokens": 2048}):
            self.assertNotEqual(first, plan_translation(self.source, replace(OPTIONS, **settings)).fingerprint)
        self.assertEqual(first, plan_translation(self.source, replace(OPTIONS, max_requests=200)).fingerprint)
        self.assertNotEqual(first, plan_translation(self.source, OPTIONS, glossary={"ordinary": "обычный"}).fingerprint)

    def test_long_paragraph_splits_and_reassembles_without_lost_spaces(self):
        text = "A sentence with several words. " * 40
        self.source.write_text(text, encoding="utf-8")
        plan = plan_translation(self.source, replace(OPTIONS, max_chunk_chars=256))
        self.assertGreater(len(plan.segments), 1)
        self.assertEqual("".join(item.text + item.separator for item in plan.segments), text.strip())
        self.assertTrue(all(len(item.text) <= 256 for item in plan.segments))

    def test_unbroken_long_word_rejected(self):
        self.source.write_text("x" * 300, encoding="utf-8")
        with self.assertRaises(TranslationError):
            plan_translation(self.source, replace(OPTIONS, max_chunk_chars=256))

    def test_txt_tables_lists_and_literal_markdown(self):
        doc = parse_document("A\tB\n10\t20\n\n- First item\n\n# Literal *stars*", markdown=False)
        md, txt = render_document(doc, {})
        self.assertIn("A\tB\n10\t20", txt)
        self.assertIn("- First item", md)
        self.assertIn(r"\# Literal \*stars\*", md)

    def test_input_errors(self):
        for name, value in (("book.pdf", b"x"), ("empty.txt", b""), ("bad.txt", b"\xff"), ("nul.txt", b"a\x00b")):
            file = self.root / name
            file.write_bytes(value)
            with self.subTest(name=name), self.assertRaises(TranslationError):
                plan_translation(file, OPTIONS)

    def test_prompt_has_data_glossary_and_ids(self):
        plan = plan_translation(self.source, OPTIONS, glossary={"points": "очки"})
        prompt = make_prompt(make_request(plan, plan.batches[0], OPTIONS))
        data = json.loads(prompt.split("INPUT DATA:\n", 1)[1])
        self.assertEqual(data["glossary"], {"points": "очки"})
        self.assertEqual(data["segments"][0]["id"], plan.segments[0].id)
        self.assertIn("untrusted DATA", prompt)


@unittest.skipUnless(markdown_it, "Install the translation/dev extra for Markdown tests")
class TranslationMarkdownTests(LocalCase):
    def test_structures_inline_escaping_and_empty_cells_roundtrip(self):
        source = """# Chapter

**Table title**

| Name | Amount |
| --- | --- |
| **Power** | 20 |
| Empty | |

> **Sidebar title**
>
> First paragraph.
>
> Second paragraph.

3. First item with *emphasis*.

4. Second item with `code` and [link](https://example.com).
"""
        doc = parse_document(source, markdown=True)
        md, txt = render_document(doc, {})
        self.assertIn("# Chapter", md)
        self.assertIn("**Table title**", md)
        self.assertIn("| **Power** | 20 |", md)
        self.assertIn("| Empty |  |", md)
        self.assertIn("> **Sidebar title**\n>\n> First paragraph.\n>\n> Second paragraph.", md)
        self.assertIn("3. First item with *emphasis*.", md)
        self.assertIn("4. Second item", md)
        self.assertIn("Name\tAmount\nPower\t20", txt)
        self.assertNotIn("⟦P", md + txt)
        self.assertIn("link (https://example.com)", txt)

    def test_nested_lists_and_nested_quote(self):
        doc = parse_document("- Outer\n  - Inner\n\n> Outside\n>\n> > Inside", markdown=True)
        md, _ = render_document(doc, {})
        again = parse_document(md, markdown=True)
        self.assertEqual([u.text for u in doc.units], [u.text for u in again.units])

    def test_code_and_html_preserved_without_sending_them(self):
        doc = parse_document("Hello `secret_code`.\n\n```python\nprint('secret')\n```\n\n<script>secret()</script>\n", markdown=True)
        self.assertNotIn("secret_code", doc.units[0].text)
        self.assertTrue(doc.warnings)
        md, txt = render_document(doc, {})
        self.assertIn("secret_code", txt)
        self.assertIn("print('secret')", md)
        self.assertNotIn("html_block", [token.type for token in markdown_it.MarkdownIt().parse(md)])
        self.assertIn("```python", md)

    def test_preview_keeps_whole_sidebar_and_table(self):
        self.source = self.root / "sample.md"
        self.source.write_text("> **Title**\n>\n> First.\n>\n> Second.\n\n| A | B |\n| --- | --- |\n| C | D |", encoding="utf-8")
        plan = plan_translation(self.source, OPTIONS, limit_units=1)
        self.assertEqual(len(plan.document.nodes), 1)
        self.assertEqual(len(plan.document.units), 3)
        table = plan_translation(self.source, OPTIONS, limit_units=4)
        self.assertEqual(len(table.document.units), 7)

    def test_existing_reference_is_only_read_for_offline_plan(self):
        file = Path(__file__).resolve().parents[1] / "output/example.md"
        if not file.exists():
            self.skipTest("Local conversion output not distributed")
        before = file.read_bytes()
        plan = plan_translation(file, OPTIONS)
        self.assertEqual(sum(node.kind == "table" for node in plan.document.nodes), 7)
        self.assertEqual(sum(node.kind == "quote" for node in plan.document.nodes), 5)
        md, txt = render_document(plan.document, {})
        self.assertIn("> **Using Purposes and Obstacles**", md)
        self.assertIn("Loyal Cadre.", txt)
        self.assertEqual(file.read_bytes(), before)


class TranslationValidationTests(unittest.TestCase):
    def setUp(self):
        self.batch = (TranslationSegment("u1.s1", "u1", "Text ⟦P00001:0⟧20 points⟦P00001:1⟧"),)

    def test_valid_reordered_response_is_mapped_by_id(self):
        batch = (TranslationSegment("a", "a", "One"), TranslationSegment("b", "b", "Two"))
        result, _ = validate_response('{"translations":[{"id":"b","text":"Два"},{"id":"a","text":"Один"}]}', batch)
        self.assertEqual(result, {"a": "Один", "b": "Два"})

    def test_missing_duplicate_extra_and_non_json_rejected(self):
        for raw in ("not json", "[]", '{"translations":[]}', '{"translations":[{"id":"x","text":"text"}]}',
                    '{"translations":[{"id":"u1.s1","text":null}]}'):
            with self.subTest(raw=raw), self.assertRaises(TranslationError):
                validate_response(raw, self.batch)
        raw = {"translations": [{"id": "u1.s1", "text": self.batch[0].text}] * 2}
        with self.assertRaises(TranslationError):
            validate_response(json.dumps(raw), self.batch)

    def test_marker_loss_duplication_reordering_and_invention_rejected(self):
        source = self.batch[0].text
        for value in ("Текст 20", source + "⟦P00001:0⟧", source.replace("⟦P00001:0⟧", "⟦P00001:8⟧"),
                      "⟦P00001:1⟧Текст 20⟦P00001:0⟧"):
            with self.subTest(value=value), self.assertRaises(TranslationError):
                validate_text(source, value, "x")

    def test_quality_warnings_and_truncation(self):
        self.assertTrue(validate_text("20 points", "30 очков", "x")[1])
        source = "This is a sufficiently long sentence that stayed unchanged."
        self.assertTrue(validate_text(source, source, "x")[1])
        with self.assertRaises(TranslationError):
            validate_text("A long sentence. " * 20, "Кратко.", "x")

    def test_duplicate_json_keys_are_not_silently_accepted(self):
        for raw in ('{"translations":[],"translations":[]}',
                    '{"translations":[{"id":"a","id":"b","text":"x"}]}',
                    '{"translations":NaN}'):
            with self.subTest(raw=raw), self.assertRaises(TranslationError):
                validate_response(raw, self.batch)


class TranslationRunnerTests(LocalCase):
    def test_api_requires_explicit_network_opt_in(self):
        with self.assertRaises(TranslationError):
            translate_file(self.source, output_dir=self.out, options=OPTIONS, provider=FakeProvider())
        self.assertFalse(self.out.exists())

    def test_complete_writes_both_formats_and_progress(self):
        original = self.source.read_bytes()
        result = self.run_translation()
        self.assertEqual((result.status, result.requests_made), ("complete", 3))
        self.assertIn("Перевод:", Path(result.text_path).read_text(encoding="utf-8"))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(list(self.out.glob("*.lock")))

    def test_budget_pause_resume_and_no_retranslation(self):
        self.source.write_text("A short paragraph.\n\n" + "A sentence repeats. " * 30, encoding="utf-8")
        provider = FakeProvider()
        options = replace(OPTIONS, max_chunk_chars=256, max_requests=3)
        result = self.run_translation(provider=provider, options=options)
        self.assertEqual(result.status, "paused")
        self.assertFalse(list(self.out.glob("*.md")))
        first = set(provider.generated)
        second = FakeProvider()
        result = self.run_translation(provider=second, options=replace(options, max_requests=100), resume=True)
        self.assertEqual(result.status, "complete")
        self.assertTrue(first.isdisjoint(second.generated))

    def test_complete_cache_can_reexport_without_key_or_preparing_provider(self):
        result = self.run_translation()
        with patch.dict("os.environ", {}, clear=True):
            provider = FakeProvider()
            provider.prepare = lambda _: self.fail("Cached export prepared provider")
            complete = translate_file(self.source, output_dir=self.out, options=OPTIONS, provider=provider,
                                      allow_network=True, resume=True, overwrite=True)
        self.assertEqual(complete.requests_made, 0)
        self.assertEqual(complete.text_path, result.text_path)

    def test_incompatible_checkpoint_refused_before_provider(self):
        self.run_translation(options=replace(OPTIONS, max_requests=1))
        for settings in ({"model": "gemini-other"}, {"target_language": "DE"}, {"max_chunk_chars": 256}):
            with self.subTest(settings=settings), self.assertRaises(TranslationError):
                self.run_translation(options=replace(OPTIONS, **settings), resume=True)
        self.source.write_text("Changed input.", encoding="utf-8")
        with self.assertRaises(TranslationError):
            self.run_translation(resume=True)

    def test_new_run_does_not_clear_existing_checkpoint(self):
        self.run_translation(options=replace(OPTIONS, max_requests=1))
        checkpoint = next(self.out.glob("*.json"))
        original = checkpoint.read_bytes()
        with self.assertRaises(TranslationError):
            self.run_translation(overwrite=True)
        self.assertEqual(checkpoint.read_bytes(), original)

    def test_output_collision_stops_before_requests(self):
        self.out.mkdir()
        (self.out / "book.ru.txt").write_text("Existing", encoding="utf-8")
        provider = FakeProvider()
        with self.assertRaises(TranslationError):
            self.run_translation(provider=provider)
        self.assertEqual(provider.limits_called, 0)
        self.assertFalse((self.out / "book.ru.md").exists())

    def test_directory_output_is_refused_before_requests_even_with_overwrite(self):
        (self.out / "book.ru.md").mkdir(parents=True)
        provider = FakeProvider()
        with self.assertRaises(TranslationError):
            self.run_translation(provider=provider, overwrite=True)
        self.assertEqual(provider.limits_called, 0)

    def test_invalid_response_does_not_poison_cache(self):
        provider = FakeProvider()
        provider.response = '{"translations": []}'
        with self.assertRaises(TranslationError):
            self.run_translation(provider=provider)
        state = load_checkpoint(next(self.out.glob("*.json")))
        self.assertEqual(state["translations"], {})
        self.assertFalse(list(self.out.glob("*.txt")))

    def test_network_error_and_interrupt_keep_progress_and_release_lock(self):
        for error in (ProviderError("offline"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                output = self.out / type(error).__name__
                provider = FakeProvider()
                provider.failure = error
                with self.assertRaises(type(error)):
                    translate_file(self.source, output_dir=output, options=OPTIONS, allow_network=True, provider=provider)
                self.assertTrue(list(output.glob("*.json")))
                self.assertFalse(list(output.glob("*.lock")))

    def test_corrupt_and_tampered_checkpoints_refused(self):
        self.run_translation(options=replace(OPTIONS, max_requests=1))
        checkpoint = next(self.out.glob("*.json"))
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        for altered in ({**state, "translations": {"bad": "text"}}, {**state, "calls_total": -1},
                        {**state, "recent_requests": [{"time": float("nan"), "tokens": 0}]}):
            checkpoint.write_text(json.dumps(altered), encoding="utf-8")
            with self.subTest(altered=altered), self.assertRaises(TranslationError):
                self.run_translation(resume=True)

    def test_exclusive_lock(self):
        checkpoint = self.out / "progress.json"
        with checkpoint_lock(checkpoint):
            with self.assertRaises(TranslationError):
                with checkpoint_lock(checkpoint):
                    self.fail("Second lock acquired")
        self.assertFalse(checkpoint.with_suffix(".json.lock").exists())

    def test_oversized_token_batch_splits_before_generation(self):
        self.source.write_text("First paragraph.\n\nSecond paragraph.", encoding="utf-8")
        class TokenProvider(FakeProvider):
            def count_tokens(self, prompt):
                count = len(json.loads(prompt.split("INPUT DATA:\n", 1)[1])["segments"])
                return 1000 if count > 1 else 100
        provider = TokenProvider()
        result = self.run_translation(options=replace(OPTIONS, max_input_tokens=800), provider=provider)
        self.assertEqual(result.status, "complete")
        self.assertEqual(len(provider.prompts), 2)

    def test_source_changed_mid_run_aborts_final_export(self):
        source = self.source
        class MutatingProvider(FakeProvider):
            def generate(self, prompt, max_output_tokens):
                result = super().generate(prompt, max_output_tokens)
                source.write_text("Changed", encoding="utf-8")
                return result
        with self.assertRaises(TranslationError):
            self.run_translation(provider=MutatingProvider())
        self.assertFalse(list(self.out.glob("*.md")))

    def test_output_write_failure_keeps_complete_checkpoint_for_reexport(self):
        with patch("pdf_translator.translation.runner.write_outputs", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.run_translation()
        provider = FakeProvider()
        result = self.run_translation(provider=provider, resume=True)
        self.assertEqual(result.status, "complete")
        self.assertEqual(provider.limits_called, 0)

    def test_output_token_limit_and_single_segment_input_limit_stop_before_generate(self):
        class SmallModel(FakeProvider):
            def model_limits(self):
                return ModelLimits(100000, 1000)
        provider = SmallModel()
        with self.assertRaises(TranslationError):
            self.run_translation(provider=provider)
        self.assertFalse(provider.generated)
        # The request log is compatible, but the input limit is checked anew.
        class TooManyTokens(FakeProvider):
            def count_tokens(self, prompt):
                return 50000
        provider = TooManyTokens()
        with self.assertRaises(TranslationError):
            self.run_translation(provider=provider, resume=True)
        self.assertFalse(provider.generated)

    def test_numbers_only_need_no_key_or_sdk_requests(self):
        self.source.write_text("123 456", encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True):
            result = translate_file(self.source, output_dir=self.out, options=OPTIONS, allow_network=True,
                                    provider=GeminiProvider())
        self.assertEqual(result.requests_made, 0)
        self.assertEqual(Path(result.text_path).read_text(encoding="utf-8").strip(), "123 456")


class TranslationCliTests(LocalCase):
    def test_default_dry_plan_needs_no_key_and_creates_nothing(self):
        with patch.dict("os.environ", {}, clear=True):
            code, output, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("запросов нет", output)
        self.assertFalse(self.out.exists())

    def test_send_missing_key_fails_without_outputs(self):
        with patch.dict("os.environ", {}, clear=True):
            code, _, errors = self.cli("--send")
        self.assertEqual(code, 1)
        self.assertIn("GEMINI_API_KEY", errors)
        self.assertFalse(list(self.out.glob("*.txt")))

    def test_preview_and_glossary_bad_files(self):
        code, _, _ = self.cli("--limit-units", "1")
        self.assertEqual(code, 0)
        code, _, _ = self.cli("--glossary", str(self.root / "missing.json"))
        self.assertEqual(code, 1)

    def test_cancellation_and_wrong_arguments(self):
        with patch("pdf_translator.translation.cli.translate_file", side_effect=KeyboardInterrupt):
            code, _, _ = self.cli("--send")
        self.assertEqual(code, 130)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(caught.exception.code, 2)

    def test_cli_send_completion_and_pause(self):
        with patch("pdf_translator.translation.cli.GeminiProvider", side_effect=FakeProvider):
            code, _, _ = self.cli("--send", "--requests-per-minute", "1000", "--max-requests", "1")
            self.assertEqual(code, 3)
            code, output, _ = self.cli("--send", "--resume", "--requests-per-minute", "1000")
            self.assertEqual(code, 0)
            self.assertIn("Сохранено:", output)


class RequestControllerTests(LocalCase):
    def test_pacing_token_reservations_and_retries(self):
        checkpoint = self.root / "state.json"
        state = {"recent_requests": [], "calls_total": 0}
        now = [1000.0]
        sleeps = []
        def sleep(value):
            sleeps.append(value)
            now[0] += value
        controller = RequestController(replace(OPTIONS, requests_per_minute=1, max_retries=1), state, checkpoint,
                                       lambda _: None, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(controller.call(lambda: 1, tokens=200, label="first"), 1)
        self.assertEqual(controller.call(lambda: 2, tokens=200, label="second"), 2)
        self.assertGreaterEqual(sum(sleeps), 60)
        attempts = []
        def temporary():
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderError("temporary", retryable=True)
            return 3
        self.assertEqual(controller.call(temporary, label="retry"), 3)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(state["calls_total"], 4)

    def test_hard_request_budget_and_tpm_failure(self):
        state = {"recent_requests": [], "calls_total": 0}
        controller = RequestController(replace(OPTIONS, max_requests=1), state, self.root / "state.json", lambda _: None)
        controller.call(lambda: None, label="one")
        with self.assertRaises(TranslationError):
            controller.call(lambda: self.fail("Over budget"), label="two")
        with self.assertRaises(TranslationError):
            controller.call(lambda: self.fail("Over tokens"), tokens=10000001, label="tokens")


if __name__ == "__main__":
    unittest.main()
