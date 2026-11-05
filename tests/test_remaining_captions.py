"""PDF-25 regressions for narrow artwork and external form captions."""

from dataclasses import replace
import unittest

from pdf_translator.captions import remove_figure_captions
from pdf_translator.models import Glyph, Word
from pdf_translator.page_data import FormRegion, PageData, TableData


class RemainingCaptionTests(unittest.TestCase):
    def test_narrow_artwork_label_is_removed_but_table_and_symbols_survive(self):
        label = Word('SPEAR', (100, 300, 133, 312), 12, 'Bold', color=(.4,.5,.6))
        table_word = replace(label, bbox=(100, 90, 133, 102))
        symbol = replace(label, text='[reaction] Guard', bbox=(200,300,250,312),
                         glyphs=(Glyph('', '', (200,300,210,312), symbol='reaction', origin='vector'),))
        p = PageData(1,612,792,[label,table_word,symbol],10,
                     images=[(105,325,128,500),(200,320,250,510)],
                     tables=[TableData((90,80,150,110),[['SPEAR']])])
        remove_figure_captions(p, known_styles={('bold',(.4,.5,.6))})
        self.assertEqual(p.words,[table_word,symbol])

    def test_form_caption_requires_explicit_display_encoding_and_no_game_symbol(self):
        box = (80,610,500,622)
        glyphs = (Glyph('a','font',box,glyph_name='a.sc'),)
        caption = Word('A completed form.',box,12,'font',color=(.4,.5,.6),glyphs=glyphs)
        for variant, removed in [(caption,True),(replace(caption,glyphs=()),False),
                                 (replace(caption,color=(0,0,0)),False),
                                 (replace(caption,glyphs=(*glyphs,Glyph('', '',box,symbol='reaction'))),False)]:
            with self.subTest(removed=removed,word=variant):
                form = FormRegion((60,60,550,600),(),(box,))
                p = PageData(1,612,792,[variant],10,form_regions=[form])
                remove_figure_captions(p)
                self.assertEqual(p.words, [] if removed else [variant])
                self.assertEqual(form.attachments, () if removed else (box,))
