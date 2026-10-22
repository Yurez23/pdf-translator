"""Span evidence, normalization, false ruling and non-destructive fallback."""

from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

import pdfplumber
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.export import render
from pdf_translator.layout import _table_blocks
from pdf_translator.models import Word
from pdf_translator.page_data import PageData, TableData
from pdf_translator.table_structure import (CellPaint, normalized_sections,
                                            recover_table_structure, structure_from_boxes)
from tests.test_table_recovery import cell_words

ROOT = Path(__file__).resolve().parents[1]


def fixture(entries):
    """Explicit physical boxes, independent of the production detector."""
    boxes, words = [], []
    for box, text in entries:
        boxes.append(box)
        words.extend(cell_words(text, box))
    return boxes, words


def rendered(structure):
    table = TableData((0, 0, 300, 200), [], structure=structure)
    data = PageData(7, 612, 792, [], 9, tables=[table])
    warnings = []
    blocks = _table_blocks(data, set(), warnings.append, lambda _: False)
    return blocks, render(blocks), warnings


class SpanTests(unittest.TestCase):
    def test_vertical_labels_and_shared_body_cell_have_exact_scope(self):
        boxes, words = fixture([
            ((10, 10, 70, 30), "Group"), ((70, 10, 150, 30), "First"), ((150, 10, 240, 30), "Second"),
            ((10, 30, 70, 70), "A"), ((70, 30, 150, 50), "-2"), ((150, 30, 240, 50), "3–5 ft."),
            ((70, 50, 150, 70), "0"), ((150, 50, 240, 70), ""),
            ((10, 70, 70, 100), "B"), ((70, 70, 240, 100), "Either 1 or 2\n(footnote*)"),
        ])
        structure = structure_from_boxes(boxes, words)
        self.assertIsNotNone(structure)
        self.assertEqual(len(structure.cells), 10)
        self.assertEqual([(c.rowspan, c.colspan, c.text) for c in structure.cells if c.rowspan > 1], [(2, 1, "A")])
        sections = normalized_sections(structure)
        self.assertEqual(sections[0].rows, (("Group", "First", "Second"), ("A", "-2", "3–5 ft."), ("A", "0", "")))
        self.assertEqual(sections[1].rows, (("Group", "First / Second"), ("B", "Either 1 or 2\n(footnote*)")))
        blocks, (md, txt), warnings = rendered(structure)
        self.assertEqual(warnings, [])
        self.assertEqual(len(blocks), 2)
        self.assertEqual(md.count("Either 1 or 2"), 1)
        self.assertEqual(txt.count("Either 1 or 2"), 1)
        self.assertIn("First / Second", md)
        self.assertIn("B\tEither 1 or 2 (footnote*)", txt)

    def test_empty_physical_cell_is_not_a_missing_geometry_slot(self):
        boxes, words = fixture([((10, 10, 70, 30), "Name"), ((70, 10, 140, 30), "Value"),
                                ((10, 30, 70, 50), "A"), ((70, 30, 140, 50), "")])
        self.assertEqual(normalized_sections(structure_from_boxes(boxes, words))[0].rows[-1], ("A", ""))
        self.assertIsNone(structure_from_boxes(boxes[:-1], words))

    def test_multilevel_headers_with_vertical_spans_and_wrapped_values(self):
        entries = [((10, 10, 70, 50), "Name"), ((70, 10, 230, 30), "Scores"),
                   ((70, 30, 150, 50), "Low"), ((150, 30, 230, 50), "High"),
                   ((10, 50, 70, 80), "Alpha"), ((70, 50, 150, 80), "-3"),
                   ((150, 50, 230, 80), "4–6 units\n(note*)")]
        boxes, words = fixture(entries)
        paints = [CellPaint(b, (0, 0, .3)) for b, _ in entries[:4]]
        structure = structure_from_boxes(boxes, words, paints)
        self.assertEqual(structure.header_rows, 2)
        self.assertEqual(normalized_sections(structure)[0].rows,
                         (("Name", "Scores / Low", "Scores / High"), ("Alpha", "-3", "4–6 units\n(note*)")))
        self.assertIsNone(structure_from_boxes(boxes, words))
        self.assertIsNone(structure_from_boxes(boxes, words, paints[:-1]))

    def test_painted_section_labels_keep_scope_without_duplicating_body_text(self):
        entries = [((10, 10, 90, 30), "Name"), ((90, 10, 250, 30), "Description"),
                   ((10, 30, 250, 50), "First group"),
                   ((10, 50, 90, 70), "A"), ((90, 50, 250, 70), "First text."),
                   ((10, 70, 250, 90), "Second group"),
                   ((10, 90, 90, 110), "B"), ((90, 90, 250, 110), "Second text.")]
        boxes, words = fixture(entries)
        paints = [CellPaint(b, (.1, .2, .3)) for b, _ in entries if b[1] in (10, 30, 70)]
        structure = structure_from_boxes(boxes, words, paints)
        self.assertEqual(structure.group_rows, (1, 3))
        blocks, (md, txt), warnings = rendered(structure)
        self.assertEqual([b.kind for b in blocks], ["heading", "table", "heading", "table"])
        for text in (md, txt):
            self.assertLess(text.index("First group"), text.index("First text."))
            self.assertLess(text.index("First text."), text.index("Second group"))
            self.assertEqual(text.count("First text."), 1)
        self.assertEqual(warnings, [])
        self.assertIsNone(structure_from_boxes(boxes, words))

    def test_grouped_table_does_not_change_other_table_title_styles(self):
        from pdf_translator.headings import assign_heading_levels
        from pdf_translator.models import Block, Line, SourceRegion
        entries = [((10, 30, 90, 50), "Name"), ((90, 30, 250, 50), "Value"),
                   ((10, 50, 250, 70), "Group"),
                   ((10, 70, 90, 90), "A"), ((90, 70, 250, 90), "Description")]
        boxes, words = fixture(entries)
        paints = [CellPaint(b, (.1, .2, .3)) for b, _ in entries[:3]]
        structure = structure_from_boxes(boxes, words, paints)
        blocks, _, _ = rendered(structure)
        word = Word("Caption", (10, 15, 100, 25), 10, "Bold")
        title = Block("heading", "Caption", (SourceRegion(7, word.bbox),), lines=[Line([word], word.bbox, 10)])
        assign_heading_levels([title,*blocks], {7}, lambda _: None)
        self.assertEqual(title.role, "table_title")
        self.assertEqual(blocks[0].role, "table_group")

    def test_missing_overlapping_crossing_and_rotated_evidence_declines(self):
        boxes, words = fixture([((10, 10, 90, 30), "A"), ((90, 10, 180, 30), "B"),
                                ((10, 30, 90, 50), "C"), ((90, 30, 180, 50), "D")])
        for altered in (boxes[:-1], boxes+[(20, 15, 150, 40)]):
            self.assertIsNone(structure_from_boxes(altered, words))
        self.assertIsNone(structure_from_boxes(boxes, words+[Word("crossing", (80, 33, 110, 41), 8)]))
        rotated = deepcopy(words)
        rotated[0].angle = 180
        self.assertIsNone(structure_from_boxes(boxes, rotated))
        self.assertEqual(structure_from_boxes(boxes+boxes, words), structure_from_boxes(boxes, words))

    def test_no_geometry_cannot_promote_lists_columns_sidebars_or_cards(self):
        for text in ("1. First item", "Left prose. Right prose.", "Sidebar text", "Card 1\nCard 2"):
            table = TableData((10, 10, 250, 100), [[text, None], ["More", "text"]])
            self.assertIs(recover_table_structure(table, [], []), table)

    def test_unresolved_grid_retains_text_and_page_warning_in_both_exports(self):
        table = TableData((10, 10, 250, 100), [["Label", None], ["-2 [reaction]", "? unknown"]], True)
        data = PageData(13, 612, 792, [], 9, tables=[table])
        warnings = []
        blocks = _table_blocks(data, set(), warnings.append)
        self.assertEqual([(w.code, w.pages) for w in warnings], [("complex_table", (13,))])
        self.assertEqual(blocks[0].role, "complex_table")
        md, txt = render(blocks)
        self.assertIn("? unknown", txt)
        self.assertIn(r"\[reaction\]", md)

    def test_conflicting_detector_does_not_duplicate_or_drop_source_words_in_raw(self):
        table = TableData((10, 10, 250, 100), [["Repeated", None], ["Repeated", "Other"]], True,
                          [[(10, 10, 100, 100), (20, 10, 150, 100)]])
        words = cell_words("Repeated Other Unassigned", (10, 20, 250, 50))
        data = PageData(4, 612, 792, words, 8, tables=[table])
        warnings = []
        blocks = _table_blocks(data, set(), warnings.append)
        self.assertEqual(blocks[0].text, "Repeated Other Unassigned")
        self.assertEqual(warnings[0].pages, (4,))

    def test_long_shared_body_prose_is_not_repeated_as_a_label(self):
        boxes, words = fixture([((10, 10, 180, 30), "Text"), ((180, 10, 240, 30), "Value"),
                                ((10, 30, 180, 70), "A long shared body prose sentence"),
                                ((180, 30, 240, 50), "1"), ((180, 50, 240, 70), "2")])
        self.assertIsNone(structure_from_boxes(boxes, words))


