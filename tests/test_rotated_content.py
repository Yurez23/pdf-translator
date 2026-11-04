"""PDF-08 counterexamples: artwork ownership does not establish irrelevance.

The investigated local case contains three useful Opportunity slots, not a
source-supported removable family. These tests deliberately require retention;
they do not claim to implement a decorative-text classifier.
"""

from collections import Counter
from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

import pdfplumber
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.captions import remove_background_captions, remove_figure_captions
from pdf_translator.export import render
from pdf_translator.layout import layout_page
from pdf_translator.models import Glyph, Word
from pdf_translator.page_data import PageData
from pdf_translator.pdf_reader import _read_page
from pdf_translator.preprocessing import remove_marginal_credits
from tests.test_captions import picture, prose, text


def rotated(c, value, *, angle=90, x=420, y=360, font='Helvetica', size=10):
    c.saveState()
    c.translate(x,y)
    c.rotate(angle)
    c.setFont(font,size)
    c.drawString(0,0,value)
    c.restoreState()


class RotatedContentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source=Path(self.temp.name)/'rotated.pdf'

    def make(self, draw):
        c=Canvas(str(self.source),pagesize=(612,792))
        draw(c)
        c.save()
        return convert_pdf(self.source,hyphen_resolver=lambda _:False)

    def assert_exports(self,result):
        self.assertEqual(render(list(result.blocks)),(result.markdown,result.text))
        raw=[b for b in result.blocks if b.role=='rotated']
        self.assertTrue(raw)
        self.assertTrue(any(w.code=='rotated_text' for w in result.warnings))
        for b in raw:
            self.assertIn(b.text,result.text)
            self.assertEqual(b.kind,'raw')
        return raw

    def test_useful_rotated_label_inside_artwork_is_kept_under_variation(self):
        for angle in (90,270):
            for font,size,x,y in (('Helvetica',8,420,350),('Times-Bold',12,430.001,360.002),
                                  ('Courier-Oblique',10,410,340)):
                with self.subTest(angle=angle,font=font,size=size):
                    def draw(c):
                        prose(c);picture(c)
                        rotated(c,'AXIS 42!',angle=angle,font=font,size=size,x=x,y=y)
                    result=self.make(draw)
                    raw=self.assert_exports(result)
                    self.assertEqual(Counter(''.join(b.text for b in raw).replace(' ','')),Counter('AXIS42!'))
                    self.assertIn('The main paragraph occupies the left column.',result.text)
                    with pdfplumber.open(self.source) as pdf:
                        page=_read_page(pdf.pages[0],lambda _:None)
                    words=[w for w in page.words if not w.upright]
                    self.assertTrue(words)
                    self.assertEqual(Counter(g.text for w in words for g in w.glyphs),Counter('AXIS42!'))
                    self.assertEqual({w.angle for w in words},{angle})
                    self.assertEqual(raw[0].sources[0].page,1)
                    box=raw[0].sources[0].bbox
                    self.assertEqual(box,(min(w.bbox[0] for w in words),min(w.bbox[1] for w in words),
                                          max(w.bbox[2] for w in words),max(w.bbox[3] for w in words)))

    def test_exact_existing_order_and_punctuation_are_not_rewritten(self):
        def draw(c):
            prose(c);picture(c);rotated(c,'KEEP AXIS 42!')
        result=self.make(draw)
        raw=self.assert_exports(result)
        self.assertEqual([b.text for b in raw],['!24 SIXA PEEK'])
        self.assertIn('!24 SIXA PEEK',result.markdown)

    def test_full_page_background_and_overlapping_images_are_not_masks(self):
        def draw(c):
            picture(c,x=0,top=0,width=612,height=792)
            picture(c,x=300,top=240,width=280,height=300)
            c.setFillAlpha(.4)
            picture(c,x=320,top=260,width=240,height=260)
            c.setFillAlpha(1)
            prose(c);rotated(c,'KEEP 73')
        result=self.make(draw)
        self.assertEqual(self.assert_exports(result)[0].text,'37 PEEK')
        self.assertIn('Its words must be retained even near artwork.',result.text)

    def test_multiple_groups_and_multiline_uncertain_tail_are_preserved_whole(self):
        def draw(c):
            prose(c);picture(c)
            rotated(c,'FIRST 12',x=380,y=350)
            rotated(c,'Tail?',x=394,y=350,font='Times-Italic')
            rotated(c,'SECOND 34',x=500,y=200,angle=270)
        result=self.make(draw)
        raw=self.assert_exports(result)
        # The current raw block aggregates independent source groups; deleting
        # it as a unit would erase useful and uncertain content together.
        self.assertEqual(len(raw),1)
        self.assertEqual(Counter(raw[0].text.replace(' ','')),Counter('FIRST12Tail?SECOND34'))

    def test_whole_page_rotation_is_not_evidence_of_decoration(self):
        def draw(c):
            c.setPageRotation(90)
            # ReportLab swaps MediaBox dimensions for /Rotate=90. Keep the
            # source baseline inside that box, not merely extractable off-page.
            text(c,['USEFUL RULE 42!'],top=300)
        result=self.make(draw)
        raw=self.assert_exports(result)
        self.assertEqual(Counter(raw[0].text.replace(' ','')),Counter('USEFULRULE42!'))
        with pdfplumber.open(self.source) as pdf:
            self.assertEqual(pdf.pages[0].rotation,90)

    def test_cancelled_page_and_object_rotation_preserves_normal_text(self):
        def draw(c):
            c.setPageRotation(90)
            rotated(c,'Useful rule 42!',angle=90,x=200,y=300)
        result=self.make(draw)
        self.assertIn('Useful rule 42!',result.text)
        self.assertFalse(any(w.code=='rotated_text' for w in result.warnings))

    def test_nearby_table_sidebar_and_prose_remain(self):
        def draw(c):
            prose(c);picture(c)
            rotated(c,'AXIS 42!')
            for x in (50,150,250):c.line(x,400,x,460)
            for y in (400,430,460):c.line(50,y,250,y)
            c.setFont('Helvetica',10)
            for x,y,value in ((55,440,'Tier'),(155,440,'Value'),(55,410,'1'),(155,410,'42')):
                c.drawString(x,y,value)
            c.setFillGray(.9);c.rect(50,200,230,90,fill=1);c.setFillGray(0)
            text(c,['Sidebar instructions stay here.'],top=520,x=60)
        result=self.make(draw)
        self.assert_exports(result)
        self.assertIn('| Tier | Value |',result.markdown)
        self.assertIn('1\t42',result.text)
        self.assertIn('Sidebar instructions stay here.',result.text)

    def test_selection_does_not_leak_rotated_text_warnings_or_callbacks(self):
        def draw(c):
            prose(c);rotated(c,'UNSELECTED 42!')
            text(c,['An unknown zzq-', 'wvv remains split.'],top=250)
            c.showPage();prose(c)
        self.make(draw)
        callbacks=[]
        result=convert_pdf(self.source,pages='2',hyphen_resolver=lambda issue:callbacks.append(issue) or False)
        self.assertEqual(callbacks,[])
        self.assertFalse(any(w.code=='rotated_text' for w in result.warnings))
        self.assertFalse(any(b.role=='rotated' for b in result.blocks))
        self.assertTrue(all(s.page==2 for b in result.blocks for s in b.sources))
        self.assertTrue(all(set(w.pages)<={2} for w in result.warnings))

    def test_known_and_unknown_symbol_provenance_survives_filters_and_exports(self):
        for symbol,label in (('opportunity','[opportunity]'),('unknown','[unknown symbol]')):
            with self.subTest(symbol=symbol):
                box=(370.,120.,400.,151.)
                word=Word(label,box,31,'Unspecified',False,90,glyphs=(Glyph('O','Unspecified',box,symbol=symbol),))
                words=[Word('Ordinary prose remains useful.',(50,90,270,100),10),word,
                       Word('Adjacent?',(400,120,414,151),10,'Other',False,90)]
                page=PageData(1,612,792,words,10,images=[(0,0,612,792),(310,50,563,235)])
                before=[asdict(w) for w in words]
                remove_marginal_credits(page);remove_figure_captions(page);remove_background_captions([page])
                self.assertEqual([asdict(w) for w in page.words],before)
                warnings=[];blocks=layout_page(page,set(),warnings.append,lambda _:False)
                md,txt=render(blocks)
                self.assertIn(label,txt)
                self.assertIn(label.replace('[',r'\[').replace(']',r'\]'),md)
                self.assertIn('Adjacent?',txt)
                self.assertEqual([asdict(w) for w in page.words],before)
                self.assertTrue(any(w.code=='rotated_text' for w in warnings))


