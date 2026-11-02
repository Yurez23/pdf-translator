"""PDF-10: an inline emphasis wrap is still prose over an image."""

import tempfile
import unittest
from pathlib import Path

from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.captions import remove_figure_captions
from pdf_translator.page_data import PageData
from tests.test_captions import picture, prose, text
from tests.test_layout_recovery import line


class InlineCaptionContinuationTests(unittest.TestCase):
    def scene(self, *, family='Times', scale=1):
        regular, emphasis = ('Times-Roman', 'Times-Italic') if family == 'Times' else (
            'Helvetica', 'Helvetica-Bold')
        rows = [line('Ordinary prose establishes the main text style.', 100+i*14,
                     font=regular) for i in range(6)]
        rows += [line(value, 300+i*14, x=330, font=regular) for i, value in enumerate([
            'Several resources explain the surrounding world',
            'and provide rules for journeys into distant lands',
            'with additional useful accounts including'])]
        previous = line([('the forthcoming', regular), ('Atlas of Places Distant Roads', emphasis)],
                        342, x=330)
        tail = line([('Companion,', emphasis), ('and', regular), ('Further Travels', emphasis),
                     ('adventure.', regular)], 356, x=330)
        caption = line('A PORTRAIT', 550, x=365, font='Courier-Bold', size=12)
        rows += [previous, tail, caption]
        for row in rows:
            for word in row.words:
                word.color = (0.0, 0.0, 0.0)
                word.bbox = tuple(n*scale for n in word.bbox)
                word.size *= scale
            row.bbox = tuple(n*scale for n in row.bbox)
            row.size *= scale
        page = PageData(1, 612*scale, 792*scale,
                        [w for row in rows for w in row.words], 10*scale,
                        images=[tuple(n*scale for n in (320, 290, 610, 700))])
        return page, previous, tail, caption

    def test_mixed_emphasis_tail_is_preserved_without_retaining_isolated_caption(self):
        for family in ('Times', 'Helvetica'):
            for scale in (.7, 1, 1.5):
                with self.subTest(family=family, scale=scale):
                    page, previous, tail, caption = self.scene(family=family, scale=scale)
                    remove_figure_captions(page)
                    self.assertTrue(all(w in page.words for w in previous.words+tail.words))
                    self.assertFalse(any(w in page.words for w in caption.words))

    def test_wrap_can_end_in_a_wholly_emphasized_line(self):
        page, previous, tail, _ = self.scene()
        for word in tail.words:
            word.font = previous.words[-1].font
        remove_figure_captions(page)
        self.assertTrue(all(w in page.words for w in tail.words))

    def test_document_caption_style_reuse_cannot_remove_the_inline_tail(self):
        page, previous, tail, _ = self.scene()
        # PDF subset prefixes are resource identities, not different faces.
        for word in previous.words:
            word.font = 'AAAAAA+'+word.font
        for word in tail.words:
            word.font = 'BBBBBB+'+word.font
        remove_figure_captions(page, known_styles={('times-italic', (0.0, 0.0, 0.0))})
        self.assertTrue(all(w in page.words for w in tail.words))
        self.assertNotIn(('times-italic', (0.0, 0.0, 0.0)), page.caption_styles)

    def test_weak_or_conflicting_evidence_does_not_retain_caption(self):
        for case in ('complete_sentence', 'colour', 'different_face', 'larger_type',
                     'offset', 'separated', 'no_inline_body', 'no_prose_anchor', 'unknown_colour'):
            with self.subTest(case=case):
                page, previous, tail, _ = self.scene()
                if case == 'complete_sentence':
                    previous.words[-1].text += '.'
                elif case == 'colour':
                    for word in tail.words:
                        word.color = (.4, .5, .6)
                elif case == 'different_face':
                    tail.words[0].font = 'Courier-Oblique'
                elif case == 'larger_type':
                    for word in tail.words:
                        word.size = 13
                elif case in ('offset', 'separated'):
                    dx, dy = (8, 0) if case == 'offset' else (0, 40)
                    for word in tail.words:
                        a,b,c,d = word.bbox
                        word.bbox = (a+dx,b+dy,c+dx,d+dy)
                elif case == 'no_inline_body':
                    for word in previous.words:
                        word.font = previous.words[-1].font
                elif case == 'no_prose_anchor':
                    page.words = [w for w in page.words if not (w.bbox[0] >= 330 and w.bbox[1] < 342)]
                elif case == 'unknown_colour':
                    for word in page.words:
                        word.color = None
                remove_figure_captions(page)
                self.assertFalse(any(w in page.words for w in tail.words))

    def test_full_pdf_keeps_one_paragraph_and_removes_caption_and_artist_credit(self):
        prefix = ['Several resources explain the surrounding world',
                  'and provide rules for journeys into distant lands',
                  'with additional useful accounts including']
        mixed = [[('the forthcoming ', 'Times-Roman'), ('Atlas of Places Distant Roads', 'Times-Italic')],
                 [('Companion, ', 'Times-Italic'), ('and ', 'Times-Roman'),
                  ('Further Travels ', 'Times-Italic'), ('adventure.', 'Times-Roman')]]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'inline-wrap.pdf'
            c = Canvas(str(source), pagesize=(612, 792))
            picture(c, x=320, top=300, width=280, height=400)
            prose(c)
            text(c, prefix, x=330, top=300)
            for i, parts in enumerate(mixed):
                x = 330
                for value, font in parts:
                    c.setFont(font, 10)
                    c.drawString(x, 792-342-i*14, value)
                    x += stringWidth(value, font, 10)
            text(c, ['A PORTRAIT'], x=365, top=550, font='Courier-Bold', size=12)
            c.saveState()
            c.translate(20, 100)
            c.rotate(90)
            c.setFont('Helvetica', 6)
            c.drawString(0, 0, 'TAYLOR MORGAN')
            c.restoreState()
            c.save()
            result = convert_pdf(source, hyphen_resolver=lambda _: False)
        expected = ' '.join(prefix+[''.join(value for value, _ in parts) for parts in mixed])
        paragraphs = [b for b in result.blocks if b.text == expected]
        self.assertEqual(len(paragraphs), 1)
        self.assertEqual(paragraphs[0].kind, 'paragraph')
        self.assertEqual(len(paragraphs[0].lines), 5)
        for output in (result.markdown, result.text):
            self.assertEqual(output.count(expected), 1)
            self.assertNotIn('A PORTRAIT', output)
            self.assertNotIn('TAYLOR', output)
            self.assertNotIn('MORGAN', output)


if __name__ == '__main__':
    unittest.main()
