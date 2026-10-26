"""Heading inference from neutral observations and synthetic PDFs, offline."""

from dataclasses import asdict
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import Block, SourceRegion, convert_pdf
from pdf_translator.export import render
from pdf_translator.heading_context import learn_heading_context
from pdf_translator.headings import assign_heading_levels
from pdf_translator.models import Glyph, Line, Word
from pdf_translator.page_data import PageData
from tests.test_converter import PAGE, lines
from tests.test_headings import prose, section


def heading(text, size=18, *, page=1, y=100, x=50, font='AAAAAA+Serif', color=None, angle=0):
    box = (x,y,x+140,y+size)
    word = Word(text,box,size,font,True,angle,color)
    return Block('heading',text,(SourceRegion(page,box),),lines=[Line([word],box,size)])


def prose_words(*,x=50,y=150,size=10,font='Body'):
    return [Word(text,(x+i*size*5,y+j*size*1.4,x+(i+1)*size*5,y+j*size*1.4+size),size,font)
            for j in range(3) for i,text in enumerate(('Ordinary','text','remains','useful.'))]


class ContextModelTests(unittest.TestCase):
    def banner(self,number,*,scale=1.,dx=0,noise=0):
        a=heading('Topic',21+noise,page=number,y=70,x=100,font='AAAAAA+Display')
        b=heading('Label',14,page=number,y=92,x=100,font='BBBBBB+Subtitle')
        words=[a.lines[0].words[0],b.lines[0].words[0],*prose_words(y=140)]
        for w in words:
            w.bbox=(w.bbox[0]*scale+dx,w.bbox[1]*scale,w.bbox[2]*scale+dx,w.bbox[3]*scale)
            w.size*=scale
        return PageData(number,612*scale,792*scale,words,10*scale,
                        images=[(40*scale+dx,40*scale,300*scale+dx,130*scale)])

    def test_recurring_banner_is_learned_across_scale_position_and_size_noise(self):
        pages=[self.banner(1),self.banner(2,scale=1.5,dx=35,noise=.1)]
        before=[[asdict(w) for w in p.words] for p in pages]
        learn_heading_context(pages)
        for p in pages:
            self.assertEqual([(h.scope,h.role,h.protect) for h in p.heading_hints],
                             [('section','',True),('','title_label',True)])
        self.assertEqual([[asdict(w) for w in p.words] for p in pages],before)

    def test_one_banner_or_pair_without_following_prose_is_not_positive_evidence(self):
        one=self.banner(1);learn_heading_context([one]);self.assertFalse(one.heading_hints)
        pages=[self.banner(1),self.banner(2)]
        for p in pages:p.words=p.words[:2]
        learn_heading_context(pages)
        self.assertTrue(all(not p.heading_hints for p in pages))

    def test_one_body_line_does_not_prove_a_banner_owns_prose(self):
        pages=[self.banner(1),self.banner(2)]
        for p in pages:p.words=p.words[:6]
        learn_heading_context(pages)
        self.assertTrue(all(not p.heading_hints for p in pages))

    def test_prose_gap_is_measured_from_banner_image_edge_too(self):
        pages=[self.banner(1),self.banner(2)]
        for p in pages:
            p.words=p.words[:2]+prose_words(y=185)
            p.images=[(40,40,300,170)]
        learn_heading_context(pages)
        self.assertTrue(all(any(h.protect for h in p.heading_hints) for p in pages))

    def test_same_display_face_elsewhere_is_not_globally_protected(self):
        pages=[self.banner(1),self.banner(2)]
        other=heading('Another purpose',21,y=500,font='CCCCCC+Display')
        pages[0].words.extend(other.lines[0].words)
        learn_heading_context(pages)
        self.assertFalse(any(h.protect and h.bbox==other.sources[0].bbox for h in pages[0].heading_hints))

    def test_tall_sidebar_header_is_auxiliary_without_rewrapping_body(self):
        h=heading('Sidebar header',11,y=55,x=65,font='Sans-Bold')
        page=PageData(1,612,792,[*h.lines[0].words,*prose_words(x=65,y=90)],10,
                      heading_panels=[(55,50,300,75),(55,50,300,710)])
        learn_heading_context([page])
        self.assertEqual([hint.role for hint in page.heading_hints],['aside_title'])
        assign_heading_levels([h],{1},self.fail,pages=[page])
        self.assertEqual((h.kind,h.role,h.outline_level),('heading','aside_title',0))

    def test_highlight_strip_without_enclosing_text_panel_is_not_a_sidebar(self):
        h=heading('Main heading',18,y=55,x=65)
        page=PageData(1,612,792,[*h.lines[0].words,*prose_words(x=65,y=90)],10,
                      heading_panels=[(55,50,300,80)])
        learn_heading_context([page]);self.assertFalse(page.heading_hints)

    def test_multi_title_diagram_and_symbol_legend_do_not_pollute_main_outline(self):
        hs=[heading('Diagram',24,y=90),heading('Subheading',14,y=115),
            heading('Left group',21,y=170),heading('Right group',21,y=170,x=330),
            heading('Legend',11,y=650,font='Sans-Bold')]
        symbols=[Word('*',(60,675+i*13,70,685+i*13),10,'Symbols',
                      glyphs=(Glyph('*','Symbols',(60,675+i*13,70,685+i*13),symbol='[reaction]'),)) for i in range(3)]
        page=PageData(1,612,792,[w for h in hs for w in h.lines[0].words]+symbols,10,
                      diagram_boxes=[(50,215,160,300),(180,215,290,300),
                                     (330,215,440,300),(460,215,570,300)])
        learn_heading_context([page]);assign_heading_levels(hs,{1},self.fail,pages=[page])
        self.assertTrue(all(h.role=='aside_title' and h.level==0 for h in hs))

    def test_prose_between_title_and_cards_preserves_main_heading(self):
        h=heading('Section',18,y=50)
        p=PageData(1,612,792,[*h.lines[0].words,*prose_words(y=90)],10,
                   diagram_boxes=[(50,200,160,300),(180,200,290,300),
                                  (50,320,160,420),(180,320,290,420)])
        learn_heading_context([p]);self.assertFalse(p.heading_hints)

    def test_varied_angles_exclude_graphic_labels_but_a_tilted_heading_survives(self):
        hs=[heading('Label '+str(i),14,y=50+i*45,angle=angle) for i,angle in enumerate((10,25,40,55))]
        tilted=heading('Tilted section',18,y=400,angle=20,font='Other')
        p=PageData(1,612,792,[w for h in [*hs,tilted] for w in h.lines[0].words],10)
        learn_heading_context([p]);assign_heading_levels([*hs,tilted],{1},self.fail,pages=[p])
        self.assertTrue(all(h.role=='graphic_label' for h in hs))
        self.assertEqual((tilted.role,tilted.level),('',1))

    def test_retained_illustration_labels_are_auxiliary_without_deletion(self):
        hs=[heading('A short label',12,y=500,font='Sans-Bold'),
            heading('continues here',12,y=512,font='Sans-Bold')]
        p=PageData(1,612,792,[w for h in hs for w in h.lines[0].words]+prose_words(y=100),10,
                   images=[(40,480,300,700)])
        learn_heading_context([p]);assign_heading_levels(hs,{1},self.fail,pages=[p])
        self.assertEqual([h.role for h in hs],['graphic_label']*2)
        self.assertEqual(render(hs)[1],'A short label\n\ncontinues here\n')

    def test_horizontal_tangent_inside_curved_label_envelope_is_auxiliary(self):
        hs=[heading('Label '+str(i),14,y=50+i*45,angle=angle)
            for i,angle in enumerate((10,25,40,55))]
        tangent=heading('Horizontal fragment',14,y=115)
        outside=heading('Ordinary section',18,y=400)
        p=PageData(1,612,792,[w for h in [*hs,tangent,outside] for w in h.lines[0].words],10)
        before=[asdict(w) for w in p.words]
        learn_heading_context([p])
        assign_heading_levels([*hs,tangent,outside],{1},self.fail,pages=[p])
        self.assertEqual((tangent.role,tangent.outline_level),('graphic_label',0))
        self.assertEqual((outside.role,outside.outline_level),('',1))
        self.assertEqual([asdict(w) for w in p.words],before)

    def test_grouped_graphic_fragments_need_a_hint_for_every_word(self):
        from pdf_translator.page_data import HeadingHint
        a=heading('Left fragment',14,x=50)
        b=heading('Right fragment',14,x=400)
        box=(50,100,540,114)
        combined=Block('heading','Left fragment Right fragment',(SourceRegion(1,box),),
                       lines=[Line([*a.lines[0].words,*b.lines[0].words],box,14)])
        p=PageData(1,612,792,[],10,heading_hints=[HeadingHint(a.sources[0].bbox,'graphic_label')])
        assign_heading_levels([combined],{1},self.fail,pages=[p])
        self.assertEqual(combined.role,'')
        p.heading_hints.append(HeadingHint(b.sources[0].bbox,'graphic_label'))
        assign_heading_levels([combined],{1},self.fail,pages=[p])
        self.assertEqual((combined.role,combined.outline_level),('graphic_label',0))

    def test_learned_single_image_label_needs_local_image_and_no_prose(self):
        seed=[heading('A short label',12,y=500,font='Sans-Bold'),
              heading('continues here',12,y=512,font='Sans-Bold')]
        p=PageData(1,612,792,[w for h in seed for w in h.lines[0].words],10,
                   images=[(40,480,300,700)])
        label=heading('One label',18,page=2,y=300,x=100,font='BBBBBB+Sans-Bold')
        away=heading('Not near an image',18,page=2,y=100,x=100,font='Sans-Bold')
        attached=heading('A prose section',18,page=2,y=500,x=100,font='Sans-Bold')
        q=PageData(2,918,1188,[w for h in (label,away,attached) for w in h.lines[0].words]
                   +prose_words(x=100,y=525,size=15),15,
                   images=[(100,330,240,450),(100,480,240,650)])
        learn_heading_context([p,q])
        assign_heading_levels([label,away,attached],{2},lambda _:None,pages=[q])
        self.assertEqual(label.role,'graphic_label')
        self.assertEqual((away.role,attached.role),('',''))

    def test_one_label_without_document_annotation_evidence_stays_main(self):
        h=heading('One label',12,y=300,font='Sans-Bold')
        p=PageData(1,612,792,h.lines[0].words,10,images=[(50,330,190,500)])
        learn_heading_context([p]);assign_heading_levels([h],{1},self.fail,pages=[p])
        self.assertEqual((h.role,h.level),('',1))


