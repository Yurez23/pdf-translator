"""PDF-04: composited art is evidence, never a mask for overlaid text."""

from pathlib import Path
import tempfile
import unittest

from reportlab.lib import colors
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.captions import remove_background_captions
from pdf_translator.models import Glyph, Word
from pdf_translator.page_data import FormRegion, PageData, TableData
from tests.test_captions import picture, prose, text

LABEL = ['Travellers cross', 'the quiet valley', 'before sunset.']
INK = colors.Color(.4, .5, .6)


def caption(c, values=LABEL, *, center=185, top=400, font='Helvetica', size=25,
            leading=20, color=INK):
    c.setFillColor(color)
    c.setFont(font, size)
    for i, value in enumerate(values):
        c.drawCentredString(center, 792-top-i*leading, value)


def background(c):
    picture(c, x=0, top=0, width=612, height=792)


class BackgroundCaptionTests(unittest.TestCase):
    def convert(self, draw, *, pages=None, resolver=None, scale=1):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'background.pdf'
            c=Canvas(str(path), pagesize=(612*scale,792*scale))
            c.scale(scale,scale)
            draw(c)
            c.save()
            return convert_pdf(path,pages=pages,hyphen_resolver=resolver)

    def assert_preserved(self, result, values):
        for output in (result.markdown,result.text):
            for value in values:
                self.assertIn(value,output)

    def test_three_line_caption_over_only_background_is_removed_before_headings(self):
        def draw(c):
            background(c);prose(c);caption(c)
            text(c,['Actual chapter title'],top=70,font='Times-Bold',size=22)
        r=self.convert(draw)
        for value in LABEL:
            self.assertNotIn(value,r.markdown)
            self.assertNotIn(value,r.text)
            self.assertFalse(any(value in b.text for b in r.blocks))
        self.assert_preserved(r,['The main paragraph occupies the left column.','Actual chapter title'])
        self.assertEqual(next(b.level for b in r.blocks if b.text=='Actual chapter title'),1)

    def test_wording_font_scale_position_and_geometry_variants(self):
        for scale in (.75,1.4):
            for font in ('Courier','Times-Roman','Helvetica'):
                with self.subTest(scale=scale,font=font):
                    def draw(c):
                        background(c)
                        if font=='Times-Roman':
                            text(c,['The main paragraph occupies the left column.',
                                    'Its words must be retained even near artwork.',
                                    'Another line establishes the ordinary text style.'],font='Helvetica')
                        else:prose(c)
                        caption(c,['A distant mountain','above a small town.'],center=425,
                                top=550,font=font,size=22,leading=19)
                    r=self.convert(draw,scale=scale)
                    self.assertNotIn('A distant mountain',r.text)
                    self.assertNotIn('above a small town.',r.markdown)
                    self.assertIn('ordinary text style.',r.text)

    def test_four_line_complete_caption_is_removed_whole(self):
        values=['A traveller arrives','with a companion','at the old gates','before the sunset.']
        def draw(c):background(c);prose(c);caption(c,values)
        r=self.convert(draw)
        for output in (r.text,r.markdown):
            for value in values:self.assertNotIn(value,output)

    def test_uncertain_runs_are_kept_whole(self):
        cases=[LABEL+['An unfinished final thought'],
               LABEL+['A fourth line','and a fifth line.'],
               ['Short first line',' '.join(['uncertain']*18),'A final line.'],
               ['No complete','sentence here'],
               ['A'*80,'B'*80,'A final line.']]
        for values in cases:
            with self.subTest(values=values):
                def draw(c):background(c);prose(c);caption(c,values)
                self.assert_preserved(self.convert(draw),values)

    def test_unaligned_run_is_not_partially_removed(self):
        def draw(c):
            background(c);prose(c);caption(c,LABEL[:2])
            caption(c,LABEL[2:],center=200,top=440)
        self.assert_preserved(self.convert(draw),LABEL)

    def test_changed_size_or_face_in_tail_vetoes_partial_removal(self):
        for font,size in [('Helvetica',26),('Courier',25)]:
            with self.subTest(font=font,size=size):
                def draw(c):
                    background(c);prose(c)
                    caption(c,['Travellers cross','the quiet valley.'])
                    caption(c,['A nearby uncertain tail.'],top=440,font=font,size=size)
                self.assert_preserved(self.convert(draw),['Travellers cross','the quiet valley.',
                                                         'A nearby uncertain tail.'])

    def test_background_or_style_signal_alone_is_insufficient(self):
        for mode in ('no_image','single','body_colour','body_font','ordinary_size','no_prose'):
            with self.subTest(mode=mode):
                def draw(c):
                    if mode!='no_image':background(c)
                    if mode!='no_prose':prose(c)
                    caption(c, LABEL[:1] if mode=='single' else LABEL,
                            color=colors.black if mode=='body_colour' else INK,
                            font='Times-Roman' if mode=='body_font' else 'Helvetica',
                            size=12 if mode=='ordinary_size' else 25)
                self.assert_preserved(self.convert(draw),LABEL[:1] if mode=='single' else LABEL)

    def test_sparse_page_reuses_strong_document_style_in_either_page_order(self):
        for reverse in (False,True):
            with self.subTest(reverse=reverse):
                def draw(c):
                    for sparse in ((True,False) if reverse else (False,True)):
                        background(c)
                        if not sparse:prose(c)
                        caption(c)
                        text(c,['Useful page title'],x=330,top=70,font='Times-Bold',size=18)
                        c.showPage()
                r=self.convert(draw)
                self.assertEqual(r.text.count('Useful page title'),2)
                for value in LABEL:self.assertNotIn(value,r.text)

    def test_learning_from_ordinary_image_still_needs_background_group_evidence(self):
        def draw(c):
            picture(c);prose(c);caption(c,center=430,top=350)
            c.showPage();background(c);caption(c)
            text(c,['Real isolated heading'],x=50,top=70,font='Helvetica',size=25,color=INK)
        r=self.convert(draw)
        for value in LABEL:self.assertNotIn(value,r.text)
        self.assertIn('Real isolated heading',r.text)

    def test_document_content_style_vetoes_even_a_compact_label(self):
        for kind in ('prose','heading'):
            with self.subTest(kind=kind):
                def draw(c):
                    background(c);prose(c);caption(c)
                    c.showPage();prose(c)
                    if kind=='prose':
                        text(c,['A secondary face is used for genuine running prose.',
                                'Its ordinary paragraph must never teach a caption style.',
                                'These useful instructions remain fully available.'],
                             x=330,top=300,font='Helvetica',color=INK)
                    else:
                        text(c,['Genuine section heading'],x=50,top=260,font='Helvetica',size=25,color=INK)
                        text(c,['The following prose belongs to this section.',
                                'It must protect the shared heading style.'],top=285)
                self.assert_preserved(self.convert(draw),LABEL)

    def test_genuine_heading_over_background_owns_nearby_prose(self):
        def draw(c):
            background(c);prose(c);caption(c,['A genuine heading','with a second line.'],center=425)
            text(c,['Useful prose immediately follows the section title.',
                    'This second line continues the useful explanation.',
                    'The last line completes the paragraph.'],x=330,top=445)
        self.assert_preserved(self.convert(draw),['A genuine heading','with a second line.','Useful prose immediately follows'])

    def test_quotations_lists_and_bold_titles_are_not_captions(self):
        for values,font,size in [(LABEL,'Helvetica-Oblique',25),
                                 (['“Travellers cross','the quiet valley.”'],'Helvetica',25),
                                 (['1. Travellers cross','the quiet valley.'],'Helvetica',25),
                                 (LABEL,'Helvetica-Bold',25),
                                 (['A literary passage','in a quieter voice.'],'Times-Italic',15)]:
            with self.subTest(font=font,values=values):
                def draw(c):background(c);prose(c);caption(c,values,font=font,size=size)
                self.assert_preserved(self.convert(draw),values)

    def test_secondary_font_prose_on_background_remains(self):
        values=['A second font carries ordinary rules and instructions.',
                'All values including 2d10 and 2d12 must remain.',
                'This is a real paragraph with enough context to identify it.']
        def draw(c):background(c);prose(c);text(c,values,x=330,top=300,font='Helvetica',color=INK)
        r=self.convert(draw)
        self.assert_preserved(r,values)

    def test_selection_keeps_document_context_without_leaking_output_warnings_or_callbacks(self):
        def draw(c):
            background(c);prose(c);caption(c)
            text(c,['First actual title'],top=60,font='Times-Bold',size=28)
            c.showPage();background(c);caption(c)
            text(c,['Selected title'],x=330,top=60,font='Times-Bold',size=20)
            text(c,['A selected ambig-','uous word remains.'],x=330,top=150)
            c.showPage()
            text(c,['Unselected ambig-','uous word stays out.'],top=110)
            c.showPage()  # a nonselected empty page must not emit empty_page
        all_issues=[];selected_issues=[]
        whole=self.convert(draw,resolver=lambda i:all_issues.append(i) or False)
        selected=self.convert(draw,pages='2',resolver=lambda i:selected_issues.append(i) or False)
        for value in LABEL:self.assertNotIn(value,selected.text)
        self.assertNotIn('First actual title',selected.text)
        self.assertNotIn('Unselected',selected.text)
        self.assertTrue(selected_issues)
        self.assertTrue(all(i.page==2 for i in selected_issues))
        self.assertTrue(all(set(w.pages)<={2} for w in selected.warnings))
        self.assertTrue(all(s.page==2 for b in selected.blocks for s in b.sources))
        a=next(b for b in whole.blocks if b.text=='Selected title')
        b=next(b for b in selected.blocks if b.text=='Selected title')
        self.assertEqual((a.kind,a.level,a.role),(b.kind,b.level,b.role))
        self.assertEqual([(i.page,i.hyphenated) for i in all_issues if i.page==2],
                         [(i.page,i.hyphenated) for i in selected_issues])


