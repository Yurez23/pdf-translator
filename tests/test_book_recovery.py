"""General reproductions from the multi-chapter local conversion audit."""

from pathlib import Path
from types import SimpleNamespace
import unittest

from pdf_translator import convert_pdf
from pdf_translator.continuations import merge_continuations
from pdf_translator.models import Block, SourceRegion, Word
from pdf_translator.page_data import PageData
from pdf_translator.pdf_reader import _repair_table_gaps, _overprinted_spaces
from pdf_translator.preprocessing import integrate_dropcaps
from pdf_translator.diagrams import card_panels
from tests.test_layout_recovery import line, label, recover


class BookRecoveryRulesTests(unittest.TestCase):
    def test_optional_local_surge_index_stays_before_abrasion_section(self):
        import pdfplumber
        from pdf_translator.pdf_reader import _read_page
        from pdf_translator.layout import layout_page
        source=Path(__file__).resolve().parents[1]/'example/8. Chapter 6 Surges.pdf'
        if not source.exists():
            self.skipTest('Local chapter PDF not supplied')
        with pdfplumber.open(source) as pdf:
            page=_read_page(pdf.pages[3],lambda _:None)
        blocks=layout_page(page,set(),lambda _:None,lambda _:False)
        right_title=next(i for i,b in enumerate(blocks) if b.text=='Abrasion'
                         and b.sources[0].bbox[0]>300)
        for name in ('Transformation','Transportation'):
            entries=[i for i,b in enumerate(blocks) if b.text==name and b.sources[0].bbox[0]<300]
            self.assertEqual(len(entries),1)
            self.assertLess(entries[0],right_title)

    def test_large_type_index_is_read_before_opposite_body_column(self):
        from pdf_translator.layout import layout_page
        rows=[]
        for i in range(10):
            name='LongIndexEntryName' if i>=8 else f'Entry{i}'
            rows += [line(name,80+i*60,x=126,size=18,font='Helvetica-Bold'),
                     line('attribute',99+i*60,x=129,size=12)]
        for i in range(34):
            rows.append(line('The body column stays independent.',160+i*14,x=330,size=9.5))
        page=PageData(1,612,792,[w for row in rows for w in row.words],9.5)
        blocks=layout_page(page,set(),lambda _:None)
        first_body=next(i for i,b in enumerate(blocks) if 'body column' in b.text)
        self.assertTrue(all(i<first_body for i,b in enumerate(blocks) if 'LongIndexEntryName' in b.text))
        self.assertEqual(sum(b.text.count('LongIndexEntryName') for b in blocks),2)

    def test_wrapped_italic_label_ends_before_roman_body_without_period(self):
        blocks,_=recover([line('Requirement: Learn the',100,font='Times-Italic'),
            line('first ability',112,font='Times-Italic'),
            line('Spend one resource to use this ability.',126)])
        self.assertEqual(len(blocks),2)
        self.assertEqual(blocks[0].text,'Requirement: Learn the first ability')
        self.assertTrue(blocks[1].text.startswith('Spend one'))

    def test_inline_italic_colon_phrase_does_not_invent_paragraph(self):
        blocks,_=recover([line([('A sentence contains','Helvetica'),
            ('a label: an example','Times-Italic')],100),
            line('and continues in ordinary type.',114)])
        self.assertEqual(len(blocks),1)

    def test_unbulleted_hanging_lead_in_keeps_plain_export(self):
        from pdf_translator.export import render
        blocks,_=recover([label('Unique Talent Tree (Level 1).','Whenever you choose',100),
            line('a new talent, use these rules.',114,x=62),
            label('Change Form (Level 1).','You gain another ability.',128)])
        self.assertEqual(blocks[0].role,'labelled_entry')
        md,txt=render(blocks)
        self.assertFalse(md.startswith('- '))
        self.assertEqual(md,txt)

    def test_multiline_unfinished_column_accepts_clear_sixty_percent_wrap(self):
        first=line('A long paragraph explains all of these rules',100)
        last=line('values added together should roughly',114)
        following=line('match the expected difficulty.',60,x=330)
        a=Block('paragraph',first.text+' '+last.text,(SourceRegion(1,(50,100,285,124)),),
                lines=[first,last],body_size=10,container_left=50,container_width=237)
        b=Block('paragraph',following.text,(SourceRegion(1,following.bbox),),
                lines=[following],body_size=10,container_left=330,container_width=237)
        self.assertIn('roughly match',merge_continuations([a,b],set(),lambda _:None)[0].text)

    def test_single_hanging_wrap_with_bold_peer_is_not_a_new_paragraph(self):
        blocks,_=recover([label('Rival adversaries','have ordinary rules and present',100),
            line('a standard challenge.',114,x=62),
            label('Boss adversaries','have more complex rules.',128)])
        self.assertEqual(len(blocks),2)
        self.assertIn('present a standard challenge.',blocks[0].text)

    def test_enlarged_note_uses_text_anchor_not_outer_frame_edge(self):
        blocks,_=recover([line('A note finishes this sentence.',100,x=150,size=12,font='Times-Italic'),
            line('Another paragraph begins here',116,x=162,size=12,font='Times-Italic'),
            line('and returns to the text edge.',132,x=150,size=12,font='Times-Italic')])
        self.assertEqual(len(blocks),2)

    def test_spurious_space_before_word_punctuation_is_normalized(self):
        from pdf_translator.text import normalize
        self.assertEqual(normalize('Permanent Injury .'),'Permanent Injury.')
        self.assertEqual(normalize('Label : ordinary text'),'Label: ordinary text')
        self.assertEqual(normalize('2 . 5'),'2 . 5')

    def test_mostly_italic_quote_is_not_split_by_one_roman_word(self):
        from pdf_translator.paragraphs import italic
        mixed=line([('Her own','Times-Italic'),('spren','Times-Roman'),
                    ('often made her jump.','Times-Italic')],100,size=12)
        self.assertTrue(italic(mixed))
        blocks,_=recover([line('She thought she could infiltrate them?',84,size=12,font='Times-Italic'),mixed])
        self.assertEqual(len(blocks),1)
        self.assertNotEqual(blocks[0].kind,'heading')

    def test_selected_empty_page_can_be_crossed_but_omitted_page_cannot(self):
        a=line('The paragraph continues with the',700)
        b=line('remaining words on another page.',100)
        def paragraph(row,page):
            return Block('paragraph',row.text,(SourceRegion(page,row.bbox),),lines=[row],
                         body_size=10,container_left=50,container_width=150)
        self.assertEqual(len(merge_continuations([paragraph(a,1),paragraph(b,3)],set(),lambda _:None)),2)
        merged=merge_continuations([paragraph(a,1),paragraph(b,3)],set(),lambda _:None,empty_pages={2})
        self.assertEqual(len(merged),1)
        self.assertIn('the remaining words',merged[0].text)

    def test_repeated_cards_are_not_regular_table_cells(self):
        shapes=[]
        words=[]
        for x in (50,180):
            for y in (100,180,260):
                shapes.append(dict(x0=x,top=y,x1=x+110,bottom=y+65,fill=True,non_stroking_color=(1,1,1)))
                words.append(Word('A titled card with genuine rules text',(x+5,y+5,x+100,y+15),10,'Bold'))
        page=PageData(1,612,792,words,10)
        self.assertEqual(len(card_panels(page,shapes)),6)
        for word in words:
            word.font='Regular'
        self.assertEqual(card_panels(page,shapes),[])

    def test_implicit_hanging_item_does_not_eat_following_paragraph(self):
        blocks,_=recover([label('Find Clues.','You investigate this place and',100),
            line('record the evidence you discover.',114,x=62),
            line('As usual, this is a separate paragraph',128,x=62),
            line('that returns to the column margin.',142)])
        self.assertEqual(len(blocks),2)
        self.assertNotIn('As usual',blocks[0].text)

    def test_explicit_bullet_without_period_does_not_eat_next_paragraph(self):
        blocks,_=recover([line('- A pair of useful tools and',100),
            line('an ordinary bracelet',114,x=62),
            line('Additionally, another paragraph starts',128,x=62),
            line('and returns to the margin.',142)])
        self.assertEqual(len(blocks),2)

    def test_empty_bold_colon_field_is_not_heading_or_part_of_prose(self):
        blocks,_=recover([label('Requirement:','Have another ability from',100),
            line('the same path.',114,x=62),
            line('Usage:',128,font='Helvetica-Bold'),
            line('This is an independent rules paragraph.',142)])
        self.assertEqual([b.kind for b in blocks],['paragraph','paragraph','paragraph'])
        self.assertEqual(blocks[1].text,'Usage:')

    def test_separately_spaced_dialogue_paragraphs_remain_separate(self):
        blocks,_=recover([line('“The first speaker says this.”',100,font='Times-Italic'),
            line('“The second speaker replies.”',116,font='Times-Italic')])
        self.assertEqual(len(blocks),2)

    def test_only_overprinted_not_real_spaces_are_removed(self):
        def char(text,x0,x1):
            return dict(text=text,x0=x0,x1=x1,top=100,fontname='Font',size=10,upright=True)
        before=char('e',50,55)
        overprint=char(' ',55,57)
        after=char('n',55,60)
        real=char(' ',60,63)
        next_word=char('w',63,69)
        self.assertEqual(_overprinted_spaces([before,overprint,after,real,next_word]),{id(overprint)})

    def test_foreign_font_blank_over_initial_glyph_is_not_a_word_boundary(self):
        blank=dict(text=' ',x0=50, x1=59.5,top=100.2,fontname='Symbols',size=10,upright=True)
        initial=dict(text='D',x0=50,x1=56.5,top=100,fontname='Bold',size=10,upright=True)
        real=dict(text=' ',x0=70,x1=73,top=100.2,fontname='Symbols',size=10,upright=True)
        self.assertEqual(_overprinted_spaces([blank,initial,real]),{id(blank)})

    def test_hanging_outcome_after_complete_sentence_is_one_paragraph(self):
        blocks,_=recover([label('If you tie', 'nobody meets their target!',100),
            line('In this case, both of you might fail',114,x=62),
            line('to achieve the objective.',128,x=62)])
        self.assertEqual(len(blocks),1)
        self.assertIn('target! In this case',blocks[0].text)

    def test_wrapped_bold_lead_is_not_a_heading(self):
        blocks,_=recover([line('Bonus Abilities (Level 1, 6, 11, 16, and',100,font='Helvetica-Bold'),
            label('21).','You gain a bonus from this path.',114,x=62),
            line('You can choose another when you advance.',128,x=62)])
        self.assertEqual(len(blocks),1)
        self.assertNotEqual(blocks[0].kind,'heading')
        self.assertIn('and 21). You gain',blocks[0].text)

    def test_display_calculation_does_not_create_outline_levels(self):
        blocks,_=recover([line('Your total equals:',100),
            line('10 + both scores +',120,x=70,font='Helvetica-Bold'),
            line('any other bonuses',134,x=70,font='Helvetica-Bold')])
        self.assertEqual(len(blocks),2)
        self.assertEqual(blocks[1].role,'formula')
        self.assertEqual(blocks[1].text,'10 + both scores + any other bonuses')

    def test_plus_in_real_title_without_formula_context_stays_heading(self):
        blocks,_=recover([line('Equipment + Supplies',100,size=18),line('The ordinary text.',126)])
        self.assertEqual(blocks[0].kind,'heading')

    def test_large_normal_caption_letter_is_not_a_dropcap(self):
        cap=Word('A',(50,100,65,125),25,'Italic')
        following=Word('traveller',(67,100,150,125),25,'Italic')
        page=PageData(1,612,792,[cap,following],10)
        integrate_dropcaps(page)
        self.assertEqual([w.text for w in page.words],['A','traveller'])

    def test_standalone_dropcap_before_capitalized_word_keeps_space(self):
        cap=Word('I',(50,100,80,150),50)
        following=Word('Alex',(82,110,110,120),10)
        page=PageData(1,612,792,[cap,following],10)
        integrate_dropcaps(page)
        self.assertEqual([w.text for w in page.words],['I Alex'])
        self.assertTrue(page.dropcaps)

    def test_grid_hole_recovers_header_without_guessing_its_text(self):
        table=SimpleNamespace(bbox=(50,50,300,150),rows=[
            SimpleNamespace(cells=[(50,50,175,80),(175,50,300,65)]),
            SimpleNamespace(cells=[(50,80,175,150),(175,80,300,150)])])
        rows=[['First',''],['A','B']]
        words=[Word('Missing',(180,68,220,78),10),Word('First',(55,60,90,70),10)]
        self.assertTrue(_repair_table_gaps(table,rows,words))
        self.assertEqual(rows,[['First','Missing'],['A','B']])

    def test_continuation_moves_past_sidebar_but_keeps_sidebar(self):
        a=line('When you appeal to their sense of right and wrong,',700)
        b=line('you test the relevant skill.',210)
        def paragraph(row,page):
            return Block('paragraph',row.text,(SourceRegion(page,row.bbox),),lines=[row],
                         body_size=10,container_left=50,container_width=230)
        first,last=paragraph(a,1),paragraph(b,2)
        aside=Block('aside','An independent note.',(SourceRegion(2,(50,50,290,190)),),
                    children=[Block('paragraph','An independent note.',(SourceRegion(2,(50,50,290,190)),))])
        blocks=merge_continuations([first,aside,last],set(),lambda _:None)
        self.assertEqual(len(blocks),2)
        self.assertIn('wrong, you test',blocks[0].text)
        self.assertIs(blocks[1],aside)


ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless((ROOT/'example/3. Chapter 1 Character Creation.pdf').exists(), 'Local chapter PDFs not supplied')
class LocalBookRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results={}
        for ident in (3,4,5):
            paths=list((ROOT/'example').glob(f'{ident}. *.pdf'))
            if not paths:
                raise unittest.SkipTest('Local chapter PDFs not supplied')
            cls.results[ident]=convert_pdf(paths[0],hyphen_resolver=lambda _:False)

    def test_secondary_font_rules_are_complete(self):
        text=self.results[4].text
        self.assertIn('damage dice increases: at 4 ranks, you roll 2d10 damage',text)
        self.assertIn('at 5 ranks or more, you roll 2d12 damage.',text)

    def test_overlaid_captions_excluded_without_false_outline_levels(self):
        for ident, phrases in [(3,['Three Knights Radiant','search the chasms for']),
                               (5,['Knights Radiant aboard','An Azish Commandant','An expert thief escapes'])]:
            for phrase in phrases:
                self.assertNotIn(phrase,self.results[ident].text)
        self.assertIn('# Character Creation',self.results[3].markdown)

    def test_quote_stays_separate_from_rewards_prose(self):
        blocks=self.results[3].blocks
        quote=next(b for b in blocks if b.text.startswith('He saw it in her eyes.'))
        self.assertEqual(quote.kind,'aside')
        main=next(b for b in blocks if 'you’ll complete' in b.text and b.sources[0].page==13)
        self.assertNotIn('He saw it',main.text)

    def test_table_title_adjacent_and_failure_header_retained(self):
        blocks=self.results[3].blocks
        i=next(i for i,b in enumerate(blocks) if b.text=='Heroic Paths at a Glance')
        self.assertEqual(blocks[i+1].kind,'table')
        table=next(b for b in self.results[5].blocks if b.role=='complex_table' and b.sources[0].page==21)
        self.assertIn('Failure',table.text)

    def test_sidebar_does_not_break_leadership_sentence(self):
        self.assertIn('right and wrong, you test Leadership.',self.results[5].text)

    def test_calculations_not_headings(self):
        for result in self.results.values():
            self.assertFalse(any(b.kind=='heading' and ' + ' in b.text for b in result.blocks))

    def test_overprinted_word_spaces_removed_but_real_words_preserved(self):
        text=self.results[5].text
        for bad in ('Aware ness','Opport unities','Opport unity','mecha nical','exper ience'):
            self.assertNotIn(bad,text)
        for good in ('Awareness','Opportunities','Opportunity','mechanical','experience'):
            self.assertIn(good,text)


if __name__=='__main__':
    unittest.main()