class FalseRulingTests(unittest.TestCase):
    def make_table(self):
        entries = [((x, y, x+80, y+20), text)
                   for y, row in [(30, ["A", "B", "C"]), (50, ["a", "-2", "3–5"]), (70, ["b", "4", "*"])]
                   for x, text in zip((10, 90, 170), row)]
        boxes, words = fixture(entries)
        words += cell_words("Caption", (65, 10, 155, 30))
        # A spurious narrow title rectangle overlaps and clips the real header.
        damaged = [[(65, 10, 155, 30)], [boxes[0], (65, 30, 155, 34), boxes[2]], boxes[3:6], boxes[6:]]
        table = TableData((10, 10, 250, 90), [["Caption", None], ["A", "B", "C"], ["a", "-2", "3–5"], ["b", "4", "*"]],
                          True, damaged)
        paints = [CellPaint(b, (.1, .2, .3)) for b in boxes[:6]]
        return table, words, paints

    def test_repeated_paint_repairs_false_geometry_and_releases_caption(self):
        table, words, paints = self.make_table()
        snapshot = deepcopy((table, words, paints))
        result = recover_table_structure(table, words, paints)
        self.assertEqual(result.bbox, (10, 30, 250, 90))
        self.assertEqual(normalized_sections(result.structure)[0].rows,
                         (("A", "B", "C"), ("a", "-2", "3–5"), ("b", "4", "*")))
        self.assertEqual((table, words, paints), snapshot)
        self.assertIs(recover_table_structure(result, words, paints), result)

    def test_partial_body_or_single_paint_band_cannot_repair_conflict(self):
        table, words, paints = self.make_table()
        self.assertIs(recover_table_structure(table, words, paints[:3]), table)
        table.cell_boxes[-1].pop()
        self.assertIs(recover_table_structure(table, words, paints), table)

    def test_existing_unrepresented_text_blocks_repair(self):
        table, words, paints = self.make_table()
        table.rows[0][0] += " absent"
        self.assertIs(recover_table_structure(table, words, paints), table)

    def test_neighboring_prose_and_adjacent_table_do_not_enter_recovered_grid(self):
        table, words, paints = self.make_table()
        words += cell_words("Neighboring prose", (10, 100, 250, 125))
        words += cell_words("Independent table", (270, 30, 400, 55))
        result = recover_table_structure(table, words, paints)
        self.assertNotIn("Neighboring", str(normalized_sections(result.structure)))
        self.assertNotIn("Independent", str(normalized_sections(result.structure)))


