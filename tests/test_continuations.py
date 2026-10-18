"""Paragraph indents, hanging labels and conservative page/column continuations."""

import tempfile
import unittest
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.layout import merge_continuations
from pdf_translator.models import Block, Line, SourceRegion, Word
from tests.test_converter import PAGE, lines


class LabelLayoutTests(unittest.TestCase):
    def convert(self, draw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "labels.pdf"
            canvas = Canvas(str(path), pagesize=PAGE)
            draw(canvas)
            canvas.save()
            return convert_pdf(path)

    @staticmethod
    def label(canvas, label, text, *, x, top):
        lines(canvas, [label], x=x, top=top, font="Helvetica-Bold")
        width = canvas.stringWidth(label + " ", "Helvetica-Bold", 10)
        lines(canvas, [text], x=x + width, top=top)

    def test_first_line_indent_is_not_a_list(self):
        def draw(c):
            lines(c, ["An introductory paragraph."])
            self.label(c, "Loyal Cadre.", "You have soldiers under your", x=62, top=138)
            lines(c, ["command and they follow your orders."], top=152)
        result = self.convert(draw)
        block = next(b for b in result.blocks if b.text.startswith("Loyal Cadre."))
        self.assertEqual(block.kind, "paragraph")
        self.assertIn("your command", block.text)
        self.assertNotIn("- Loyal Cadre.", result.markdown)

    def test_hanging_bold_label_remains_a_list(self):
        def draw(c):
            self.label(c, "Save Others.", "You defend people who cannot", x=50, top=110)
            lines(c, ["protect themselves."], x=62, top=124)
            self.label(c, "Seek Truth.", "You have an enduring curiosity", x=50, top=138)
            lines(c, ["about the world."], x=62, top=152)
        result = self.convert(draw)
        self.assertEqual([b.kind for b in result.blocks], ["list_item", "list_item"])
        self.assertIn("- Save Others.", result.markdown)
        self.assertIn("- Seek Truth.", result.markdown)

    def test_single_line_bold_prose_does_not_borrow_next_paragraph_indent(self):
        def draw(c):
            self.label(c, "Profile.", "A short complete sentence.", x=50, top=110)
            lines(c, ["A new indented paragraph starts here."], x=62, top=124)
            lines(c, ["Its continuation returns to the column edge."], top=138)
        result = self.convert(draw)
        self.assertEqual([b.kind for b in result.blocks], ["paragraph", "paragraph"])
        self.assertEqual(result.blocks[0].text, "Profile. A short complete sentence.")

    def test_quoted_continuation_of_bold_prose_across_pages(self):
        def draw(c):
            self.label(c, "Loyal Cadre.", "You can find allies (in the earlier", x=62, top=680)
            lines(c, ["chapter about the allies mentioned in the earlier"], top=694)
            c.showPage()
            lines(c, ['"Companions" section). They follow your orders.'])
        result = self.convert(draw)
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(result.blocks[0].kind, "paragraph")
        self.assertIn('earlier "Companions" section). They', result.text)
        self.assertEqual([s.page for s in result.blocks[0].sources], [1, 2])
        self.assertNotIn("- Loyal Cadre.", result.markdown)

    def test_list_continuation_keeps_one_marker(self):
        def draw(c):
            lines(c, ["1. A long list item begins here and"], top=680)
            lines(c, ["continues across the page boundary with"], x=62, top=694)
            c.showPage()
            lines(c, ["more details before it ends."], x=62)
            lines(c, ["2. A different item."], top=138)
            lines(c, ["A paragraph after the list."], top=166)
        result = self.convert(draw)
        self.assertEqual([b.kind for b in result.blocks], ["list_item", "list_item", "paragraph"])
        self.assertIn("boundary with more details", result.blocks[0].text)
        self.assertEqual([s.page for s in result.blocks[0].sources], [1, 2])
        self.assertEqual(result.markdown.count("1. A long list"), 1)

    def test_implicit_list_continuation_keeps_one_bullet(self):
        def draw(c):
            self.label(c, "Save Others.", "You defend those who cannot", x=50, top=680)
            lines(c, ["protect themselves when they are faced with"], x=62, top=694)
            c.showPage()
            lines(c, ["danger and need your help."], x=62)
        result = self.convert(draw)
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(result.blocks[0].kind, "list_item")
        self.assertIn("with danger", result.text)
        self.assertEqual(result.markdown.count("- Save Others."), 1)

    def test_first_line_indent_on_next_page_stays_a_new_paragraph(self):
        def draw(c):
            lines(c, ["An unfinished paragraph that could continue with"], top=700)
            c.showPage()
            lines(c, ["a new indented paragraph begins here"], x=62)
            lines(c, ["and its next line returns to the column edge."], top=124)
        result = self.convert(draw)
        self.assertEqual(len(result.blocks), 2)
        self.assertIn("with\n\na new", result.text)
        self.assertEqual([s.page for s in result.blocks[1].sources], [2])


def prose(text, *, page=1, x=50, top=680, kind="paragraph", indent=0,
          font="Helvetica", width=240):
    box = (x + indent, top, x + width, top + 10)
    line = Line([Word(text, box, 10, font)], box, 10)
    return Block(kind, text, (SourceRegion(page, box),), lines=[line],
                 body_size=10, container_width=width, container_left=x)


class ContinuationRulesTests(unittest.TestCase):
    def merge(self, *blocks):
        warnings = []
        result = merge_continuations(list(blocks), set(), warnings.append)
        self.assertFalse(warnings)
        return result

    def test_quoted_lowercase_continuation(self):
        result = self.merge(prose("The rule uses the term"),
                            prose('“companion” for this ally.', page=2, top=100))
        self.assertEqual(len(result), 1)
        self.assertIn("term “companion”", result[0].text)

    def test_capitalized_quotation_closes_an_open_parenthesis(self):
        for opening, closing in [("(", ")"), ("[", "]")]:
            with self.subTest(opening=opening):
                result = self.merge(prose("See the earlier " + opening + "chapter about"),
                                    prose("“Companions” section" + closing + ".", page=2, x=318))
                self.assertEqual(len(result), 1)
                self.assertEqual([s.page for s in result[0].sources], [1, 2])

    def test_do_not_merge_new_indented_paragraph_even_if_lowercase(self):
        result = self.merge(prose("An unfinished line with no final punctuation"),
                            prose("a new indented paragraph.", page=2, indent=12))
        self.assertEqual(len(result), 2)

    def test_do_not_merge_a_completed_sentence(self):
        for ending in (".", "?", "!", ":", '.”', "?)"):
            with self.subTest(ending=ending):
                self.assertEqual(len(self.merge(prose("The sentence ends" + ending),
                                                prose("another paragraph.", page=2))), 2)

    def test_unrelated_capitalized_start_is_not_enough(self):
        for start in ("Another paragraph.", "“Companions” is a new topic."):
            with self.subTest(start=start):
                self.assertEqual(len(self.merge(prose("An unfinished line"),
                                                prose(start, page=2))), 2)

    def test_closed_parenthesis_earlier_does_not_authorize_a_merge(self):
        self.assertEqual(len(self.merge(prose("An earlier (completed aside) and more text"),
                                        prose("“Companions” (a new topic).", page=2))), 2)

    def test_closing_bracket_after_a_new_sentence_is_not_enough(self):
        self.assertEqual(len(self.merge(prose("An unfinished (aside about"),
                                        prose("“Companions” is a topic. Unrelated note).", page=2))), 2)

    def test_footnote_or_rotated_text_does_not_break_main_continuation(self):
        for kind, role in (("footnote", ""), ("raw", "rotated")):
            with self.subTest(kind=kind):
                note = prose("An independent annotation.", kind=kind)
                note.role = role
                result = self.merge(prose("An unfinished line"), note,
                                    prose("more text.", page=2))
                self.assertEqual(len(result), 2)
                self.assertIn("line more text.", result[0].text)
                self.assertIs(result[1], note)

    def test_heading_table_aside_raw_and_new_list_are_barriers(self):
        for kind in ("heading", "table", "aside", "raw", "list_item"):
            with self.subTest(kind=kind):
                self.assertEqual(len(self.merge(prose("An unfinished line"),
                                                prose("more text.", page=2, kind=kind))), 2)
                self.assertEqual(len(self.merge(prose("An unfinished line"),
                                                prose("Intervening block", page=2, kind=kind),
                                                prose("more text.", page=2, top=140))), 3)

    def test_nonconsecutive_pages_and_same_column_do_not_merge(self):
        for page, x in ((3, 50), (1, 50)):
            with self.subTest(page=page):
                self.assertEqual(len(self.merge(prose("An unfinished line"),
                                                prose("more text.", page=page, x=x))), 2)

    def test_different_body_font_or_size_does_not_merge(self):
        for font, size in (("Courier", 10), ("Helvetica", 14)):
            following = prose("more text.", page=2, font=font)
            following.lines[0].size = size
            with self.subTest(font=font, size=size):
                self.assertEqual(len(self.merge(prose("An unfinished line"), following)), 2)

    def test_subset_font_prefix_does_not_block_a_continuation(self):
        result = self.merge(prose("An unfinished line", font="AAAAAA+Helvetica"),
                            prose("more text.", page=2, font="BBBBBB+Helvetica"))
        self.assertEqual(len(result), 1)

    def test_short_last_line_is_not_a_clear_continuation(self):
        previous = prose("An unfinished line")
        previous.lines[0].bbox = (50, 680, 140, 690)
        self.assertEqual(len(self.merge(previous, prose("more text.", page=2))), 2)

    def test_quoted_continuation_across_columns(self):
        result = self.merge(prose("See (the earlier"),
                            prose("“Companions” section).", x=318, top=100))
        self.assertEqual(len(result), 1)
        self.assertEqual([s.page for s in result[0].sources], [1, 1])

    def test_merged_block_uses_last_column_geometry_for_next_boundary(self):
        result = self.merge(prose("An unfinished line", width=240),
                            prose("still continuing", page=1, x=318, width=160),
                            prose("and ending here.", page=2, width=240))
        self.assertEqual(len(result), 1)
        self.assertEqual([s.page for s in result[0].sources], [1, 1, 2])
        self.assertEqual(result[0].container_width, 240)


if __name__ == "__main__":
    unittest.main()