class StructuralModelTests(unittest.TestCase):
    def infer(self, bs, pages=(), selected=(1,)):
        warnings=[]
        before=[asdict(b) for b in bs]
        assign_heading_levels(bs,set(selected),warnings.append,pages=pages)
        for b,old in zip(bs,before):
            now=asdict(b)
            self.assertEqual({k:v for k,v in now.items() if k not in ('level','outline_level','role')},
                             {k:v for k,v in old.items() if k not in ('level','outline_level','role')})
        return warnings

    def test_recurring_bounded_children_create_depth_without_preassigned_levels(self):
        bs=[heading('Root',26)]
        for name in ('A','B','C'):
            bs.extend([heading(name,18),heading(name+' one',14),heading(name+' two',14)])
        self.infer(bs)
        self.assertEqual([b.level for b in bs],[1,2,3,3,2,3,3,2,3,3])

    def test_different_faces_colors_and_numeric_noise_can_be_peers(self):
        bs=[heading('Root',26),heading('A',18,font='AAAAAA+Face',color=(0,)),
            heading('B',18.15,font='BBBBBB+Another',color=(1,0,0))]
        self.infer(bs)
        self.assertEqual([b.level for b in bs],[1,2,2])

    def test_repeated_parent_child_run_can_close_at_an_ancestor(self):
        bs=[heading('Root',26),heading('Group A',18),heading('Item A',14),
            heading('Detail one',12),heading('Detail two',12),heading('Group B',18),
            heading('Item B',14),heading('Item C',14),heading('Group C',18),
            heading('Item D',14),heading('Item E',14),heading('Group D',18)]
        self.assertFalse(self.infer(bs))
        self.assertEqual([b.outline_level for b in bs],[1,2,3,4,4,2,3,3,2,3,3,2])

    def test_unrepeated_chain_is_not_proved_by_a_larger_closing_boundary(self):
        bs=[heading('Root',26),heading('Only parent',18),heading('One style',14),
            heading('Two style',12),heading('Another root',26)]
        self.assertTrue(self.infer(bs))
        self.assertEqual([b.outline_level for b in bs],[1,2,2,2,1])

    def test_final_recurring_parent_can_close_at_an_established_ancestor(self):
        bs=[heading('Root',26),heading('Group A',18),heading('Member A',14),
            heading('Member B',14),heading('Group B',18),heading('Last member',14),
            heading('Detail alpha',12),heading('Detail beta',12),heading('Group C',18)]
        self.assertFalse(self.infer(bs))
        self.assertEqual([b.outline_level for b in bs],[1,2,3,3,2,3,4,4,2])

    def test_final_children_need_a_closed_known_parent_and_one_group(self):
        prefix=lambda:[heading('Root',26),heading('Group A',18),heading('Member A',14),
                       heading('Member B',14),heading('Group B',18)]
        cases=[prefix()+[heading('Last member',14),heading('Detail alpha',12),heading('Detail beta',12)],
               prefix()+[heading('Unique parent',13),heading('Detail alpha',12),heading('Detail beta',12),heading('Group C',18)],
               prefix()+[heading('Last member',14),heading('Detail alpha',12),heading('Detail beta',11),heading('Group C',18)]]
        for bs in cases:
            with self.subTest(text=[b.text for b in bs]):
                self.assertTrue(self.infer(bs))
                self.assertTrue(all(b.outline_level<=3 for b in bs if b.text.startswith('Detail')))

    def test_local_ruled_intervals_do_not_reverse_dominant_document_structure(self):
        bs=[heading('Root',26,y=30)];rules=[]
        for i in range(10):
            bs.append(heading('Main '+str(i),18,y=60+i*60))
            if i not in (2,4,9):
                child=heading('Sub '+str(i),16,y=85+i*60)
                bs.append(child);box=child.sources[0].bbox
                rules.append((box[0],box[3]+2,box[2],box[3]+2))
                bs.append(Block('paragraph','Ordinary source prose follows this heading.',
                                (SourceRegion(1,(50,box[3]+5,250,box[3]+15)),)))
        page=PageData(1,612,792,[],10,heading_rules=rules)
        self.assertFalse(self.infer(bs,pages=[page]))
        self.assertEqual([b.outline_level for b in bs if b.kind=='heading'],
                         [1]+[level for i in range(10) for level in ([2] if i in (2,4,9) else [2,3])])

    def test_repeated_ruled_parent_can_be_smaller_than_display_children(self):
        bs=[];rules=[]
        for i in range(4):
            y=50+i*160
            body=Block('paragraph','An independent introduction.',(SourceRegion(1,(50,y+20,250,y+30)),))
            bs.extend([heading(f'Group {i}',16,y=y),body,heading(f'Child {i} a',24,y=y+40),
                       heading(f'Child {i} b',24,y=y+85)])
            rules.append((50,y+16,250,y+16))
        page=PageData(1,612,792,[],10,heading_rules=rules)
        self.infer(bs,[page])
        self.assertEqual([b.level for b in bs if b.kind=='heading'],[1,2,2]*4)

    def test_numbered_same_style_has_distinguishable_roles_and_missing_parents(self):
        bs=[heading('4.2 First'),heading('4.2.1 Inner'),heading('4.3 Second'),heading('5 Next')]
        self.infer(bs)
        self.assertEqual([b.outline_level for b in bs],[2,3,2,1])

    def test_lone_decimal_and_extra_formats_do_not_create_deep_hierarchy(self):
        bs=[heading('Topic',30),heading('7.5 Units',21),heading('Detail',16),heading('Other',13)]
        warnings=self.infer(bs)
        self.assertEqual([b.level for b in bs],[1,2,2,2])
        self.assertEqual([w.code for w in warnings],['heading_hierarchy'])

    def test_ambiguous_minor_style_stays_with_nearest_established_depth(self):
        bs=[heading('Root',26)]
        for name in ('A','B','C'):
            bs.extend([heading(name,18),heading(name+' one',14),heading(name+' two',14)])
        bs.append(heading('Uncertain detail',12))
        warnings=self.infer(bs)
        self.assertEqual(bs[-1].outline_level,3)
        self.assertEqual([w.code for w in warnings],['heading_hierarchy'])

    def test_same_style_can_have_different_depth_with_optional_intermediate_parent(self):
        bs=[heading('Root',26),heading('Group A',18),heading('Section B',14),
            heading('First item',12),heading('Second item',12),heading('Section C',14),
            heading('Third item',12),heading('Fourth item',12),heading('Group D',18),
            heading('Direct item',12),heading('Another direct item',12),heading('Group E',18)]
        self.infer(bs)
        self.assertEqual([b.outline_level for b in bs],[1,2,3,4,4,3,4,4,2,3,3,2])
        self.assertEqual(bs[3].lines[0].words[0].font,bs[9].lines[0].words[0].font)
        self.assertEqual(bs[3].lines[0].size,bs[9].lines[0].size)

    def test_initial_fragment_keeps_document_calibration_without_inventing_a_parent(self):
        bs=[heading('Initial fragment',12),heading('Root',26,page=2),heading('Group A',18,page=2),
            heading('Section B',14,page=2),heading('First item',12,page=2),heading('Second item',12,page=2),
            heading('Section C',14,page=2),heading('Third item',12,page=2),heading('Fourth item',12,page=2),
            heading('Group D',18,page=2)]
        self.infer(bs,selected=(1,))
        self.assertEqual(bs[0].outline_level,4)

    def test_subtitle_does_not_make_a_distant_heading_a_document_root(self):
        bs=[heading('Topic A',21,y=50,x=100),heading('Descriptor A',14,y=72,x=100),
            heading('Distant section',18,y=400),heading('Topic B',21,page=2,y=50,x=100),
            heading('Descriptor B',14,page=2,y=72,x=100)]
        self.infer(bs,selected=(1,2))
        self.assertEqual([(b.text,b.level) for b in bs if not b.role],
                         [('Topic A',1),('Distant section',2),('Topic B',1)])

    def test_deep_levels_are_exact_in_model_and_valid_markdown(self):
        bs=[heading('.'.join(['2']*i)+' Topic',18,y=i*35) for i in range(1,9)]
        warnings=self.infer(bs)
        self.assertEqual([b.outline_level for b in bs],list(range(1,9)))
        self.assertEqual([b.level for b in bs],[1,2,3,4,5,6,6,6])
        md,txt=render(bs)
        self.assertNotIn('#######',md)
        self.assertEqual(txt,'\n\n'.join(b.text for b in bs)+'\n')
        self.assertEqual([w.code for w in warnings],['heading_depth'])

    def test_unselected_overflow_is_silent_but_still_inferred(self):
        bs=[heading('.'.join(['2']*i)+' Topic',18,page=i) for i in range(1,9)]
        self.assertFalse(self.infer(bs,selected=(1,)))
        self.assertEqual(bs[-1].outline_level,8)

    def test_existing_positional_block_and_render_contract_is_compatible(self):
        b=Block('heading','Old constructor',(SourceRegion(1,(0,0,100,20)),),3,[],[],10,0,'',None,[])
        self.assertEqual(b.outline_level,0)
        self.assertEqual(render([b]),('### Old constructor\n','Old constructor\n'))

    def test_multiple_correlated_kickers_establish_multiple_roots(self):
        bs=[]
        for i in range(2):
            bs.extend([heading('Part',12,page=i+1,y=50,x=200),
                       heading(f'Root {i}',24,page=i+1,y=75,x=200)])
        self.infer(bs,selected=(1,2))
        self.assertEqual([(b.level,b.role) for b in bs],[(0,'title_label'),(1,''),(0,'title_label'),(1,'')])


class StructuralPdfTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'structure.pdf'

    def convert(self,draw,**kwargs):
        c=Canvas(str(self.path),pagesize=PAGE);draw(c);c.save()
        return convert_pdf(self.path,**kwargs)

    @staticmethod
    def main(result):
        return {b.text:b.outline_level for b in result.blocks if b.kind=='heading' and not b.role}

    def test_recurring_parent_children_end_to_end(self):
        def draw(c):
            section(c,'Root',26,70)
            for i in range(3):
                c.showPage()
                section(c,f'Group {i}',18,90)
                section(c,f'First {i}',14,260)
                section(c,f'Second {i}',14,430)
        r=self.convert(draw)
        self.assertEqual(self.main(r),{'Root':1,**{name:depth for i in range(3)
                         for name,depth in [(f'Group {i}',2),(f'First {i}',3),(f'Second {i}',3)]}})
        selected=convert_pdf(self.path,pages='3',hyphen_resolver=lambda _:self.fail('Unexpected callback'))
        self.assertEqual(self.main(selected),{f'Group 1':2,f'First 1':3,f'Second 1':3})
        self.assertTrue(all(set(w.pages)<={3} for w in selected.warnings))
        self.assertTrue(all(s.page==3 for b in selected.blocks for s in b.sources))

    def test_dominant_parent_pattern_survives_local_ruled_inversion_end_to_end(self):
        def draw(c):
            section(c,'Root',26,70)
            for i in range(10):
                c.showPage();section(c,'Main '+str(i),18,80)
                if i not in (2,4,9):
                    section(c,'Sub '+str(i),16,300)
                    c.line(50,PAGE[1]-304,320,PAGE[1]-304)
        r=self.convert(draw)
        for name,level in self.main(r).items():
            self.assertEqual(level,1 if name=='Root' else 2 if name.startswith('Main') else 3)

    def test_child_run_before_ancestor_return_end_to_end(self):
        def draw(c):
            section(c,'Root',26,70);c.showPage()
            section(c,'Group A',18,70);section(c,'Item A',14,230)
            section(c,'Detail alpha',12,390,font='Helvetica-Bold')
            section(c,'Detail beta',12,550,font='Helvetica-Bold')
            for name in ('B','C'):
                c.showPage();section(c,'Group '+name,18,70)
                section(c,'Item '+name+' one',14,260)
                section(c,'Item '+name+' two',14,450)
            c.showPage();section(c,'Group D',18,70)
        r=self.convert(draw)
        self.assertEqual(self.main(r)['Detail alpha'],4)
        self.assertEqual(self.main(r)['Detail beta'],4)
        self.assertFalse(any(w.code=='heading_hierarchy' for w in r.warnings))
        selected=convert_pdf(self.path,pages='2')
        self.assertEqual(self.main(selected),{'Group A':2,'Item A':3,'Detail alpha':4,'Detail beta':4})

    def test_final_child_run_closes_at_known_ancestor_end_to_end(self):
        def draw(c):
            section(c,'Root',26,70);c.showPage()
            section(c,'Group A',18,70)
            section(c,'Member A',14,260);section(c,'Member B',14,450)
            c.showPage();section(c,'Group B',18,70)
            section(c,'Last member',14,230)
            section(c,'Detail alpha',12,390,font='Helvetica-Bold')
            section(c,'Detail beta',12,550,font='Helvetica-Bold')
            c.showPage();section(c,'Group C',18,70)
        r=self.convert(draw)
        self.assertEqual(self.main(r)['Detail alpha'],4)
        self.assertEqual(self.main(r)['Detail beta'],4)
        self.assertFalse(any(w.code=='heading_hierarchy' for w in r.warnings))
        selected=convert_pdf(self.path,pages='3')
        self.assertEqual(self.main(selected),{'Group B':2,'Last member':3,'Detail alpha':4,'Detail beta':4})

    def test_numbering_overrides_size_and_font_without_english_semantics(self):
        def supported(c):
            for title,size,top,font in [('3 Grundlagen',16,80,'Helvetica'),
                                      ('3.1 Abschnitt',26,250,'Times-Bold'),
                                      ('3.1.1 Details',18,420,'Helvetica'),
                                      ('4 Autre',16,590,'Courier-Bold')]:
                section(c,title,size,top,font=font)
        r=self.convert(supported)
        self.assertEqual(list(self.main(r).values()),[1,2,3,1])
        self.assertIn('## 3.1 Abschnitt',r.markdown)

    def test_repeated_banner_titles_are_protected_and_share_primary_section_depth(self):
        image=ImageReader(Image.new('RGB',(300,90),(20,40,80)))
        def draw(c):
            c.setFont('Helvetica-Bold',14);c.drawCentredString(306,742,'Part One')
            c.setFont('Times-Bold',26);c.drawCentredString(306,710,'Collected Topics')
            prose(c,130)
            section(c,'General Guidance',18,260)
            for i in range(2):
                c.showPage();c.drawImage(image,40,PAGE[1]-140,width=300,height=90)
                c.setFillColorRGB(1,1,1)
                c.setFont('Times-Bold',21);c.drawCentredString(190,PAGE[1]-90,f'Topic {i}')
                c.setFont('Helvetica',14);c.drawCentredString(190,PAGE[1]-112,'Reference Part')
                c.setFillColorRGB(0,0,0);prose(c,170)
                section(c,f'First topic {i}',16,300)
                section(c,f'Second topic {i}',16,470)
        r=self.convert(draw)
        self.assertEqual(self.main(r)['Collected Topics'],1)
        for i in range(2):
            self.assertEqual(self.main(r)[f'Topic {i}'],2)
            self.assertEqual(self.main(r)[f'First topic {i}'],3)
            self.assertEqual(r.text.count(f'Topic {i}'),1)
        self.assertEqual(self.main(r)['General Guidance'],2)
        self.assertEqual(r.text.count('Reference Part'),2)
        self.assertEqual([b.role for b in r.blocks if b.text=='Reference Part'],['title_label']*2)

    def test_bold_lead_ins_and_quotations_do_not_create_heading_depth(self):
        def draw(c):
            section(c,'Main title',24,80)
            lines(c,['A bold introduction.'],top=240,font='Helvetica-Bold')
            prose(c,270)
            lines(c,['A literary quotation ends here.'],size=14,top=430,font='Times-Italic')
        r=self.convert(draw)
        self.assertEqual(self.main(r),{'Main title':1})
        self.assertIn('A bold introduction.',r.text)
        self.assertTrue(any(b.role=='quotation' for b in r.blocks))

    def test_real_overflow_selected_alone_keeps_depth_and_warning_page(self):
        def draw(c):
            for i in range(1,9):
                section(c,'.'.join(['5']*i)+f' Node {i}',18,100)
                c.showPage()
        whole=self.convert(draw)
        selected=convert_pdf(self.path,pages='8')
        self.assertEqual(list(self.main(selected).values()),[8])
        self.assertEqual([w.pages for w in selected.warnings if w.code=='heading_depth'],[(8,)])
        self.assertNotIn('#######',whole.markdown)

    def test_diagram_header_chain_is_auxiliary_end_to_end(self):
        from tests.test_diagrams import cards,draw_cards
        def draw(c):
            lines(c,['Main chapter'],size=24,top=70)
            lines(c,['A normal paragraph establishes the ordinary text flow.',
                     'Its useful words and punctuation must all remain.',
                     'A separate diagram occupies the other column.'],top=120,size=10)
            c.setFont('Times-Bold',24);c.drawCentredString(435,PAGE[1]-80,'Diagram title')
            c.setFont('Helvetica',14);c.drawCentredString(435,PAGE[1]-104,'Diagram subtitle')
            c.setFont('Helvetica-Bold',21);c.drawCentredString(435,PAGE[1]-143,'Group label')
            draw_cards(c,cards())
        r=self.convert(draw)
        self.assertEqual(self.main(r),{'Main chapter':1})
        self.assertTrue(any(b.role=='diagram' for b in r.blocks))
        for title in ('Diagram title','Diagram subtitle','Group label'):
            self.assertIn(title,r.text)
            self.assertTrue(next(b for b in r.blocks if b.text==title).role)

    def test_tall_sidebar_is_not_an_extra_heading_level_end_to_end(self):
        def draw(c):
            c.setFillColorRGB(.9,.9,.9);c.rect(50,PAGE[1]-720,250,670,stroke=1,fill=1)
            c.setFillColorRGB(.1,.2,.3);c.rect(50,PAGE[1]-75,250,25,stroke=0,fill=1)
            c.setFillColorRGB(1,1,1);lines(c,['Tall panel title'],x=60,top=68,size=11,font='Helvetica-Bold')
            c.setFillColorRGB(0,0,0)
            lines(c,['A complete paragraph stays inside this panel.',
                     'Another line confirms its ordinary prose content.',
                     'Every useful word remains in the output.'],x=60,top=100,size=10)
            lines(c,['Main section'],x=340,top=90,size=18)
            lines(c,['The main text is outside the side panel.',
                     'Its heading is the only ordinary outline entry.',
                     'Nothing here is a caption to be deleted.'],x=340,top=130,size=10)
        r=self.convert(draw)
        self.assertEqual(self.main(r),{'Main section':1})
        self.assertEqual(next(b.role for b in r.blocks if b.text=='Tall panel title'),'aside_title')
        self.assertIn('Every useful word remains',r.text)

    def test_curved_baselines_are_retained_outside_the_main_outline(self):
        def draw(c):
            section(c,'Main chapter',24,80)
            for index,angle in enumerate((10,25,40,55)):
                c.saveState();c.translate(100+index*100,350)
                c.rotate(angle);c.setFont('Helvetica-Bold',14);c.drawString(0,0,chr(65+index));c.restoreState()
        r=self.convert(draw)
        self.assertEqual(self.main(r),{'Main chapter':1})
        graphic=[b for b in r.blocks if b.role=='graphic_label']
        self.assertEqual(sorted(b.text for b in graphic),list('ABCD'))
        self.assertTrue(all(b.outline_level==0 for b in graphic))

    def test_smaller_ruled_parents_and_larger_children_end_to_end(self):
        def draw(c):
            for i in range(4):
                if i:c.showPage()
                section(c,f'Group {i}',16,80)
                c.line(50,PAGE[1]-84,320,PAGE[1]-84)
                section(c,f'Display {i} alpha',24,270)
                section(c,f'Display {i} beta',24,470)
        r=self.convert(draw)
        self.assertEqual(list(self.main(r).values()),[1,2,2]*4)

    def test_optional_parent_changes_same_style_depth_and_selection_end_to_end(self):
        def draw(c):
            section(c,'Root',26,80);c.showPage()
            section(c,'Group A',18,70)
            for name,top,size in [('Section B',170,14),('First item',270,12),
                                  ('Second item',380,12),('Section C',490,14),
                                  ('Third item',590,12),('Fourth item',690,12)]:
                section(c,name,size,top,font='Helvetica-Bold' if size==12 else 'Helvetica')
            c.showPage();section(c,'Group D',18,70)
            section(c,'Direct item',12,250,font='Helvetica-Bold')
            section(c,'Another direct item',12,430,font='Helvetica-Bold')
            c.showPage();section(c,'Group E',18,80)
        r=self.convert(draw)
        self.assertEqual(self.main(r)['First item'],4)
        self.assertEqual(self.main(r)['Direct item'],3)
        selected=convert_pdf(self.path,pages='3')
        self.assertEqual(self.main(selected),{'Group D':2,'Direct item':3,'Another direct item':3})