class PdfAdapterTests(unittest.TestCase):
    def convert(self, draw):
        stream = BytesIO()
        canvas = Canvas(stream, pagesize=(612, 792))
        canvas.setFont("Helvetica", 9)
        draw(canvas)
        canvas.showPage(); canvas.save()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"layout.pdf"
            source.write_bytes(stream.getvalue())
            return convert_pdf(source)

    def test_lists_columns_and_filled_sidebar_are_not_promoted_to_tables(self):
        def draw(canvas):
            for x in (40, 320):
                for i in range(6):
                    canvas.drawString(x, 710-i*15, f"{i+1}. Ordinary text in a column.")
            canvas.setFillColorRGB(.9, .9, .9)
            canvas.rect(40, 450, 230, 100, fill=1, stroke=0)
            canvas.setFillColorRGB(0, 0, 0)
            for i in range(4):
                canvas.drawString(48, 530-i*15, "A paragraph inside a painted sidebar.")
        result = self.convert(draw)
        self.assertFalse(any(b.kind == "table" for b in result.blocks))
        self.assertNotIn("complex_table", [w.code for w in result.warnings])
        self.assertIn("painted sidebar", result.text)

    def test_adjacent_independent_spanned_tables_stay_separate(self):
        def draw(canvas):
            for x, label in ((40, "First"), (330, "Second")):
                for y in (700, 680, 640):
                    canvas.line(x, y, x+230, y)
                canvas.line(x+80, 660, x+230, 660)
                for edge in (x, x+80, x+230):
                    canvas.line(edge, 640, edge, 700)
                for dx, y, text in ((5, 687, "Group"), (85, 687, "Value"),
                                    (5, 657, label), (85, 667, "-1"), (85, 647, "2–4 ft.")):
                    canvas.drawString(x+dx, y, text)
            canvas.drawString(40, 620, "Neighboring prose stays outside both tables.")
        result = self.convert(draw)
        tables = [b for b in result.blocks if b.kind == "table"]
        self.assertEqual(len(tables), 2)
        self.assertEqual([t.rows[1][0] for t in tables], ["First", "Second"])
        self.assertEqual([t.rows[2][0] for t in tables], ["First", "Second"])
        self.assertNotIn("Neighboring", str([t.rows for t in tables]))
        self.assertEqual(result.text.count("Neighboring prose"), 1)

    def test_ruled_spans_symbols_and_surrounding_prose_through_both_exports(self):
        from tests.test_symbols import draw_icon
        stream = BytesIO()
        canvas = Canvas(stream, pagesize=(612, 792))
        canvas.setFont("Helvetica", 9)
        canvas.drawString(40, 720, "Before the table.")
        # Two header cells, a true vertical span, and two ordinary data rows.
        for y in (700, 680, 640):
            canvas.line(40, y, 280, y)
        canvas.line(120, 660, 280, 660)
        for x in (40, 120, 280):
            canvas.line(x, 640, x, 700)
        for x, y, text in [(45, 687, "Group"), (125, 687, "Value"), (45, 657, "Shared"),
                           (125, 667, "-2"), (145, 647, "3-5 ft. *")]:
            canvas.drawString(x, y, text)
        draw_icon(canvas, "reaction", 125, 138, 8)
        canvas.drawString(40, 620, "After the table.")
        canvas.showPage(); canvas.save()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/"table.pdf"
            source.write_bytes(stream.getvalue())
            result = convert_pdf(source)
        tables = [b for b in result.blocks if b.kind == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows, [["Group", "Value"], ["Shared", "-2"], ["Shared", "[reaction] 3-5 ft. *"]])
        self.assertNotIn("complex_table", [w.code for w in result.warnings])
        for text in (result.markdown, result.text):
            self.assertEqual(text.count("Before the table."), 1)
            self.assertEqual(text.count("After the table."), 1)
            self.assertLess(text.index("Before"), text.index("Shared"))
            self.assertLess(text.index("3-5"), text.index("After"))
        self.assertEqual(result.text.count("[reaction]"), 1)


