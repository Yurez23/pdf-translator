"""Symbol contracts: semantics, extraction, geometry, containers and safe logs.

The local PDF oracle locations were visually checked; they do not infer meaning
from output text. Synthetic PDFs exercise the extraction/export integration.
"""

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

import pdfplumber
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.models import Block, SourceRegion
from pdf_translator.export import render
from pdf_translator.pdf_reader import _read_page
from pdf_translator.pdf_symbols import glyph_page, recover_symbols, glyph_provenance
from pdf_translator.pdf_symbols import encoding_glyphs
from pdfminer.psparser import LIT
from pdf_translator.symbol_geometry import vector_symbols
from pdf_translator.symbol_signatures import PATH_SIGNATURES
from pdf_translator.symbols import LABELS, REGULAR, SMALL_CAPS, identify_glyph
from pdf_translator.telemetry import log_run, emit

ROOT = Path(__file__).resolve().parents[1]


def draw_icon(canvas, identity, x, top, extent=10):
    for part in PATH_SIGNATURES[identity]:
        path = canvas.beginPath()
        for command in part:
            points = [(x+a*extent, 792-top-b*extent) for a, b in command[1:]]
            if command[0] == 'm': path.moveTo(*points[0])
            elif command[0] == 'l': path.lineTo(*points[0])
            elif command[0] == 'c': path.curveTo(*(v for point in points for v in point))
            elif command[0] == 'h': path.close()
        canvas.drawPath(path, fill=1, stroke=0)


def fixture(draw):
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=(612, 792))
    canvas.setFont('Helvetica', 10)
    draw(canvas)
    canvas.showPage(); canvas.save()
    return stream.getvalue()


