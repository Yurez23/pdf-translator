"""Document-wide heading calibration, independent of particular book sizes."""

import tempfile
import unittest
from pathlib import Path

from reportlab.lib import colors
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Table, TableStyle

from pdf_translator import convert_pdf
from pdf_translator.headings import assign_heading_levels
from pdf_translator.models import Block, Line, SourceRegion, Word
from tests.test_converter import PAGE, lines


def prose(canvas, top, *, x=50):
    lines(canvas, ["Ordinary body text establishes the usual font size and style.",
                   "This paragraph contains enough words for a reliable comparison.",
                   "All of its content must survive the heading calibration."], top=top, x=x)


def section(canvas, title, size, top, *, font="Helvetica"):
    lines(canvas, [title], size=size, top=top, font=font)
    prose(canvas, top + 24)


def table(canvas, *, top=260, merged=False):
    value = Table([["Name", "Value"], ["First", "1"], ["Second", "2"]],
                  colWidths=[120, 150], rowHeights=24)
    commands = [("GRID", (0, 0), (-1, -1), .5, colors.black)]
    if merged:
        commands.append(("SPAN", (0, 0), (1, 0)))
    value.setStyle(TableStyle(commands))
    value.wrapOn(canvas, 400, 600)
    value.drawOn(canvas, 50, PAGE[1] - top - 72)


class DocumentHeadingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "headings.pdf"

    def convert(self, draw, **kwargs):
        canvas = Canvas(str(self.path), pagesize=PAGE)
        draw(canvas)
        canvas.save()
        return convert_pdf(self.path, **kwargs)

    @staticmethod
    def levels(result):
        return {b.text: b.level for b in result.blocks if b.kind == "heading" and not b.role}

    def test_four_styles_use_four_levels_without_book_specific_sizes(self):
        for sizes in ((26, 21, 16, 13), (24, 20, 17, 14)):
            with self.subTest(sizes=sizes):
                def draw(c):
                    for i, size in enumerate(sizes):
                        section(c, f"Section {i}", size, 90 + i * 140)
                result = self.convert(draw)
                self.assertEqual(self.levels(result), {f"Section {i}": i + 1 for i in range(4)})
                self.assertIn("#### Section 3\n", result.markdown)

    def test_larger_style_on_later_page_is_seen_before_assigning_levels(self):
        def draw(c):
            section(c, "A subsection", 16, 100)
            c.showPage()
            section(c, "A parent section", 22, 100)
        result = self.convert(draw)
        self.assertEqual(self.levels(result), {"A subsection": 2, "A parent section": 1})

    def test_page_selection_preserves_document_levels(self):
        def draw(c):
            for i, size in enumerate((26, 21, 16, 13)):
                section(c, f"Section {i}", size, 100)
                c.showPage()
        whole = self.convert(draw)
        for pages, expected in (("3", {"Section 2": 3}),
                                ("2,4", {"Section 1": 2, "Section 3": 4})):
            with self.subTest(pages=pages):
                selected = convert_pdf(self.path, pages=pages)
                self.assertEqual(self.levels(selected), expected)
                self.assertTrue(all(self.levels(whole)[name] == level for name, level in expected.items()))
                self.assertNotIn("Section 0", selected.text)

    def test_near_equal_sizes_form_one_style_band(self):
        def draw(c):
            section(c, "Root", 24, 90)
            section(c, "First peer", 18, 230)
            section(c, "Second peer", 18.15, 370)
            section(c, "Nested", 16, 510)
        self.assertEqual(self.levels(self.convert(draw)),
                         {"Root": 1, "First peer": 2, "Second peer": 2, "Nested": 3})

    def test_different_fonts_of_the_same_size_do_not_invent_depth(self):
        def draw(c):
            section(c, "Root", 24, 90)
            section(c, "Regular peer", 18, 230)
            section(c, "Bold peer", 18, 370, font="Helvetica-Bold")
        self.assertEqual(self.levels(self.convert(draw)),
                         {"Root": 1, "Regular peer": 2, "Bold peer": 2})

    def test_table_title_is_separate_from_the_section_hierarchy(self):
        def draw(c):
            section(c, "Root", 22, 100)
            lines(c, ["A Table Title"], size=28, top=250, font="Courier-Bold")
            table(c)
            section(c, "Child", 16, 420)
        result = self.convert(draw)
        self.assertEqual(self.levels(result), {"Root": 1, "Child": 2})
        caption = next(b for b in result.blocks if b.text == "A Table Title")
        self.assertEqual(caption.role, "table_title")
        self.assertIn("**A Table Title**\n", result.markdown)
        self.assertIn("A Table Title\n\nName\tValue", result.text)

    def test_shared_section_style_immediately_above_table_remains_a_section(self):
        def draw(c):
            section(c, "Root", 24, 80)
            section(c, "First section", 18, 200)
            lines(c, ["Second section"], size=18, top=370)
            table(c, top=380)
        result = self.convert(draw)
        self.assertEqual(self.levels(result), {"Root": 1, "First section": 2, "Second section": 2})

    def test_complex_table_title_is_also_outside_the_main_hierarchy(self):
        def draw(c):
            section(c, "Root", 22, 100)
            lines(c, ["A Complex Table"], size=28, top=250, font="Courier-Bold")
            table(c, merged=True)
            section(c, "Child", 16, 420)
        result = self.convert(draw)
        self.assertEqual(self.levels(result), {"Root": 1, "Child": 2})
        self.assertIn("**A Complex Table**", result.markdown)
        self.assertTrue(any(w.code == "complex_table" for w in result.warnings))

    def test_sidebar_heading_does_not_add_a_main_heading_level(self):
        def draw(c):
            section(c, "Root", 22, 90)
            c.setFillColor(colors.lightgrey)
            c.rect(50, PAGE[1] - 200 - 160, 270, 160, fill=1)
            c.setFillColor(colors.black)
            lines(c, ["Sidebar"], size=30, top=235, x=60, font="Helvetica-Bold")
            prose(c, 270, x=60)
            section(c, "Child", 16, 440)
        result = self.convert(draw)
        self.assertEqual(self.levels(result), {"Root": 1, "Child": 2})
        self.assertTrue(any(b.kind == "aside" and "Sidebar" in b.text for b in result.blocks))

    def test_small_centered_title_label_does_not_become_a_subsection(self):
        def draw(c):
            c.setFont("Helvetica-Bold", 14)
            c.drawCentredString(306, PAGE[1] - 80, "Volume Four")
            c.setFont("Times-Bold", 26)
            c.drawCentredString(306, PAGE[1] - 110, "The Main Title")
            prose(c, 150)
            section(c, "Section", 18, 270)
        result = self.convert(draw)
        self.assertEqual(self.levels(result), {"The Main Title": 1, "Section": 2})
        self.assertIn("**Volume Four**", result.markdown)
        self.assertIn("Volume Four\n\nThe Main Title", result.text)

    def test_one_heading_is_level_one_even_when_only_slightly_larger_than_body(self):
        result = self.convert(lambda c: section(c, "Only heading", 12, 100))
        self.assertEqual(self.levels(result), {"Only heading": 1})

    def test_no_headings_is_valid(self):
        result = self.convert(lambda c: prose(c, 100))
        self.assertEqual(self.levels(result), {})
        self.assertIn("All of its content must survive", result.text)

    def test_more_than_six_levels_are_capped_with_a_warning(self):
        def draw(c):
            for i, size in enumerate((31, 28, 25, 22, 19, 16, 13)):
                section(c, f"Depth {i}", size, 80 + i * 95)
        result = self.convert(draw)
        self.assertEqual(self.levels(result), {f"Depth {i}": min(i + 1, 6) for i in range(7)})
        self.assertTrue(any(w.code == "heading_depth" and w.pages == (1,) for w in result.warnings))

    def test_unselected_pages_do_not_prompt_or_emit_warnings(self):
        def draw(c):
            section(c, "Selected section", 18, 100)
            c.showPage()
            lines(c, ["A distant fort-", "ress appears here."])
            c.showPage()
            c.showPage()
        result = self.convert(draw, pages="1", hyphen_resolver=lambda _: self.fail("Unselected ambiguity prompted"))
        self.assertFalse(result.warnings)
        self.assertNotIn("distant", result.text)
        self.assertTrue(all(s.page == 1 for b in result.blocks for s in b.sources))

    def test_deep_styles_on_unselected_pages_do_not_emit_warnings(self):
        def draw(c):
            for i, size in enumerate((31, 28, 25, 22, 19, 16, 13)):
                section(c, f"Depth {i}", size, 100)
                c.showPage()
        result = self.convert(draw, pages="1")
        self.assertEqual(self.levels(result), {"Depth 0": 1})
        self.assertFalse(result.warnings)

    def test_selected_hyphen_choice_is_requested_only_once(self):
        def draw(c):
            section(c, "Root", 24, 100)
            c.showPage()
            section(c, "Child", 16, 100)
            lines(c, ["A distant fort-", "ress appears here."], top=200)
        issues = []
        def resolve(issue):
            issues.append(issue)
            return True
        result = self.convert(draw, pages="2", hyphen_resolver=resolve)
        self.assertEqual(len(issues), 1)
        self.assertIn("fortress", result.text)
        self.assertEqual(self.levels(result), {"Child": 2})

    def test_multiline_heading_and_paragraphs_are_preserved(self):
        def draw(c):
            section(c, "Root", 24, 100)
            lines(c, ["A wrapped subsection", "with its second line"], top=270, size=16, leading=18)
            prose(c, 320)
        result = self.convert(draw)
        self.assertEqual(self.levels(result), {"Root": 1, "A wrapped subsection with its second line": 2})
        self.assertEqual(result.text.count("This paragraph contains enough words"), 2)


class HeadingStyleTests(unittest.TestCase):
    def test_subset_prefix_does_not_turn_a_repeated_section_style_into_a_caption(self):
        def heading(text, y, font):
            box = (50, y, 250, y + 18)
            word = Word(text, box, 18, font)
            return Block("heading", text, (SourceRegion(1, box),),
                         lines=[Line([word], box, 18)])
        first = heading("First section", 100, "AAAAAA+Helvetica")
        body = Block("paragraph", "Body.", (SourceRegion(1, (50, 130, 250, 150)),))
        second = heading("Second section", 270, "BBBBBB+Helvetica")
        values = Block("table", "", (SourceRegion(1, (50, 300, 320, 400)),), rows=[["A", "B"], ["1", "2"]])
        warnings = []
        assign_heading_levels([first, body, second, values], {1}, warnings.append)
        self.assertEqual((first.level, second.level), (1, 1))
        self.assertEqual(second.role, "")
        self.assertFalse(warnings)


if __name__ == "__main__":
    unittest.main()
