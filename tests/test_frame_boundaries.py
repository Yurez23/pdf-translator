"""A decorative fill must not claim the ends of adjacent prose lines."""

import tempfile
import unittest
from pathlib import Path

from reportlab.lib import colors
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.geometry import group_lines
from pdf_translator.models import Glyph
from pdf_translator.page_data import FormRegion, PageData, TableData
from pdf_translator.pdf_reader import _frame_cuts_body_flow
from tests.test_converter import PAGE, lines
from tests.test_layout_recovery import line


class DecorativeFrameTests(unittest.TestCase):
    def test_oval_overlapping_prose_tails_does_not_interrupt_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'decorative-fill.pdf'
            canvas = Canvas(str(path), pagesize=PAGE)
            canvas.setFillColor(colors.white)
            canvas.ellipse(276, PAGE[1] - 706, 590, PAGE[1] - 534,
                           fill=1, stroke=0)
            canvas.setFillColor(colors.black)
            lines(canvas, ['Ordinary body text anchors the main column.'] * 12,
                  x=42, top=116, size=10)
            lines(canvas, ['A passage begins here and explains the rule.'],
                  x=42, top=532, size=10)
            # Different text and metrics from the source; only the final word
            # centres overlap the oval's bounding box on two ordinary lines.
            tails = ['An ordinary sentence continues across the page with a',
                     'second line that also ends with a clearly written value (3).']
            lines(canvas, tails, x=42, top=545, size=10)
            lines(canvas, ['Other column'], x=328, top=90,
                  size=14, font='Helvetica-Bold')
            lines(canvas, ['Independent rules remain in their own column.'] * 8,
                  x=328, top=116, size=10)
            lines(canvas, ['A separate quotation stays independent.',
                           'Its remaining words belong to this quotation.'],
                  x=345, top=605, size=12, font='Times-Italic', leading=16)
            canvas.save()
            result = convert_pdf(path, hyphen_resolver=lambda _: False)

        expected = 'A passage begins here and explains the rule. ' + ' '.join(tails)
        matches = [b for b in result.blocks if expected in b.text]
        self.assertEqual(len(matches), 1)
        paragraph = matches[0]
        self.assertEqual(paragraph.kind, 'paragraph')
        other = next(b for b in result.blocks if b.text == 'Other column')
        self.assertLess(result.blocks.index(paragraph), result.blocks.index(other))
        quote = next(b for b in result.blocks if 'A separate quotation' in b.text)
        self.assertEqual(quote.role, 'quotation')
        self.assertNotIn('clearly written value', quote.text)
        for output in (result.markdown, result.text):
            self.assertEqual(output.count(expected), 1)
            self.assertLess(output.index(expected), output.index('Other column'))


class FrameBoundaryEvidenceTests(unittest.TestCase):
    def scene(self, *, prefix=False):
        rows = [line('A normal passage keeps its words beside a note here.', y,
                     x=250 if prefix else 50) for y in (400, 414)]
        word = rows[0].words[0 if prefix else -1]
        edge = (word.bbox[0]+word.bbox[2])/2
        box = (20, 380, edge, 600) if prefix else (edge, 380, 590, 600)
        page = PageData(1, 612, 792, [w for row in rows for w in row.words], 10)
        return page, box

    def cuts(self, page, box):
        return _frame_cuts_body_flow(box, page, group_lines(page.words, page.body_size, split_gaps=True))

    def test_repeated_prose_intrusion_at_either_side_and_other_scales(self):
        for prefix in (False, True):
            for scale in (.7, 1, 1.5):
                with self.subTest(prefix=prefix, scale=scale):
                    page, box = self.scene(prefix=prefix)
                    box = tuple(n*scale for n in box)
                    for word in page.words:
                        word.bbox = tuple(n*scale for n in word.bbox)
                        word.size *= scale
                    page.body_size *= scale
                    self.assertTrue(self.cuts(page, box))

    def test_a_single_clipped_line_is_insufficient(self):
        page, box = self.scene()
        page.words = [w for w in page.words if w.bbox[1] == 400]
        self.assertFalse(self.cuts(page, box))

    def test_real_box_owning_whole_lines_is_not_rejected(self):
        page, _ = self.scene()
        self.assertFalse(self.cuts(page, (40, 380, 590, 600)))

    def test_adjacent_columns_separated_by_space_are_not_continuous_prose(self):
        page, box = self.scene()
        for word in page.words:
            if word.text == 'here.':
                x0, top, x1, bottom = word.bbox
                word.bbox = (x0+15, top, x1+15, bottom)
        self.assertFalse(self.cuts(page, box))

    def test_matching_size_without_matching_face_or_colour_is_insufficient(self):
        for attribute, value in (('font', 'Helvetica-Oblique'), ('color', (.4, .6, .8)),
                                 ('size', 8)):
            with self.subTest(attribute=attribute):
                page, box = self.scene()
                for word in page.words:
                    if word.text == 'here.':
                        setattr(word, attribute, value)
                self.assertFalse(self.cuts(page, box))

    def test_large_line_gap_or_different_alignment_is_insufficient(self):
        for dx, dy in ((20, 0), (0, 20)):
            with self.subTest(dx=dx, dy=dy):
                page, box = self.scene()
                for word in page.words:
                    if word.bbox[1] == 414:
                        x0, top, x1, bottom = word.bbox
                        word.bbox = (x0+dx, top+dy, x1+dx, bottom+dy)
                self.assertFalse(self.cuts(page, box))

    def test_tables_forms_diagrams_and_game_glyphs_cannot_reject_a_frame(self):
        protected_box = (45, 395, 400, 430)
        for kind in ('table', 'form', 'diagram', 'glyph'):
            with self.subTest(kind=kind):
                page, box = self.scene()
                if kind == 'table':
                    page.tables = [TableData(protected_box, [['Preserved table']])]
                elif kind == 'form':
                    page.form_regions = [FormRegion(protected_box, (protected_box,))]
                elif kind == 'diagram':
                    page.diagram_boxes = [protected_box]
                else:
                    for word in page.words:
                        if word.text == 'here.':
                            word.glyphs = (Glyph('O', 'SymbolFont', word.bbox, symbol='opportunity'),)
                self.assertFalse(self.cuts(page, box))


if __name__ == '__main__':
    unittest.main()