class BackgroundProtectionModelTests(unittest.TestCase):
    def page(self):
        body=[Word('Ordinary prose provides reliable evidence for the main text style.',
                   (50,100+i*14,300,110+i*14),10,'Body',color=(0,)) for i in range(3)]
        words=[Word(value,(100+i*20,400+i*20,270-i*20,425+i*20),25,'Display',color=(.4,.5,.6))
               for i,value in enumerate(LABEL)]
        return PageData(1,612,792,body+words,10,images=[(0,0,612,792)]),words

    def test_any_protected_member_vetoes_the_entire_group(self):
        for kind in ('table','table_title','table_note','frame','diagram','form','attachment','symbol'):
            with self.subTest(kind=kind):
                page,words=self.page();box=words[1].bbox
                if kind=='table':page.tables=[TableData(box,[['Field','Value'],['1','2']])]
                elif kind=='table_title':page.tables=[TableData((90,430,280,550),[['Field','Value'],['1','2']])]
                elif kind=='table_note':
                    page.tables=[TableData((90,300,280,395),[['Field','Value'],['1','2']])]
                    words[0].text='* '+words[0].text
                elif kind=='frame':page.frames=[box]
                elif kind=='diagram':page.diagram_boxes=[(90,430,280,550)]
                elif kind=='form':page.form_regions=[FormRegion(box,(box,))]
                elif kind=='attachment':page.form_regions=[FormRegion((350,150,550,500),(),(box,))]
                else:words[1].glyphs=(Glyph('x','Symbol',box,symbol='one_action'),)
                before=list(page.words);remove_background_captions([page])
                self.assertEqual(page.words,before)

    def test_model_group_is_removable_without_protection(self):
        page,words=self.page();remove_background_captions([page])
        self.assertFalse(any(w in page.words for w in words))


class OptionalLocalBackgroundCaptionTests(unittest.TestCase):
    def test_heroic_paths_first_physical_page(self):
        path=Path(__file__).resolve().parents[1]/'example/6. Chapter 4 Heroic Paths.pdf'
        if not path.is_file():self.skipTest('Optional private Heroic Paths PDF absent')
        result=convert_pdf(path,pages='1',hyphen_resolver=lambda _:False)
        for value in ('Adolin Kholin, Alethkar’s','most talented young','warrior.'):
            self.assertFalse(any(b.kind=='heading' and b.text==value for b in result.blocks))
        self.assertNotIn('Adolin Kholin',result.text)
        self.assertNotIn('most talented young',result.markdown)
        for value in ('Heroic Paths','Using Paths and Talents','Creation Tips','help others in need'):
            self.assertIn(value,result.text)
        self.assertTrue(all(s.page==1 for b in result.blocks for s in b.sources))
        self.assertTrue(all(set(w.pages)<={1} for w in result.warnings))


if __name__=='__main__':unittest.main()
