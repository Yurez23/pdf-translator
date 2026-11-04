"""Source-backed legend cells and independent labels in diagram layouts."""

from dataclasses import replace
from pathlib import Path
import unittest

import pdfplumber

from pdf_translator.export import render
from pdf_translator.geometry import group_lines
from pdf_translator.layout import _diagram_banner, _logical_label, layout_page
from pdf_translator.models import Glyph, Word
from pdf_translator.page_data import PageData
from pdf_translator.pdf_reader import _read_page
from pdf_translator.symbol_legends import recover_symbol_legends
from pdf_translator.symbols import LABELS


ROOT = Path(__file__).resolve().parents[1]
IDENTITIES = ("action_1", "action_2", "action_3", "free_action", "reaction",
              "special_activation", "always_active")
VALUES = ("1 action", "2 actions", "3 actions", "Free action", "Reaction",
          "Special activation", "Always active")
EXPECTED = [["icon", "meaning"], *[[LABELS[s], value] for s, value in zip(IDENTITIES, VALUES)]]


def fixture(x=20, y=20):
    words = [Word("icon", (x, y, x+20, y+11), 11, "Bold"),
             Word("meaning", (x+29, y, x+67, y+11), 11, "Bold")]
    for i, (symbol, value) in enumerate(zip(IDENTITIES, VALUES)):
        top = y+14+i*12
        box = (x+6, top, x+15, top+8.5)
        glyph = Glyph("", "source", box, symbol=symbol, origin="vector")
        words.extend((Word(LABELS[symbol], box, 8.5, glyphs=(glyph,)),
                      Word(value, (x+29, top, x+88, top+8.5), 8.5)))
    return words


def page(words):
    return PageData(1, 612, 792, words, 8.5)


def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.children)


class LegendRecoveryTests(unittest.TestCase):
    def test_source_cells_render_and_keep_glyph_provenance(self):
        p = page(fixture())
        p.tables = recover_symbol_legends(p)
        self.assertEqual(len(p.tables), 1)
        self.assertEqual(p.tables[0].rows, EXPECTED)
        blocks = layout_page(p, set(), lambda _: None)
        table = next(b for b in blocks if b.role == "symbol_legend")
        self.assertEqual(len(table.sources), 17)  # whole table plus 16 observed cell extents
        self.assertEqual(len(table.lines), 8)
        self.assertEqual([w.glyphs[0].symbol for row in table.lines for w in row.words if w.glyphs],
                         list(IDENTITIES))
        md, txt = render(blocks)
        self.assertIn("| icon | meaning |\n| --- | --- |", md)
        self.assertIn("| \\[1 action\\] | 1 action |", md)
        self.assertIn("icon\tmeaning\n[1 action]\t1 action", txt)
        self.assertIn("[always active]\tAlways active", txt)

    def test_cell_contents_are_observed_not_invented_from_the_glyph(self):
        words = fixture()
        words[3] = replace(words[3], text="Different observed value")
        result = recover_symbol_legends(page(words))
        self.assertEqual(result[0].rows[1], ["[1 action]", "Different observed value"])

    def test_incomplete_unknown_unproven_or_misaligned_candidates_remain_flow(self):
        for case in ("missing first glyph", "missing last glyph", "unknown glyph", "unproven labels",
                     "merged header", "misaligned meaning", "extra prose", "card overlap", "existing table"):
            with self.subTest(case=case):
                words = fixture()
                p = page(words)
                if case == "missing first glyph":
                    words.pop(2)
                elif case == "missing last glyph":
                    words.pop(-2)
                elif case == "unknown glyph":
                    words[2] = replace(words[2], text=LABELS["unknown"],
                                       glyphs=(replace(words[2].glyphs[0], symbol="unknown"),))
                elif case == "unproven labels":
                    words[:] = [replace(w, glyphs=()) for w in words]
                elif case == "merged header":
                    words[:2] = [replace(words[0], text="icon meaning", bbox=(20, 20, 87, 31))]
                elif case == "misaligned meaning":
                    old = words[9]
                    words[9] = replace(old, bbox=(old.bbox[0]+10, old.bbox[1], old.bbox[2]+10, old.bbox[3]))
                elif case == "extra prose":
                    words.append(Word("extra", (42, 58, 46, 66.5), 8.5))
                elif case == "card overlap":
                    p.diagram_boxes = [(40, 40, 60, 80)]
                else:
                    from pdf_translator.page_data import TableData
                    p.tables = [TableData((20, 20, 110, 120), [["existing"]])]
                self.assertEqual(recover_symbol_legends(p), [])
                if case not in ("card overlap", "existing table"):
                    output = render(layout_page(p, set(), lambda _: None))[1]
                    self.assertIn("meaning", output)

    def test_failed_candidate_does_not_suppress_an_independent_valid_key(self):
        bad = fixture()
        bad.pop(2)
        good = fixture(x=300)
        result = recover_symbol_legends(page(bad+good))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].rows, EXPECTED)
        self.assertGreater(result[0].bbox[0], 290)

    def test_duplicate_overlapping_header_does_not_claim_words_twice(self):
        words = fixture()
        words.append(replace(words[0]))
        tables = recover_symbol_legends(page(words))
        self.assertLessEqual(len(tables), 1)
        if tables:
            self.assertTrue(all(len({box for box in row}) == 2 for row in tables[0].cell_boxes))

    def test_separate_table_when_enclosed_by_diagram_envelope(self):
        words = fixture(x=60, y=70)
        p = page(words)
        p.diagram_boxes = [(10, 20, 40, 200), (170, 20, 200, 200)]
        p.tables = recover_symbol_legends(p)
        self.assertEqual(len(p.tables), 1)
        blocks = layout_page(p, set(), lambda _: None)
        table = next(b for b in blocks if b.role == "symbol_legend")
        self.assertEqual(table.kind, "table")
        self.assertFalse(any(c is table for b in blocks if b.role == "diagram" for c in walk(b.children)))


