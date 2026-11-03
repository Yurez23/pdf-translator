"""PDF-11: a wrapped narrative opening is not an isolated image caption."""

import tempfile
import unittest
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.captions import remove_figure_captions
from pdf_translator.page_data import PageData
from tests.test_captions import picture, prose, text
from tests.test_layout_recovery import line


class NarrativeCaptionTests(unittest.TestCase):
    def scene(self, *, font='Times-Italic', scale=1):
        rows = [line('Ordinary prose establishes the main text style.', 100+i*14,
                     font='Times-Roman') for i in range(6)]
        first = line('You adapt your approach to changing circumstances, quietly',
                     300, x=328.5, font=font)
        tail = line('finding another way.', 314, x=330, font=font)
        anchor = [line(value, 342+i*14, x=330, font='Times-Roman')
                  for i, value in enumerate([
                      'The following rules describe the useful effect.',
                      'This ordinary paragraph belongs to the same column.',
                      'Its final sentence completes the instructions.'])]
        caption = line('A TRAVELLER', 550, x=365, font='Courier-Bold', size=12)
        rows += [first, tail, *anchor, caption]
        for row in rows:
            for word in row.words:
                word.color = (0.0, 0.0, 0.0)
                word.bbox = tuple(n*scale for n in word.bbox)
                word.size *= scale
        page = PageData(1, 612*scale, 792*scale,
                        [w for row in rows for w in row.words], 10*scale,
                        images=[tuple(n*scale for n in (320, 290, 610, 700))])
        return page, first, tail, anchor, caption

    def test_opening_wrap_is_kept_at_other_scales_and_italic_faces(self):
        for font in ('Times-Italic', 'Helvetica-Oblique'):
            for scale in (.7, 1, 1.5):
                with self.subTest(font=font, scale=scale):
                    page, first, tail, _, caption = self.scene(font=font, scale=scale)
                    remove_figure_captions(page)
                    self.assertTrue(all(w in page.words for w in first.words+tail.words))
                    self.assertFalse(any(w in page.words for w in caption.words))

    def test_known_caption_style_and_subset_prefixes_do_not_override_narrative_context(self):
        page, first, tail, _, _ = self.scene()
        for word in first.words:
            word.font = 'AAAAAA+'+word.font
        for word in tail.words:
            word.font = 'BBBBBB+'+word.font
        known = {('times-italic', (0.0, 0.0, 0.0))}
        remove_figure_captions(page, known_styles=known)
        remove_figure_captions(page, known_styles=known)
        self.assertTrue(all(w in page.words for w in first.words+tail.words))
        self.assertNotIn(next(iter(known)), page.caption_styles)

    def test_conflicting_or_missing_context_does_not_protect_an_isolated_opening(self):
        for case in ('complete_sentence', 'different_face', 'different_colour', 'large_type',
                     'offset', 'separated', 'no_anchor', 'unknown_colour', 'not_italic',
                     'list', 'protected_continuation'):
            with self.subTest(case=case):
                page, first, tail, anchor, _ = self.scene()
                if case == 'complete_sentence':
                    first.words[-1].text += '.'
                elif case == 'different_face':
                    for word in tail.words:
                        word.font = 'Courier-Oblique'
                elif case == 'different_colour':
                    for word in first.words+tail.words:
                        word.color = (.4, .5, .6)
                elif case == 'large_type':
                    for word in first.words+tail.words:
                        word.size = 13
                elif case in ('offset', 'separated'):
                    dx, dy = (8, 0) if case == 'offset' else (0, -20)
                    for word in first.words:
                        a, b, c, d = word.bbox
                        word.bbox = (a+dx, b+dy, c+dx, d+dy)
                elif case == 'no_anchor':
                    removed = {id(w) for row in anchor for w in row.words}
                    page.words = [w for w in page.words if id(w) not in removed]
                elif case == 'unknown_colour':
                    for word in page.words:
                        word.color = None
                elif case == 'not_italic':
                    for word in first.words+tail.words:
                        word.font = 'Courier'
                elif case == 'list':
                    first.words[0].text = '1.'
                else:
                    page.frames = [(325, 312, 610, 326)]
                remove_figure_captions(page)
                self.assertFalse(any(w in page.words for w in first.words))

    def test_isolated_matching_italic_caption_remains_removable(self):
        page, first, tail, _, _ = self.scene()
        isolated = [line('A traveller crosses the silent valley', 570, x=330, font='Times-Italic'),
                    line('before sunset.', 584, x=330, font='Times-Italic')]
        for row in isolated:
            for word in row.words:
                word.color = (0.0, 0.0, 0.0)
                page.words.append(word)
        remove_figure_captions(page)
        self.assertTrue(all(w in page.words for w in first.words+tail.words))
        self.assertFalse(any(w in page.words for row in isolated for w in row.words))

    def test_protected_opening_does_not_bootstrap_a_longer_chain(self):
        page, first, tail, _, _ = self.scene()
        earlier = line('A detached fragment', 286,
                       x=328.5, font='Times-Italic')
        for word in earlier.words:
            word.color = (0.0, 0.0, 0.0)
            page.words.append(word)
        remove_figure_captions(page)
        self.assertTrue(all(w in page.words for w in first.words+tail.words))
        self.assertFalse(any(w in page.words for w in earlier.words))

    def test_synthetic_pdf_keeps_narrative_separate_and_filters_caption_and_artist(self):
        first = 'You adapt your approach to changing circumstances, quietly'
        tail = 'finding another way.'
        instructions = ['The following rules describe the useful effect.',
                        'This ordinary paragraph belongs to the same column.',
                        'Its final sentence completes the instructions.']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'narrative-wrap.pdf'
            c = Canvas(str(path), pagesize=(612, 792))
            picture(c, x=320, top=290, width=290, height=410)
            prose(c)
            text(c, ['Adaptable Approach'], x=330, top=235, size=14, font='Times-Bold')
            text(c, [first], x=328.5, top=300, font='Times-Italic')
            text(c, [tail], x=330, top=314, font='Times-Italic')
            text(c, instructions, x=330, top=342)
            text(c, ['A TRAVELLER'], x=365, top=550, font='Courier-Bold', size=12)
            c.saveState()
            c.translate(20, 100)
            c.rotate(90)
            c.setFont('Helvetica', 6)
            c.drawString(0, 0, 'TAYLOR MORGAN')
            c.restoreState()
            c.save()
            result = convert_pdf(path, hyphen_resolver=lambda _: False)
        narrative = first+' '+tail
        match = [b for b in result.blocks if b.text == narrative]
        self.assertEqual(len(match), 1)
        self.assertEqual(match[0].kind, 'paragraph')
        self.assertEqual(len(match[0].lines), 2)
        self.assertTrue(any(b.kind == 'paragraph' and b.text == ' '.join(instructions)
                            for b in result.blocks))
        self.assertTrue(any(b.kind == 'heading' and b.text == 'Adaptable Approach'
                            for b in result.blocks))
        for output in (result.markdown, result.text):
            self.assertEqual(output.count(narrative), 1)
            self.assertNotIn('A TRAVELLER', output)
            self.assertNotIn('TAYLOR', output)
            self.assertNotIn('MORGAN', output)


if __name__ == '__main__':
    unittest.main()
