"""Exercise circular labels through PDF extraction and both exporters."""

import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf


class CircularLabelTests(unittest.TestCase):
    def convert(self, *, table_name="Starling*", circle=True):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "wheel.pdf"
            c = Canvas(str(source), pagesize=(400, 500))
            if circle:
                c.circle(200, 350, 43)
            for index, letter in enumerate("STARLING"):
                angle = (index - 3.5) * 12
                radians = math.radians(angle)
                c.saveState()
                c.translate(200 + 35 * math.sin(radians), 350 - 35 * math.cos(radians))
                c.rotate(angle)
                c.setFont("Helvetica", 10)
                c.drawCentredString(0, 0, letter)
                c.restoreState()
            c.setFont("Helvetica", 10)
            c.drawCentredString(200, 351, "Standalone label +")
            c.drawString(45, 411, "Read this explanation before consulting the table.")
            for x in (60, 220, 340):
                c.line(x, 220, x, 260)
            for y in (220, 240, 260):
                c.line(60, y, 340, y)
            for x, y, value in ((70, 246, "Name"), (230, 246, "Color"),
                                (70, 226, table_name), (230, 226, "Blue")):
                c.drawString(x, y, value)
            c.drawString(45, 192, "*See the note below the table.")
            c.save()
            return convert_pdf(source, hyphen_resolver=lambda _: False)

    def test_rejoins_exact_table_token_and_keeps_neighbors(self):
        result = self.convert()
        labels = [b for b in result.blocks if b.role == "graphic_label"]
        self.assertEqual([(b.kind, b.text, b.diagram) for b in labels], [("raw", "STARLING", None)])
        self.assertEqual(labels[0].sources[0].page, 1)
        self.assertIn("> STARLING\n", result.markdown)
        self.assertIn("\nSTARLING\n", result.text)
        self.assertEqual([b.rows for b in result.blocks if b.kind == "table"],
                         [[["Name", "Color"], ["Starling*", "Blue"]]])
        for export in (result.markdown, result.text):
            self.assertIn("Standalone label +", export)
            self.assertIn("Read this explanation before consulting the table.", export)
            self.assertIn("See the note below the table.", export)
        self.assertFalse(result.warnings)

    def test_unconfirmed_join_keeps_fragments_in_one_warned_raw_block(self):
        result = self.convert(table_name="Starlings")
        labels = [b for b in result.blocks if b.role == "graphic_label"]
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].kind, "raw")
        self.assertIn(" / ", labels[0].text)
        self.assertEqual(labels[0].text.replace(" / ", ""), "STARLING")
        self.assertIn(labels[0].text, result.markdown)
        self.assertIn(labels[0].text, result.text)
        self.assertEqual([(w.code, w.pages) for w in result.warnings], [("graphic_text", (1,))])

    def test_angled_fragments_and_matching_table_without_circle_do_not_join(self):
        result = self.convert(circle=False)
        self.assertFalse(any(b.kind == "raw" for b in result.blocks))
        self.assertNotIn("\nSTARLING\n", result.text)
        self.assertIn("Standalone label +", result.text)
        self.assertIn("Starling*\tBlue", result.text)
