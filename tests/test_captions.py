"""Figure labels must disappear without masking prose behind image layers."""

import tempfile
import unittest
from pathlib import Path

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Table, TableStyle

from pdf_translator import convert_pdf

HEIGHT = 792


def text(canvas, values, *, x=50, top=110, font="Times-Roman", size=10, color=colors.black, leading=14):
    canvas.setFillColor(color)
    canvas.setFont(font, size)
    for i, value in enumerate(values):
        canvas.drawString(x, HEIGHT-top-i*leading, value)


def picture(canvas, *, x=325, top=110, width=230, height=420):
    canvas.drawImage(ImageReader(Image.new("RGB", (20, 30), (180, 190, 200))), x, HEIGHT-top-height, width, height)


def prose(canvas):
    text(canvas, ["The main paragraph occupies the left column.",
                  "Its words must be retained even near artwork.",
                  "Another line establishes the ordinary text style."])


def label(canvas, values, *, x=365, top=80, color=colors.Color(.4, .5, .6), leading=14):
    text(canvas, values, x=x, top=top, font="Helvetica-Bold", size=12, color=color, leading=leading)


class CaptionTests(unittest.TestCase):
    def convert(self, draw):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "captions.pdf"
            c = Canvas(str(source), pagesize=(612, HEIGHT))
            draw(c)
            c.save()
            return convert_pdf(source)

    def test_styled_caption_above_picture_is_removed_in_both_formats(self):
        def draw(c):
            picture(c)
            prose(c)
            label(c, ["PORTRAIT NAME"])
        result = self.convert(draw)
        for output in (result.markdown, result.text):
            self.assertNotIn("PORTRAIT NAME", output)
            self.assertIn("Its words must be retained even near artwork.", output)

    def test_two_line_label_does_not_interrupt_a_sentence(self):
        def draw(c):
            picture(c)
            text(c, ["So long as you earn their", "ongoing loyalty, the troops obey."], top=80)
            label(c, ["HOUSE OF TEST", "HERALDRY"])
        result = self.convert(draw)
        self.assertIn("earn their ongoing loyalty", result.text)
        self.assertNotIn("HOUSE OF TEST", result.text)
        self.assertNotIn("HERALDRY", result.text)

    def test_monochrome_caption_below_picture_is_removed(self):
        def draw(c):
            picture(c, top=100, height=300)
            prose(c)
            label(c, ["BLACK CAPTION"], top=420, color=colors.black)
        self.assertNotIn("BLACK CAPTION", self.convert(draw).text)

    def test_body_sized_caption_with_distinct_colour_is_removed(self):
        def draw(c):
            picture(c)
            prose(c)
            text(c, ["COLOURED CAPTION", "SECOND LABEL LINE"], x=355, top=80, color=colors.Color(.4, .5, .6))
        result = self.convert(draw)
        self.assertNotIn("COLOURED CAPTION", result.text)
        self.assertNotIn("SECOND LABEL LINE", result.text)
        self.assertIn("The main paragraph occupies the left column.", result.text)

    def test_page_background_is_not_a_figure(self):
        def draw(c):
            picture(c, x=0, top=0, width=612, height=792)
            prose(c)
            label(c, ["UNRELATED HEADING"])
        self.assertIn("UNRELATED HEADING", self.convert(draw).text)

    def test_overlapping_art_does_not_mask_main_text(self):
        def draw(c):
            picture(c, x=225, top=100, width=380, height=600)
            prose(c)
            label(c, ["ISOLATED FIGURE"])
        result = self.convert(draw)
        self.assertNotIn("ISOLATED FIGURE", result.text)
        self.assertIn("The main paragraph occupies the left column.", result.text)
        self.assertIn("Another line establishes the ordinary text style.", result.text)

    def test_section_heading_followed_by_prose_is_retained(self):
        def draw(c):
            picture(c)
            prose(c)
            label(c, ["Important rules"], x=330)
            text(c, ["This is a real section below its heading."], x=330, top=110)
        self.assertIn("Important rules", self.convert(draw).text)

    def test_table_title_and_cells_are_protected(self):
        def draw(c):
            picture(c, x=320, top=100)
            prose(c)
            label(c, ["TABLE TITLE"], x=330)
            table = Table([["Tier", "Benefit"], ["1", "First"], ["2", "Second"]], colWidths=[75, 145], rowHeights=24)
            table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .5, colors.black)]))
            table.wrapOn(c, 300, 600)
            table.drawOn(c, 330, HEIGHT-100-72)
        result = self.convert(draw)
        self.assertIn("TABLE TITLE", result.text)
        self.assertIn("| Tier | Benefit |", result.markdown)
        self.assertIn("2\tSecond", result.text)

    def test_sidebar_title_and_text_are_protected(self):
        def draw(c):
            prose(c)
            c.setFillColor(colors.lightgrey)
            c.rect(325, HEIGHT-80-140, 235, 140, fill=1)
            picture(c, x=330, top=90, width=220, height=100)
            label(c, ["SIDEBAR TITLE"], x=335, top=100)
            text(c, ["These sidebar instructions must stay.", "They are not a figure caption."], x=335, top=130)
        result = self.convert(draw)
        self.assertIn("SIDEBAR TITLE", result.text)
        self.assertIn("These sidebar instructions must stay.", result.text)

    def test_proximity_without_distinct_style_is_not_enough(self):
        def draw(c):
            picture(c)
            prose(c)
            text(c, ["Uncertain text is retained"], x=350, top=80)
        self.assertIn("Uncertain text is retained", self.convert(draw).text)

    def test_long_isolated_block_is_not_partially_deleted(self):
        values = ["First line of an isolated block", "Second line of this same block",
                  "Third line continues the block", "Fourth line completes this block"]
        def draw(c):
            picture(c, top=140)
            prose(c)
            # Keep the body font dominant while testing a four-line candidate.
            text(c, ["Additional main text establishes the normal font size and style."], top=180)
            label(c, values, x=330, top=100, leading=10)
        result = self.convert(draw)
        for value in values:
            self.assertIn(value, result.text)


if __name__ == "__main__":
    unittest.main()