class SymbolMappingTests(unittest.TestCase):
    def test_cid_unicode_program_and_family_are_all_required(self):
        self.assertEqual(identify_glyph('ABCDEF+CosmereDingbats-Regular', REGULAR, 79, 'O', 'O'), 'opportunity')
        self.assertEqual(identify_glyph('CosmereDingbats-Regular', REGULAR, 67, 'C', 'C'), 'complication')
        self.assertEqual(identify_glyph('Helvetica', REGULAR, 79, 'O', 'O'), '')
        for digest, cid, text in [('different', 79, 'O'), (REGULAR, 79, 'C'), (REGULAR, 88, 'O')]:
            self.assertEqual(identify_glyph('UVWXYZ+CosmereDingbats-Regular', digest, cid, text, 'O'), 'unknown')
        # ToUnicode can still say O while Encoding selects the C outline.
        self.assertEqual(identify_glyph('CosmereDingbats-Regular', REGULAR,79,'O','C'),'unknown')
        self.assertEqual(identify_glyph('CosmereDingbats-Regular', REGULAR,32,' ','infinity'),'unknown')

    def test_conflicting_font_codes_and_verified_blank(self):
        self.assertEqual(identify_glyph('CosmereDingbats-Regular', REGULAR, 31, '\u25b7', 'uni25B7'), 'free_action')
        self.assertEqual(identify_glyph('CosmereDingbats-Regular-SC700', SMALL_CAPS, 31, '\u21bb', 'uni21BB'), 'reaction')
        self.assertEqual(identify_glyph('CosmereDingbats-Regular', SMALL_CAPS, 31, '\u21bb', 'uni21BB'), 'unknown')
        self.assertEqual(identify_glyph('CosmereDingbats-Regular-SC700', SMALL_CAPS, 32, ' ', 'space'), '')
        self.assertEqual(identify_glyph('ZapfDingbats', '', 88, '\u25c6', 'a88'), '')

    def test_encoding_differences_are_separate_from_unicode_and_fail_closed(self):
        base=encoding_glyphs({'Encoding':LIT('WinAnsiEncoding')})
        self.assertEqual(base[79],'O')
        changed=encoding_glyphs({'Encoding':{'BaseEncoding':LIT('WinAnsiEncoding'),
                                           'Differences':[79,LIT('C'),LIT('R')]}})
        self.assertEqual((changed[79],changed[80]),('C','R'))
        for encoding in (LIT('CustomEncoding'),{},
                         {'BaseEncoding':LIT('WinAnsiEncoding'),'Differences':[LIT('C')]},
                         {'BaseEncoding':LIT('WinAnsiEncoding'),'Differences':[256,LIT('C')]}):
            self.assertEqual(encoding_glyphs({'Encoding':encoding}),{})

    def test_mixed_font_chars_unknowns_and_idempotence(self):
        base = dict(x0=10, top=10, x1=16, bottom=20, size=10, upright=True)
        chars = [dict(base, text='O', fontname='Helvetica', source_cid=79),
                 dict(base, x0=16, x1=22, text='O', fontname='ABCDEF+CosmereDingbats-Regular',
                      source_cid=79, font_sha256=REGULAR, encoding_glyph='O'),
                 dict(base, x0=22, x1=28, text='C', fontname='CosmereDingbats-Regular', source_cid=67,
                      font_sha256='unrecognized', encoding_glyph='C')]
        class Page:
            page_number = 7
            curves = []
            def __init__(self, chars): self.chars=chars; self.objects={'char':chars}
            def filter(self, _): return Page(self.chars)
        page=Page(chars); warnings=[]
        result=recover_symbols(page, warnings.append)
        result.chars=result._objects['char']
        self.assertEqual([c['text'] for c in result.chars], ['O','[opportunity]','[unknown symbol]'])
        again=recover_symbols(result, warnings.append)
        self.assertEqual(again._objects['char'],result.chars)
        self.assertEqual((warnings[0].code,warnings[0].pages),('unknown_game_symbol',(7,)))
        self.assertEqual(len(warnings),1)
        glyphs=[glyph_provenance(c) for c in result.chars]
        self.assertEqual([g.text for g in glyphs],['O','O','C'])
        self.assertEqual([g.font for g in glyphs],[c['fontname'] for c in chars])
        self.assertEqual(chars[1]['text'],'O','Input chars must not mutate')

    def test_warning_telemetry_excludes_text_and_font_payloads(self):
        sink=SimpleNamespace(records=[])
        sink.write=sink.records.append
        with log_run(sink):
            emit('conversion.warning','WARNING',code='unknown_game_symbol',pages=(7,),
                 text='PRIVATE_DOCUMENT',font='PRIVATE_FONT',raw_error='PRIVATE_ERROR')
        event=next(r for r in sink.records if r['event']=='conversion.warning')
        self.assertEqual((event['code'],event['pages']),('unknown_game_symbol',[7]))
        self.assertNotIn('PRIVATE',json.dumps(sink.records))


