"""PDF-24: form details require source geometry and unambiguous owners."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from pdf_translator.export import render
from pdf_translator.form_details import (_circle_marks, _closing_parenthesis,
    _defense_groups, _path_digest, _rank_groups, _resource_groups,
    _small_caps, _source_order_word, prepare_form_details)
from pdf_translator.layout import _table_blocks
from pdf_translator.models import Glyph, Word
from pdf_translator.page_data import FormRegion, PageData, TableData
from pdf_translator.table_structure import itemized_columns


def word(text, box, size=10, font="face", glyphs=()):
    return Word(text,box,size,font,glyphs=glyphs)


def circle(x,y, *, fill=False, stroke=True):
    r, k = 2.5, .55228475
    cx,cy=x+r,y+r
    path = [('m',(cx,y)),('c',(cx-k*r,y),(x,cy-k*r),(x,cy)),
            ('c',(x,cy+k*r),(cx-k*r,y+2*r),(cx,y+2*r)),
            ('c',(cx+k*r,y+2*r),(x+2*r,cy+k*r),(x+2*r,cy)),
            ('c',(x+2*r,cy-k*r),(cx+k*r,y),(cx,y))]
    return {'x0':x,'top':y,'x1':x+5,'bottom':y+5,'width':5,'height':5,
            'fill':fill,'stroke':stroke,'non_stroking_color':(0,0,0),
            'stroking_color':(0,0,0),'path':path}


def rank_fixture():
    fields, words, marks = [], [], []
    for col in range(2):
        for row in range(3):
            x,y=10+120*col,20+20*row
            box=(x,y,x+20,y+15)
            fields.append(box)
            blank=(row==2)
            if not blank:
                words.append(word(str(4+row),(x+6,y+3,x+10,y+12)))
            words.append(word('____' if blank else f'Skill{col}{row}',(x+24,y+3,x+45,y+12)))
            for slot in range(4):
                marks.append(((x+65+slot*7,y+5,x+70+slot*7,y+10),slot < row and not blank))
    form=FormRegion((0,0,260,100),tuple(fields))
    page=PageData(1,260,120,words,10,form_regions=[form],form_circles=marks)
    return page,form,words


def table_fixture(width=100):
    rows=[['Label','Items']]
    boxes=[[(0,0,100,18),(100,0,100+width,18)]]
    words=[]
    items=[('Alpha','Bravo','Charlie'),('Delta','Echo','Foxtrot'),('Golf','Hotel','India')]
    for i, names in enumerate(items):
        top=18+i*36
        rows.append([str(i),'\n'.join(names)])
        boxes.append([(0,top,100,top+36),(100,top,100+width,top+36)])
        for j,name in enumerate(names):
            extent = 20 if width < 30 else len(name)*3
            words.append(word(name,(104,top+2+10*j,104+extent,top+10+10*j),8))
    table=TableData((0,0,100+width,126),rows,cell_boxes=boxes)
    return table,words


class FormDetailTests(unittest.TestCase):
    def test_filled_circles_need_matching_outline_shape_and_color(self):
        form=FormRegion((0,0,100,100),())
        hollow=circle(10,20)
        outline=circle(20,20)
        fill=circle(20,20,fill=True,stroke=False)
        foreign={'x0':30,'top':20,'x1':35,'bottom':25,'width':5,'height':5,
                 'fill':True,'stroke':False,'non_stroking_color':(0,0,0),'path':[]}
        states=_circle_marks([hollow,outline,fill,circle(30,20),foreign],[form])
        self.assertEqual([state for _,state in states],[False,True,None])
        self.assertEqual(_circle_marks([fill],[form]),[])

    def test_rank_matrix_keeps_modifier_and_blank_rows_and_source_marks(self):
        page,form,words=rank_fixture()
        groups=_rank_groups(page,form,words)
        self.assertEqual(len(groups),2)
        first=[b.text for b in groups[0][1]]
        self.assertEqual(first,['4 Skill00 [ranks: 0]','5 Skill01 [ranks: 1]','____'])
        glyphs=groups[0][1][1].lines[0].words[-1].glyphs
        self.assertEqual([g.symbol for g in glyphs],['rank_filled','rank_empty','rank_empty','rank_empty'])
        self.assertTrue(all(g.origin=='vector' for g in glyphs))
        self.assertEqual(groups[0][1][1].sources[0].page,1)

    def test_unrelated_dots_and_ambiguous_rank_row_are_not_counted(self):
        page,form,words=rank_fixture()
        page.form_circles[0]=(page.form_circles[0][0],None)
        self.assertEqual(_rank_groups(page,form,words),[])
        page,form,words=rank_fixture()
        self.assertEqual(_rank_groups(page,FormRegion(form.bbox,()),words),[])
        page.form_circles=page.form_circles[:4]
        self.assertEqual(_rank_groups(page,form,words),[])

    def test_resource_ownership_distinguishes_blank_zero_and_ambiguous_crossing(self):
        fields=[]; words=[]; rules=[]
        for i in range(3):
            x=i*100
            box=(x,10,x+80,40); fields.append(box); rules.append((x+40,10,x+40,40))
            words += [word(('HEALTH','FOCUS','POWER')[i],(x+2,11,x+28,17)),
                      word('MAXIMUM',(x+2,18,x+32,23)),word('CURRENT',(x+45,11,x+75,17)),
                      word(('10','5','0')[i],(x+10,27,x+17,35))]
            if i==2:
                words.append(word('0',(x+55,27,x+61,35)))
        form=FormRegion((0,0,300,60),tuple(fields))
        page=PageData(1,300,80,words,10,form_regions=[form],form_dividers=rules)
        result=_resource_groups(page,form,words)
        self.assertEqual([g[1][0].text for g in result],
                         ['HEALTH MAXIMUM 10, CURRENT [blank]',
                          'FOCUS MAXIMUM 5, CURRENT [blank]',
                          'POWER MAXIMUM 0, CURRENT 0'])
        crossing=word('?',(38,25,42,32))
        self.assertNotIn(fields[0],{box for g in _resource_groups(page,form,words+[crossing]) for box in g[3]})
        page.form_ink=[(48,27,53,33)]
        self.assertNotIn(fields[0],{box for g in _resource_groups(page,form,words) for box in g[3]})
        page.form_ink=[(40.6,24,79.4,35)]
        self.assertNotIn(fields[0],{box for g in _resource_groups(page,form,words) for box in g[3]})
        page.form_ink=[(70,25,84,35)]
        self.assertNotIn(fields[0],{box for g in _resource_groups(page,form,words) for box in g[3]})

    def test_panel_sized_diagonal_ink_is_not_a_border(self):
        box=(10,10,90,40)
        points=[(10,10),(90,40),(10,40),(90,10),(10,10)]
        mark={'object_type':'curve','x0':10,'top':10,'x1':90,'bottom':40,
              'width':80,'height':30,'fill':False,'stroke':True,
              'stroking_color':(0,0,0),'pts':points,
              'path':[('m',points[0]),*[('l',p) for p in points[1:]]]}
        rectangle={**mark,'object_type':'rect'}
        outline_with_mark=[(10,10),(90,10),(90,40),(10,40),(10,10),
                           (90,40),(10,40),(90,10),(10,10)]
        combined={**mark,'pts':outline_with_mark,
                  'path':[('m',outline_with_mark[0]),*[('l',p) for p in outline_with_mark[1:]]]}
        form=FormRegion((0,0,100,60),(box,))
        page=PageData(1,100,60,[],10,form_regions=[form])
        prepare_form_details(page,[mark,rectangle,combined],[])
        self.assertEqual(page.form_ink,[box,box])

    def test_defense_values_require_six_fields_and_unique_center_owners(self):
        fields=[]; words=[]
        for col in range(3):
            x=10+110*col
            left=(x,60,x+30,100); right=(x+60,60,x+90,100)
            fields += [left,right]
            words += [word(f'LEFT{col}',(x+2,62,x+22,69)),word(str(col),(x+10,78,x+15,88)),
                      word(f'RIGHT{col}',(x+62,62,x+85,69)),word(str(col+1),(x+70,78,x+75,88)),
                      word(('PHYSICAL','COGNITIVE','SPIRITUAL')[col],(x+43,42,x+57,49)),
                      word('DEFENSE',(x+43,51,x+57,58)),word(str(11+col),(x+47,78,x+53,88))]
        form=FormRegion((0,0,340,120),tuple(fields))
        page=PageData(1,340,120,words,10,form_regions=[form])
        groups=_defense_groups(page,form,words)
        self.assertEqual(len(groups),3)
        self.assertEqual(groups[0][1][0].text,'PHYSICAL DEFENSE 11, LEFT0 0, RIGHT0 1')
        self.assertEqual(_defense_groups(page,form,words+[word('9',(58,78,60,88))]),[])

    def test_explicit_small_caps_only_and_original_mixed_case_prose(self):
        glyphs=tuple(Glyph(c,'face',(i,0,i+1,8),glyph_name=c.lower()+'.sc') for i,c in enumerate('CHaraCter'))
        printed=word('CHaraCter',(0,0,9,8),glyphs=glyphs)
        self.assertEqual(_small_caps(printed).text,'CHARACTER')
        regular=replace(printed,text='MiXeD',glyphs=tuple(Glyph(c,'face',(i,0,i+1,8),glyph_name=c) for i,c in enumerate('MiXeD')))
        self.assertEqual(_small_caps(regular).text,'MiXeD')
        page=PageData(1,100,100,[printed],10,form_regions=[FormRegion((40,40,90,90),())])
        prepare_form_details(page,[],[])
        self.assertEqual(page.words[0].text,'CHaraCter')

    def test_source_order_repair_requires_one_overlapping_pair_one_run(self):
        a=Glyph('a','face',(5,10,7,20),text_run=2,source_order=0)
        b=Glyph('b','face',(4.5,10,7.5,20),text_run=2,source_order=1)
        w=word('ba',(4.5,10,7.5,20),glyphs=(b,a))
        self.assertEqual(_source_order_word(w).text,'ab')
        self.assertEqual(_source_order_word(replace(w,glyphs=(replace(b,text_run=3),a))).text,'ba')
        self.assertEqual(_source_order_word(replace(w,glyphs=(replace(b,bbox=(1,10,2,20)),a))).text,'ba')

    def test_parenthesis_join_requires_source_run_and_overlapping_letters(self):
        glyphs=(Glyph('(','face',(10,0,13,10),text_run=1,source_order=0),
                Glyph('A','face',(12,0,18,10),text_run=1,source_order=1),
                Glyph('b','face',(16,0,20,10),text_run=1,source_order=2))
        left=word('(Ab',(10,0,20,10),glyphs=glyphs)
        right=word(')',(22,0,24,10),glyphs=(Glyph(')','face',(22,0,24,10),text_run=1,source_order=3),))
        self.assertEqual(_closing_parenthesis(left,right).text,'(Ab)')
        self.assertIsNone(_closing_parenthesis(left,replace(right,glyphs=(replace(right.glyphs[0],text_run=2),))))
        self.assertIsNone(_closing_parenthesis(replace(left,glyphs=(glyphs[-1],)),right))

    def test_icons_need_exact_fingerprint_form_and_unambiguous_field(self):
        path=[('m',(10,20))]+[('l',(10+(i%5),20+(i%7))) for i in range(46)]
        shape={'x0':10,'top':20,'x1':15,'bottom':26,'width':5,'height':6,
               'fill':True,'stroke':False,'path':path}
        label=word('Talent',(18,19,45,29))
        field=(5,10,80,50); form=FormRegion((0,0,100,100),(field,))
        with patch.dict('pdf_translator.form_details.FORM_ICON_PATHS',{_path_digest(path):'reaction'}):
            page=PageData(1,100,100,[label],10,form_regions=[form])
            prepare_form_details(page,[shape],[])
            self.assertEqual(page.words[0].text,'[reaction] Talent')
            self.assertEqual(page.words[0].glyphs[0].bbox,(10,20,15,26))
            ambiguous=PageData(1,100,100,[label],10,form_regions=[FormRegion(form.bbox,(field,(6,11,79,49)))])
            prepare_form_details(ambiguous,[shape],[])
            self.assertEqual(ambiguous.words[0].text,'Talent')
        page=PageData(1,100,100,[label],10,form_regions=[form])
        prepare_form_details(page,[shape],[])
        self.assertEqual(page.words[0].text,'Talent')

    def test_repeated_item_lines_keep_boundaries_but_normal_wraps_do_not(self):
        table,words=table_fixture()
        self.assertEqual(itemized_columns(table,words),{1})
        page=PageData(1,300,150,words,8,tables=[table])
        blocks=_table_blocks(page,set(),lambda _:None)
        md,txt=render(blocks)
        self.assertIn('Alpha, Bravo, Charlie',md)
        self.assertIn('Alpha, Bravo, Charlie',txt)
        narrow,words=table_fixture(width=25)
        self.assertEqual(itemized_columns(narrow,words),set())
        page=PageData(1,300,150,words,8,tables=[narrow])
        self.assertIn('Alpha Bravo Charlie',render(_table_blocks(page,set(),lambda _:None))[1])


if __name__ == '__main__':
    unittest.main()
