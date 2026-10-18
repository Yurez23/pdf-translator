"""A framed sidebar is one quote container, with its internal paragraphs intact."""

import tempfile
import unittest
from pathlib import Path

from reportlab.lib import colors
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.export import render
from pdf_translator.models import Block, SourceRegion
from tests.test_converter import PAGE, lines


def sidebar(canvas, *, title="Sidebar title", top=160, x=50, paragraphs=2):
    canvas.setFillColor(colors.lightgrey)
    canvas.rect(x, PAGE[1] - top - 180, 240, 180, fill=1)
    canvas.setFillColor(colors.black)
    lines(canvas, [title], x=x + 10, top=top + 25, size=12, font="Helvetica-Bold")
    for i in range(paragraphs):
        lines(canvas, [f"Paragraph {i + 1} starts with useful information and",
                       "continues without a physical line break."],
              x=x + 10, top=top + 55 + i * 45)


class AsideLayoutTests(unittest.TestCase):
    def convert(self, draw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "asides.pdf"
            canvas = Canvas(str(path), pagesize=PAGE)
            draw(canvas)
            canvas.save()
            return convert_pdf(path)

    def test_one_frame_is_one_container_with_title_and_two_paragraphs(self):
        def draw(c):
            lines(c, ["Main text outside the sidebar remains independent."], top=110)
            sidebar(c)
        result = self.convert(draw)
        asides = [b for b in result.blocks if b.kind == "aside"]
        self.assertEqual(len(asides), 1)
        aside = asides[0]
        self.assertEqual([b.kind for b in aside.children], ["heading", "paragraph", "paragraph"])
        self.assertEqual(aside.children[0].text, "Sidebar title")
        self.assertEqual(aside.children[0].role, "aside_title")
        self.assertEqual(len(aside.sources), 1)
        self.assertTrue(all(b.sources[0].page == aside.sources[0].page for b in aside.children))
        self.assertNotIn("Main text", aside.text)
        expected = ("> **Sidebar title**\n>\n"
                    "> Paragraph 1 starts with useful information and continues without a physical line break.\n>\n"
                    "> Paragraph 2 starts with useful information and continues without a physical line break.")
        self.assertIn(expected, result.markdown)
        self.assertEqual(result.text.count("Paragraph 1 starts"), 1)
        self.assertEqual(result.text.count("Paragraph 2 starts"), 1)
        self.assertIn("Sidebar title\n\nParagraph 1", result.text)

    def test_adjacent_frames_stay_separate(self):
        def draw(c):
            sidebar(c, title="First sidebar", top=160)
            sidebar(c, title="Second sidebar", top=370)
        result = self.convert(draw)
        asides = [b for b in result.blocks if b.kind == "aside"]
        self.assertEqual(len(asides), 2)
        self.assertEqual([b.children[0].text for b in asides], ["First sidebar", "Second sidebar"])
        self.assertIn("physical line break.\n\n> **Second sidebar**", result.markdown)

    def test_sidebar_without_a_heading_retains_two_paragraphs(self):
        def draw(c):
            c.setFillColor(colors.lightgrey)
            c.rect(50, 400, 240, 170, fill=1)
            c.setFillColor(colors.black)
            lines(c, ["The first paragraph begins here and", "continues on its second line."], x=60, top=250)
            lines(c, ["The second paragraph is separate.", "Its lines must join as well."], x=60, top=295)
        result = self.convert(draw)
        aside = next(b for b in result.blocks if b.kind == "aside")
        self.assertEqual([b.kind for b in aside.children], ["paragraph", "paragraph"])
        self.assertIn("second line.\n>\n> The second paragraph", result.markdown)
        self.assertNotIn("**", result.markdown)

    def test_sidebar_list_items_remain_inside_one_quote(self):
        def draw(c):
            c.setFillColor(colors.lightgrey)
            c.rect(50, 400, 240, 180, fill=1)
            c.setFillColor(colors.black)
            lines(c, ["Useful options"], size=12, font="Helvetica-Bold", top=245, x=60)
            lines(c, ["- First item contains useful information."], top=275, x=60)
            lines(c, ["- Second item remains a separate entry."], top=303, x=60)
            lines(c, ["A paragraph after the list is still inside."], top=340, x=60)
        result = self.convert(draw)
        asides = [b for b in result.blocks if b.kind == "aside"]
        self.assertEqual(len(asides), 1)
        self.assertEqual([b.kind for b in asides[0].children], ["heading", "list_item", "list_item", "paragraph"])
        self.assertIn("> - First item", result.markdown)
        self.assertIn("entry.\n>\n> A paragraph after", result.markdown)

    def test_sidebar_hyphen_choice_is_not_requested_twice(self):
        def draw(c):
            c.setFillColor(colors.lightgrey)
            c.rect(50, 400, 240, 180, fill=1)
            c.setFillColor(colors.black)
            lines(c, ["A remote fort-", "ress guards this road."], x=60, top=260)
        # Use a separate invocation to inspect callback side effects.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hyphen.pdf"
            c = Canvas(str(path), pagesize=PAGE)
            draw(c)
            c.save()
            issues = []
            result = convert_pdf(path, hyphen_resolver=lambda issue: issues.append(issue) or True)
        self.assertEqual(len(issues), 1)
        self.assertEqual(result.text.count("fortress"), 1)


class AsideExportTests(unittest.TestCase):
    def test_titles_and_paragraph_syntax_are_escaped_inside_the_container(self):
        source = (SourceRegion(1, (0, 0, 1, 1)),)
        title = Block("heading", "A *literal* title", source, role="aside_title")
        paragraph = Block("paragraph", "# Not an extra heading", source)
        aside = Block("aside", "Flat search text must not be duplicated", source,
                      children=[title, paragraph])
        md, txt = render([aside])
        self.assertEqual(md, "> **A \\*literal\\* title**\n>\n> \\# Not an extra heading\n")
        self.assertEqual(txt, "A *literal* title\n\n# Not an extra heading\n")
        self.assertNotIn("Flat search text", md + txt)

    def test_independent_unframed_quotes_are_not_merged(self):
        source = (SourceRegion(1, (0, 0, 1, 1)),)
        md, txt = render([Block("aside", "First quotation.", source),
                          Block("aside", "Second quotation.", source)])
        self.assertEqual(md, "> First quotation.\n\n> Second quotation.\n")
        self.assertEqual(txt, "First quotation.\n\nSecond quotation.\n")

    def test_plain_legacy_asides_quotes_and_footnotes_remain_supported(self):
        source = (SourceRegion(1, (0, 0, 1, 1)),)
        for kind in ("aside", "footnote", "raw"):
            with self.subTest(kind=kind):
                md, txt = render([Block(kind, "A plain block.", source)])
                self.assertEqual(md, "> A plain block.\n")
                self.assertEqual(txt, "A plain block.\n")


if __name__ == "__main__":
    unittest.main()
