"""PDF-27 stanza grouping, source label case and rendered pointer lines."""

from dataclasses import replace
import unittest

from markdown_it import MarkdownIt
from pdf_translator.diagram_geometry import DiagramStructure, DiagramConnection
from pdf_translator.export import render
from pdf_translator.layout import _verse_stanzas
from pdf_translator.headings import assign_heading_levels
from pdf_translator.models import Block, Glyph, Line, SourceRegion, Word
from pdf_translator.page_data import PageData
from pdf_translator.text import logical_label


def verse():
    values=['The distant knights must return.', 'The ancient lights have returned.',
            'We fear their coming.', 'A soul is broken.', 'The cracks can grow.',
            'The powers can mend.', 'The powers can destroy.']
    lines=[]
    for i,(text,y) in enumerate(zip(values,[10,29,45,64,80,96,112])):
        words=[Word(w,(100+j*25,y,120+j*25,y+12),12,
                    'Times-Regular' if i==0 else 'Times-Italic',color=(.4,.5,.6))
               for j,w in enumerate(text.split())]
        lines.append(Line(words,(100,y,300,y+12),12))
    source=(SourceRegion(1,(100,10,300,124)),)
    b=Block('aside',' '.join(values),source,role='quotation',lines=lines,
            children=[Block('paragraph',' '.join(values),source)])
    return b,PageData(1,612,792,[],9.5)


class VerseLayoutTests(unittest.TestCase):
    def test_centered_stanzas_keep_source_lines_in_both_formats(self):
        b,p=verse();_verse_stanzas(b,p)
        self.assertEqual([len(c.lines) for c in b.children],[1,2,4])
        md,txt=render([b])
        self.assertIn('returned.\nWe fear',txt)
        self.assertIn('returned.  \n> We fear',md)
        tokens=MarkdownIt().parse(md)
        self.assertEqual(sum(t.type=='paragraph_open' for t in tokens),3)
        self.assertEqual(sum(c.type=='hardbreak' for t in tokens for c in t.children or []),4)

    def test_ordinary_or_uncentered_quote_keeps_existing_grouping(self):
        for case in ('roman','uncentered','uniform'):
            b,p=verse();old=b.children
            if case=='roman':b.lines[2].words=[replace(w,font='Times-Regular') for w in b.lines[2].words]
            elif case=='uncentered':b.lines[-1].bbox=(40,112,240,124)
            else:
                for i,line in enumerate(b.lines[1:]):line.bbox=(100,29+16*i,300,41+16*i)
            _verse_stanzas(b,p)
            self.assertIs(b.children,old)

    def test_correspondences_render_as_distinct_lines(self):
        b=Block('raw','cells',(SourceRegion(1,(0,0,10,10)),),
                diagram=DiagramStructure(connections=(DiagramConnection('C1','C2','correspondence'),),status='correspondence'))
        md,txt=render([b]);tokens=MarkdownIt().parse(md)
        self.assertIn('explicit pointers):  \n> C1',md)
        self.assertEqual(sum(c.type=='hardbreak' for t in tokens for c in t.children or []),1)
        self.assertIn('explicit pointers):\nC1',txt)

    def test_label_honors_actual_text_without_guessing_case(self):
        g=Glyph('c','face',(0,0,10,10),actual_text='C')
        w=Word('chapter',(0,0,60,10),10,glyphs=(g,*[replace(g,text=c,actual_text='') for c in 'hapter']))
        self.assertEqual(logical_label(Line([w],w.bbox,10)),'Chapter')
        self.assertEqual(logical_label(Line([replace(w,glyphs=())],w.bbox,10)),'chapter')
        source=(SourceRegion(1,w.bbox),)
        plain=Block('heading','chapter',source,role='title_label',lines=[Line([w],w.bbox,10)])
        numbered=Block('heading','chapter 7',source,role='title_label',
                       lines=[Line([w,Word('7',(61,0,70,10),10)],(0,0,70,10),10)])
        assign_heading_levels([plain,numbered],{1},self.fail)
        self.assertEqual(plain.text,'chapter')
        self.assertEqual(numbered.text,'Chapter 7')