class OptionalLocalStructureTests(unittest.TestCase):
    def read_blocks(self, name, number):
        from pdf_translator.pdf_reader import _read_page
        with pdfplumber.open(ROOT/"example"/name, unicode_norm="NFC") as pdf:
            warnings = []
            data = _read_page(pdf.pages[number-1], warnings.append)
            blocks = _table_blocks(data, set(), warnings.append, lambda _: False)
        self.assertNotIn("complex_table", [w.code for w in warnings])
        return blocks

    @unittest.skipUnless((ROOT/"example/3. Chapter 1 Character Creation.pdf").is_file(), "Optional local PDF absent")
    def test_advancement_every_level_and_combined_final_choice(self):
        tables = self.read_blocks("3. Chapter 1 Character Creation.pdf", 11)
        self.assertEqual(len(tables), 2)
        rows = tables[0].rows
        self.assertEqual(rows[0], ["Tier", "Character Level", "Attribute Points", "Health Gained", "Maximum Skill Rank", "Skill Ranks", "Talents Gained"])
        self.assertEqual(len(rows), 21)
        for level, row in enumerate(rows[1:], 1):
            tier = (level-1)//5+1
            self.assertEqual(row[0:2], [str(tier), str(level)])
            self.assertEqual(row[2], "12 attribute points" if level == 1 else "+1 attribute point" if level % 3 == 0 else "—")
            self.assertEqual(row[3], "10 + STR health" if level == 1 else f"+{6-tier}" + (" + STR" if level in (6, 11, 16) else "") + " health")
            self.assertEqual(row[4], str(tier+1))
            self.assertEqual(row[5], "4 skill ranks (+1 from starting path)" if level == 1 else "+2 skill ranks")
            self.assertEqual(row[6], "1 talent from starting path plus ancestry bonus talent(s)" if level == 1 else
                             "+1 talent plus ancestry bonus talent" if level in (6, 11, 16) else "+1 talent")
        self.assertEqual(tables[1].rows, [
            ["Tier", "Character Level", "Attribute Points", "Health Gained", "Maximum Skill Rank", "Skill Ranks / Talents Gained"],
            ["5", "21+", "—", "+1 health", "5", "Either +1 skill rank or +1 talent (plus ancestry bonus talent at level 21 only)"]])

    @unittest.skipUnless((ROOT/"example/5. Chapter 3 Character Statistics.pdf").is_file(), "Optional local PDF absent")
    def test_skill_examples_has_six_columns_and_all_nine_tasks(self):
        tables = self.read_blocks("5. Chapter 3 Character Statistics.pdf", 21)
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows[0], ["Task", "Test DC", "Success", "Failure", "Opportunity", "Complication"])
        self.assertEqual([r[1] for r in tables[0].rows[1:]], [f"DC {n}" for n in (17, 14, 20, 20, 15, 22, 12, 12, 15)])
        self.assertTrue(all(len(r) == 6 and all(r) for r in tables[0].rows))
        self.assertIn("1d4 keen damage", tables[0].rows[2][5])

    @unittest.skipUnless((ROOT/"example/15. Chapter 13 Gamemastering.pdf").is_file(), "Optional local PDF absent")
    def test_rewards_headers_and_every_value(self):
        tables = self.read_blocks("15. Chapter 13 Gamemastering.pdf", 21)
        self.assertEqual(tables[0].rows, [
            ["Starting Tier", "Marks", *[f"— Rewards — / Tier {n}" for n in (1, 2, 3, "4+")]],
            ["Tier 1 (levels 2–5)", "200 mk", "1", "—", "—", "—"],
            ["Tier 2 (levels 6–10)", "1,000 mk", "2", "1", "—", "—"],
            ["Tier 3 (levels 11–15)", "2,000 mk", "2", "1", "1", "—"],
            ["Tier 4+ (levels 16+)", "5,000 mk", "2", "1", "1", "1"]])

    @unittest.skipUnless((ROOT/"example/15. Chapter 13 Gamemastering.pdf").is_file(), "Optional local PDF absent")
    def test_spren_group_scopes_are_separate_and_complete(self):
        blocks = self.read_blocks("15. Chapter 13 Gamemastering.pdf", 23)
        self.assertEqual([b.text for b in blocks if b.kind == "heading"], ["Emotion Spren", "Nature Spren"])
        tables = [b for b in blocks if b.kind == "table"]
        self.assertEqual([len(t.rows)-1 for t in tables], [9, 6])
        self.assertEqual([r[0] for r in tables[0].rows[1:]], ["Angerspren", "Anticipationspren", "Creationspren", "Exhaustionspren", "Fearspren", "Gloryspren", "Joyspren", "Painspren", "Shamespren"])
        self.assertEqual([r[0] for r in tables[1].rows[1:]], ["Decayspren", "Flamespren", "Lifespren", "Rainspren", "Rotspren", "Windspren"])

    @unittest.skipUnless((ROOT/"example/17. Appendix Animal Companions.pdf").is_file(), "Optional local PDF absent")
    def test_all_seven_animal_grids_with_exact_header_associations(self):
        expected = {1: [[2, 14, 2, 0, 10, 0, 3, 13, 0]],
                    2: [[4, 14, 0, 0, 11, 1, 3, 13, 0], [1, 14, 3, 1, 11, 0, 3, 14, 1]],
                    3: [[2, 15, 3, 0, 11, 1, 3, 13, 0], [3, 17, 4, 0, 11, 1, 3, 14, 1]],
                    6: [[1, 14, 3, 1, 13, 2, 3, 14, 1], [2, 17, 5, 2, 15, 3, 4, 16, 2]]}
        header = [f"{group} / {label}" for group, labels in
                  [("Physical", ("str", "def", "spd")), ("Cognitive", ("int", "def", "wil")), ("Spiritual", ("awa", "def", "pre"))]
                  for label in labels]
        for page, values in expected.items():
            with self.subTest(page=page):
                blocks = self.read_blocks("17. Appendix Animal Companions.pdf", page)
                self.assertEqual([b.rows for b in blocks], [[header, list(map(str, row))] for row in values])

    @unittest.skipUnless((ROOT/"example/15. Chapter 13 Gamemastering.pdf").is_file(), "Optional local PDF absent")
    def test_thief_grid_keeps_all_nine_values(self):
        blocks = self.read_blocks("15. Chapter 13 Gamemastering.pdf", 7)
        self.assertEqual(blocks[0].rows[1], ["1", "14", "3", "2", "13", "1", "2", "13", "1"])
        self.assertEqual(blocks[0].rows[0][4], "Cognitive / def")


if __name__ == "__main__":
    unittest.main()