class SymbolExtractionTests(unittest.TestCase):
    def convert(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'symbols.pdf';path.write_bytes(payload)
            return convert_pdf(path,hyphen_resolver=lambda _:False)

    def test_baseline_placement_punctuation_counts_and_repeated_icons(self):
        def draw(c):
            c.drawString(60,692,'Use (')
            draw_icon(c,'action_2',84,93,8)
            c.drawString(93,692,'), then')
            draw_icon(c,'reaction',130,93,8)
            draw_icon(c,'reaction',140,93,8)
            c.drawString(150,692,'; keep 42.')
            c.saveState();c.translate(570,700);c.rotate(90);c.drawString(0,0,'ARTIST');c.restoreState()
        result=self.convert(fixture(draw))
        self.assertIn('Use ([2 actions]), then [reaction] [reaction] ; keep 42.',result.text)
        self.assertEqual(result.text.count('[reaction]'),2)
        self.assertIn(r'Use (\[2 actions\]),',result.markdown)
        self.assertNotIn('[unknown symbol]',result.text)

    def test_supported_vector_symbols_in_table_sidebar_and_labelled_field(self):
        def draw(c):
            c.setFont('Helvetica-Bold',10)
            c.drawString(60,732,'Activation:')
            draw_icon(c,'special_activation',119,53,8)
            c.setFont('Helvetica-Oblique',10)
            c.drawString(60,719,'Narrative remains a separate paragraph.')
            c.setFont('Helvetica',10)
            c.rect(60,510,240,150,fill=0,stroke=1)
            for y in (560,610):c.line(60,y,300,y)
            c.line(180,510,180,660)
            for text,x,y in [('Type',66,640),('Cost',186,640),('Guard',66,590),('Run',66,540)]:
                c.drawString(x,y,text)
            draw_icon(c,'reaction',186,195,8)
            draw_icon(c,'action_3',186,245,10)
            c.setFillColorRGB(.9,.9,.9);c.rect(330,490,220,180,fill=1,stroke=0);c.setFillColorRGB(0,0,0)
            c.drawString(340,650,'Sidebar with ordinary text and a symbol:')
            draw_icon(c,'opportunity',340,155,9)
            c.drawString(352,630,'remains available, exactly once.')
        result=self.convert(fixture(draw))
        table=next(b for b in result.blocks if b.kind=='table')
        field=next(b for b in result.blocks if b.text.startswith('Activation:'))
        self.assertEqual(field.kind,'paragraph','Adding a recovered sign must not turn a field into a heading')
        self.assertEqual(field.text,'Activation: [special activation]')
        self.assertTrue(any(b.text=='Narrative remains a separate paragraph.' for b in result.blocks))
        self.assertEqual(table.rows,[['Type','Cost'],['Guard','[reaction]'],['Run','[3 actions]']])
        for label in ('[special activation]','[reaction]','[3 actions]','[opportunity]'):
            self.assertEqual(result.text.count(label),1)
            self.assertEqual(result.markdown.count(label.replace('[',r'\[').replace(']',r'\]')),1)

    def test_ordinary_letters_bullets_stars_arrows_and_art_do_not_decode(self):
        def draw(c):
            c.drawString(60,692,'Ordinary O C R 2 * > + remain.')
            # A generic triangle of different aspect, ordinary star and arrow.
            p=c.beginPath();p.moveTo(60,650);p.lineTo(65,645);p.lineTo(60,640);p.close();c.drawPath(p,fill=1,stroke=0)
            c.drawString(70,642,'A geometric marker.')
            p=c.beginPath();p.moveTo(200,650);p.lineTo(210,645);p.lineTo(200,640);p.lineTo(203,645);p.close();c.drawPath(p,fill=1,stroke=0)
            c.drawString(215,642,'Arrow beside prose.')
            c.drawString(70,600,'A small ordinary star stays decorative.')
            p=c.beginPath()
            for i,(x,y) in enumerate(((60,608),(61,604),(65,604),(62,601),(63,597),
                                      (60,600),(57,597),(58,601),(55,604),(59,604))):
                (p.moveTo if i==0 else p.lineTo)(x,y)
            p.close();c.drawPath(p,fill=1,stroke=0)
            c.drawString(60,560,'* Keep the genuine list marker.')
            draw_icon(c,'special_activation',400,300,60)  # Large decorative use.
            draw_icon(c,'reaction',400,500,10)  # No compatible text baseline.
        result=self.convert(fixture(draw))
        self.assertIn('Ordinary O C R 2 * > + remain.',result.text)
        self.assertIn('- Keep the genuine list marker.',result.markdown)
        self.assertFalse(any(label in result.text for label in LABELS.values()))

    def test_partial_distinctive_outline_warns_without_inventing_a_meaning(self):
        payload=fixture(lambda c:(draw_icon(c,'opportunity',60,93,10),c.drawString(72,692,'can be spent.')))
        with pdfplumber.open(BytesIO(payload)) as pdf:
            page=glyph_page(pdf.pages[0])
            filtered=page.filter(lambda o:o.get('object_type')!='curve' or o['width']>9)
            warnings=[]; result=recover_symbols(filtered,warnings.append)
            symbols=[c for c in result.chars if c.get('symbol')]
            self.assertEqual([c['text'] for c in symbols],['[unknown symbol]'])
            self.assertEqual(warnings[0].pages,(1,))

    def test_glyph_provenance_survives_word_aggregation_and_regular_pdf_cids(self):
        payload=fixture(lambda c:(c.drawString(60,692,'Before ('),draw_icon(c,'reaction',96,93,8),c.drawString(105,692,').')))
        with pdfplumber.open(BytesIO(payload)) as pdf:
            data=_read_page(pdf.pages[0],lambda _:None)
        word=next(w for w in data.words if '[reaction]' in w.text)
        self.assertTrue(any(g.origin=='vector' and g.symbol=='reaction' for g in word.glyphs))
        self.assertTrue(any(g.cid is not None and not g.symbol for g in word.glyphs))
        self.assertLess(word.source_length,len(word.text))

    def test_a_text_glyph_and_its_overlapping_outline_are_inserted_once(self):
        payload=fixture(lambda c:(draw_icon(c,'opportunity',60,93,10),c.drawString(72,692,'remains.')))
        with pdfplumber.open(BytesIO(payload)) as pdf:
            page=glyph_page(pdf.pages[0])
            anchor=page.chars[0]
            glyph=dict(anchor,text='O',fontname='ABCDEF+CosmereDingbats-Regular',source_cid=79,
                       font_sha256=REGULAR,encoding_glyph='O',x0=60,x1=70,width=10)
            copy=page.filter(lambda _:True)
            copy._objects={**page.objects,'char':[glyph,*page.chars]}
            recovered=recover_symbols(copy,lambda _:None)
            self.assertEqual(sum(c.get('symbol')=='opportunity' for c in recovered.chars),1)

    def test_md_txt_share_labels_in_nested_diagram_and_list(self):
        source=(SourceRegion(1,(0,0,100,100)),)
        children=[Block('heading','[reaction] Card',source,role='aside_title'),
                  Block('list_item','\u25c6 Spend [2 actions], then [free action].',source)]
        md,txt=render([Block('aside','unused flat text',source,children=children,role='diagram_node')])
        self.assertEqual(txt.count('[reaction]'),1)
        self.assertEqual(md.count(r'\[reaction\]'),1)
        self.assertIn('Spend [2 actions], then [free action].',txt)
        self.assertNotIn('unused',md+txt)

    def test_leading_symbol_does_not_split_an_aligned_two_line_title(self):
        def draw(c):
            c.setFont('Helvetica-Bold',10)
            draw_icon(c,'always_active',60,93,11)
            c.drawString(75,692,'Wrapped')
            c.drawString(75,680,'Title')
            c.setFont('Helvetica',10)
            c.drawString(60,660,'Ordinary body prose follows the title.')
        result=self.convert(fixture(draw))
        self.assertTrue(any(b.kind=='heading' and b.text=='[always active] Wrapped Title'
                            for b in result.blocks))

    def test_caption_filter_keeps_a_symbol_field_and_removes_an_ordinary_caption(self):
        from tests.test_captions import picture, prose, text, label
        def draw(c):
            picture(c);prose(c)
            text(c,['Activation:'],x=365,top=80,font='Helvetica-Bold',size=10)
            x=365+c.stringWidth('Activation:','Helvetica-Bold',10)+2
            draw_icon(c,'special_activation',x,74,8)
            label(c,['PORTRAIT CAPTION'],top=550)
        result=self.convert(fixture(draw))
        self.assertIn('Activation: [special activation]',result.text)
        self.assertNotIn('PORTRAIT CAPTION',result.text)

    def test_caption_filter_keeps_an_explicit_unknown_symbol_line(self):
        from pdf_translator.captions import remove_figure_captions
        from pdf_translator.models import Word,Glyph
        from pdf_translator.page_data import PageData
        body=[Word('Ordinary body prose with enough letters.',(50,100+i*14,260,110+i*14),10,'Times-Roman') for i in range(3)]
        box=(365,70,450,82)
        symbol=Word('[unknown symbol]',box,12,'Helvetica-Bold',glyphs=(Glyph('?', 'CosmereDingbats-Regular',box,symbol='unknown'),))
        page=PageData(1,612,792,body+[symbol],10,images=[(325,110,555,530)])
        remove_figure_captions(page)
        self.assertIn(symbol,page.words)


class OptionalLocalSymbolTests(unittest.TestCase):
    def read(self, filename, number):
        path=ROOT/'example'/filename
        if not path.is_file(): self.skipTest('Optional local PDF not supplied: '+filename)
        with pdfplumber.open(path) as pdf:
            warnings=[];data=_read_page(pdf.pages[number-1],warnings.append)
        return data,warnings

    def test_verified_introduction_legend_and_previously_missing_sentence(self):
        data,_=self.read('2. Introduction.pdf',10)
        self.assertEqual([g.symbol for w in data.words for g in w.glyphs if g.symbol],
                         ['action_1','action_1','action_2','action_3','reaction','free_action'])
        data,_=self.read('2. Introduction.pdf',9)
        text=' '.join(w.text for w in sorted(data.words,key=lambda w:(round(w.bbox[1]),w.bbox[0])))
        self.assertIn('If you roll an [opportunity] or',text)
        self.assertIn('[complication] symbol,',text)
        data,_=self.read('2. Introduction.pdf',8)
        values=[w.text for w in data.words if any(g.symbol=='complication' for g in w.glyphs)]
        self.assertIn('[complication] 2',values)
        self.assertIn('[complication] 4',values)

    def test_verified_radiant_page_67_card_and_activation(self):
        data,warnings=self.read('7. Chapter 5 Radiant Paths.pdf',67)
        self.assertEqual([g.symbol for w in data.words for g in w.glyphs if g.symbol].count('complication'),1)
        self.assertEqual(sum(g.symbol=='special_activation' for w in data.words for g in w.glyphs),5)
        self.assertFalse(any(w.code=='unknown_game_symbol' for w in warnings))

    def test_verified_vertically_stretched_always_active_variant(self):
        data,warnings=self.read('7. Chapter 5 Radiant Paths.pdf',15)
        # Previously omitted sign before Distant Surgebinding, checked visually.
        icons=[g for w in data.words for g in w.glyphs if g.symbol=='always_active'
               and 184 < g.bbox[0] < 186 and 414 < g.bbox[1] < 416]
        self.assertEqual(len(icons),1)
        self.assertFalse(any(w.code=='unknown_game_symbol' for w in warnings))

    def test_verified_condensed_always_active_in_cloth_mastery_card(self):
        data,warnings=self.read('7. Chapter 5 Radiant Paths.pdf',55)
        icons=[g for w in data.words for g in w.glyphs if g.symbol=='always_active'
               and 321 < g.bbox[0] < 323 and 473 < g.bbox[1] < 474]
        self.assertEqual(len(icons),1)
        self.assertFalse(any(w.code=='unknown_game_symbol' for w in warnings))
        # A supported font glyph that r09's caption filter had discarded.
        data,_=self.read('9. Chapter 7 Items.pdf',12)
        self.assertTrue(any(g.symbol=='complication' and abs(g.bbox[0]-456.214)<.001
                            for w in data.words for g in w.glyphs))

    def test_verified_combat_table_costs(self):
        data,_=self.read('12. Chapter 10 Combat.pdf',2)
        table=next(t for t in data.tables if t.rows[0]==['Type','Cost','Type','Cost'])
        self.assertEqual(table.rows[1],['Aid','[reaction]','Interact','[1 action]'])
        self.assertEqual(table.rows[3],['Banter','[free action]','Reactive Strike','[reaction]'])
        self.assertEqual(table.rows[5],['Disengage','[1 action]','Recover','[2 actions]'])


if __name__=='__main__':
    unittest.main()