class OptionalLocalHierarchyTests(unittest.TestCase):
    def test_final_animal_examples_remain_children_of_stat_block_changes(self):
        p=Path(__file__).resolve().parents[1]/'example/17. Appendix Animal Companions.pdf'
        if not p.is_file():self.skipTest('Local chapter PDF not supplied')
        r=convert_pdf(p,pages='4-5',hyphen_resolver=lambda _:False)
        heads={b.text:b.outline_level for b in r.blocks if b.kind=='heading' and not b.role}
        self.assertEqual(heads['Example Stat Block Changes'],4)
        self.assertEqual([heads[n] for n in ('Axehind','Hunting Chicken (Hawk)','Poison Cremling','Sarpenth')],[5]*4)
        self.assertFalse(any(w.code=='heading_hierarchy' for w in r.warnings))

    def test_gamemaster_main_sections_are_not_children_of_ruled_subsections(self):
        p=Path(__file__).resolve().parents[1]/'example/15. Chapter 13 Gamemastering.pdf'
        if not p.is_file():self.skipTest('Local chapter PDF not supplied')
        r=convert_pdf(p,pages='1-2',hyphen_resolver=lambda _:False)
        main={b.text:b.outline_level for b in r.blocks if b.kind=='heading' and not b.role}
        self.assertEqual({name:main[name] for name in ('Getting Started','The First Step',
                         'Character Creation','Calling for Skill Tests','Using the Plot Die','Raising the Stakes')},
                         {'Getting Started':2,'The First Step':3,'Character Creation':3,
                          'Calling for Skill Tests':2,'Using the Plot Die':2,'Raising the Stakes':3})

    def test_weapon_illustration_labels_are_retained_outside_outline(self):
        p=Path(__file__).resolve().parents[1]/'example/9. Chapter 7 Items.pdf'
        if not p.is_file():self.skipTest('Local chapter PDF not supplied')
        r=convert_pdf(p,pages='6-7',hyphen_resolver=lambda _:False)
        names={'knife','jaVelin','mace','longbow','poleaxe'}
        labels=[b for b in r.blocks if b.text in names]
        self.assertEqual({b.text for b in labels},names)
        self.assertTrue(all(b.role=='graphic_label' and b.outline_level==0 for b in labels))

    def test_same_style_has_source_confirmed_different_depth_in_statistics(self):
        from pdf_translator.headings import _style
        p=Path(__file__).resolve().parents[1]/'example/5. Chapter 3 Character Statistics.pdf'
        if not p.is_file():self.skipTest('Local chapter PDF not supplied')
        r=convert_pdf(p,pages='2,10',hyphen_resolver=lambda _:False)
        heads={b.text:b for b in r.blocks if b.kind=='heading' and not b.role}
        a,b=heads['Lifting Capacity'],heads['Set DC']
        self.assertEqual(_style(a),_style(b))
        self.assertEqual((a.outline_level,b.outline_level),(5,4))
        self.assertEqual((a.sources[0].page,b.sources[0].page),(2,10))

    def test_heroic_paths_source_confirmed_roles_and_depth(self):
        p=Path(__file__).resolve().parents[1]/'example/6. Chapter 4 Heroic Paths.pdf'
        if not p.is_file():self.skipTest('Local chapter PDF not supplied')
        issues=[]
        r=convert_pdf(p,pages='1-3,6,9',hyphen_resolver=lambda i:issues.append(i) or False)
        expected={'Heroic Paths':1,'Using Paths and Talents':2,'Creating Your Hero':3,
                  'Creation Tips':4,'Accessing Other Talents':4,'Talents With Goals':5,
                  'Agent Specialties':3,'Agents of Roshar':3}
        for b in r.blocks:
            if b.kind=='heading' and not b.role and b.text in expected:
                self.assertEqual(b.outline_level,expected.pop(b.text))
        self.assertFalse(expected)
        names=[b for b in r.blocks if b.text=='Agent']
        self.assertEqual([(b.sources[0].page,b.outline_level,b.role) for b in names],[(6,2,''),(9,0,'aside_title')])
        self.assertEqual(next(b.role for b in r.blocks if b.text=='Terms from Later Chapters'),'aside_title')
        self.assertFalse(any(w.code=='heading_depth' for w in r.warnings))
        self.assertTrue(all(i.page in {1,2,3,6,9} for i in issues))
        self.assertTrue(all(set(w.pages)<={1,2,3,6,9} for w in r.warnings))

    def test_radiant_title_selected_alone_keeps_document_depth(self):
        p=Path(__file__).resolve().parents[1]/'example/7. Chapter 5 Radiant Paths.pdf'
        if not p.is_file():self.skipTest('Local chapter PDF not supplied')
        r=convert_pdf(p,pages='10',hyphen_resolver=lambda _:False)
        title=next(b for b in r.blocks if b.text=='Dustbringer')
        self.assertEqual((title.level,title.outline_level,title.role),(2,2,''))
        self.assertEqual(next(b.role for b in r.blocks if b.text=='Radiant Path'),'title_label')
        self.assertTrue(all(s.page==10 for b in r.blocks for s in b.sources))
        self.assertTrue(all(set(w.pages)<={10} for w in r.warnings))

    def test_radiant_curved_labels_have_no_main_headings(self):
        p=Path(__file__).resolve().parents[1]/'example/7. Chapter 5 Radiant Paths.pdf'
        if not p.is_file():self.skipTest('Local chapter PDF not supplied')
        r=convert_pdf(p,pages='9',hyphen_resolver=lambda _:False)
        self.assertFalse([b for b in r.blocks if b.kind=='heading' and not b.role])
        self.assertTrue(any(b.role=='graphic_label' for b in r.blocks))
        self.assertTrue(any(b.kind=='table' for b in r.blocks))


if __name__=='__main__':
    unittest.main()