class DiagramBannerTests(unittest.TestCase):
    def test_logical_banner_requires_its_panel_and_adjacent_card_geometry(self):
        box = (113, 38, 167, 53)
        glyphs = tuple(Glyph(c, "source", box, actual_text="S" if i == 0 else "")
                       for i, c in enumerate("singer"))
        w = Word("singer", box, 15, "Bold", glyphs=glyphs)
        p = page([w])
        p.diagram_obstacles = [(105, 36, 175, 55)]
        p.diagram_boxes = [(100, 10, 180, 30), (60, 70, 100, 100),
                           (120, 70, 160, 100), (180, 70, 220, 100)]
        row = group_lines(p.words, p.body_size)[0]
        self.assertTrue(_diagram_banner(row, p))
        self.assertEqual(_logical_label(row), "Singer")
        self.assertFalse(_diagram_banner(row, replace(p, diagram_obstacles=[])))
        self.assertFalse(_diagram_banner(row, replace(p, diagram_boxes=p.diagram_boxes[:2])))
        self.assertFalse(_diagram_banner(row, replace(p, diagram_boxes=p.diagram_boxes[:3])))
        self.assertFalse(_diagram_banner(row, replace(p, words=p.words+[Word("extra", (120, 40, 130, 50), 8)])))


class OptionalSourceLegendTests(unittest.TestCase):
    def check_pdf(self, filename, numbers):
        path = ROOT / "example" / filename
        if not path.exists():
            self.skipTest("Optional source PDF is unavailable")
        with pdfplumber.open(path, unicode_norm="NFC") as pdf:
            for number in numbers:
                with self.subTest(page=number):
                    data = _read_page(pdf.pages[number-1], lambda _: None, defer_captions=True)
                    blocks = layout_page(data, set(), lambda _: None, lambda _: False)
                    legends = [b for b in blocks if b.role == "symbol_legend"]
                    self.assertEqual(len(legends), 1)
                    self.assertEqual(legends[0].rows, EXPECTED)
                    self.assertEqual(len(legends[0].sources), 17)
                    self.assertEqual([w.glyphs[0].symbol for row in legends[0].lines for w in row.words
                                      if len(w.glyphs) == 1 and w.glyphs[0].symbol], list(IDENTITIES))
                    md, txt = render(blocks)
                    self.assertIn("| icon | meaning |\n| --- | --- |", md)
                    self.assertIn("[always active]\tAlways active", txt)
                    if filename.startswith("4."):
                        diagram = next(b for b in blocks if b.role == "diagram")
                        banner = next(b for b in diagram.children if b.text == "Singer forms")
                        self.assertEqual((banner.kind, banner.role), ("heading", "graphic_label"))
                        self.assertIn("> **Singer forms**", md)
                        self.assertNotIn("> > **Singer forms**", md)
                        self.assertFalse(any(banner in c.children for c in diagram.children))

    def test_origins(self):
        self.check_pdf("4. Chapter 2 Origins.pdf", [4])

    def test_heroic(self):
        self.check_pdf("6. Chapter 4 Heroic Paths.pdf", [9, 17, 25, 32, 43, 49])

    def test_radiant(self):
        self.check_pdf("7. Chapter 5 Radiant Paths.pdf", [15, 23, 31, 39, 47, 55, 63, 73, 81])

    def test_two_card_banner_is_not_reclassified(self):
        path = ROOT / "example" / "7. Chapter 5 Radiant Paths.pdf"
        if not path.exists():
            self.skipTest("Optional source PDF is unavailable")
        with pdfplumber.open(path, unicode_norm="NFC") as pdf:
            data = _read_page(pdf.pages[66], lambda _: None, defer_captions=True)
        blocks = layout_page(data, set(), lambda _: None, lambda _: False)
        labels = [b for b in walk(blocks) if b.text == "enlightened"]
        self.assertEqual([(b.kind, b.role) for b in labels], [("raw", "graphic_label")])


if __name__ == "__main__":
    unittest.main()