class OptionalAdventuringRotatedTests(unittest.TestCase):
    def test_three_source_confirmed_opportunity_slots_are_useful_and_retained(self):
        source=Path(__file__).resolve().parents[1]/'example/11. Chapter 9 Adventuring.pdf'
        if not source.exists():self.skipTest('Private Adventuring source PDF absent')
        with pdfplumber.open(source) as pdf:
            self.assertEqual(pdf.pages[3].rotation,0)
            page=_read_page(pdf.pages[3],lambda _:None)
        words=[w for w in page.words if not w.upright]
        self.assertEqual([w.text for w in words],['[opportunity]']*3)
        self.assertEqual([w.angle for w in words],[90]*3)
        self.assertEqual([(g.text,g.cid,g.symbol) for w in words for g in w.glyphs],[('O',79,'opportunity')]*3)
        before=[asdict(w) for w in words]
        for selection in (None,'4'):
            with self.subTest(pages=selection):
                result=convert_pdf(source,pages=selection,hyphen_resolver=lambda _:False)
                card=next(b for b in result.blocks if b.children and any(
                    c.text=='Reinforcements Arrive' for c in b.children))
                row=next(c for c in card.children if c.role=='symbol_row')
                self.assertEqual(row.text,'[opportunity] [opportunity] [opportunity]')
                index=card.children.index(row)
                self.assertEqual(card.children[index-1].text,'Triggers when the final Opportunity slot is filled.')
                self.assertEqual(card.children[index+1].text,'Reinforcements Arrive')
                self.assertEqual([s.bbox for s in row.sources[1:]],[w.bbox for w in words])
                self.assertEqual([asdict(w) for w in row.lines[0].words],before)
                self.assertTrue(all(s.page==4 for s in row.sources))
                self.assertEqual(result.text.count('[opportunity]'),3)
                self.assertEqual(result.markdown.count(r'\[opportunity\]'),3)
                self.assertFalse(any(w.code=='rotated_text' for w in result.warnings))
                self.assertFalse(any(b.role=='rotated' for b in result.blocks))
                self.assertIn('Triggers when the final Opportunity slot is filled.',result.text)
                self.assertIn('Reinforcements Arrive',result.text)
                self.assertIn('Contributing to Events',result.text)
                self.assertEqual(render(list(result.blocks)),(result.markdown,result.text))
        self.assertEqual([asdict(w) for w in words],before)
