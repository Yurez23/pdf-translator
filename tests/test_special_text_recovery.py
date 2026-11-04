"""Evidence-based recovery of quotations and text attached to diagrams."""

import tempfile
import unittest
from pathlib import Path

from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.export import render
from pdf_translator.models import Word
from pdf_translator.page_data import PageData
from pdf_translator.special_text import recognize_special_text
from tests.test_captions import picture, prose, text

PAGE = (612, 792)
ROOT = Path(__file__).resolve().parents[1]


def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.children)


class SpecialTextTests(unittest.TestCase):
    def convert(self, draw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.pdf"
            canvas = Canvas(str(path), pagesize=PAGE)
            draw(canvas)
            canvas.save()
            return convert_pdf(path, hyphen_resolver=lambda _: False)

    def test_cell_title_is_in_scope_and_cells_keep_their_existing_rendering(self):
        def draw(c):
            picture(c, x=20, top=15, width=270, height=75)
            text(c, ['Ordinary explanatory prose establishes the normal body style.',
                     'The diagram is separate from the surrounding text.',
                     'A third line keeps the body font representative.'], top=500)
            c.setFont('Helvetica-Bold', 12)
            c.drawCentredString(150, PAGE[1]-87, 'Die diagram')
            for i in range(3):
                x = 70+i*60
                for y in (100, 150):
                    c.rect(x, PAGE[1]-y-40, 40, 40, fill=0, stroke=1)
                c.setFont('Helvetica', 20)
                c.drawString(x+10, PAGE[1]-130, str(i+1))
                if i != 1:
                    c.drawString(x+8, PAGE[1]-180, str(i+7))
                path = c.beginPath()
                path.moveTo(x+15, PAGE[1]-142)
                path.lineTo(x+25, PAGE[1]-142)
                path.lineTo(x+20, PAGE[1]-148)
                path.close()
                c.drawPath(path, stroke=0, fill=1)
        result = self.convert(draw)
        scope, = [b for b in result.blocks if b.role == 'cell_diagram']
        self.assertEqual((scope.children[0].text, scope.children[0].role), ('Die diagram', 'aside_title'))
        structure, = [b.diagram for b in scope.children if b.diagram and b.diagram.status == 'correspondence']
        self.assertEqual(len(structure.nodes), 6)
        self.assertEqual(len(structure.connections), 3)
        self.assertIn('**Die diagram**\n\n**(C1) 1**', result.markdown)
        self.assertIn('Die diagram\n\n(C1) 1', result.text)
        self.assertEqual(render([scope]), render(scope.children))
        self.assertEqual(render(list(result.blocks)), (result.markdown, result.text))

    def test_mapping_title_must_be_centered_and_near_the_cells(self):
        from pdf_translator.diagram_geometry import DiagramNode, DiagramStructure
        from pdf_translator.models import SourceRegion
        nodes = tuple(DiagramNode(f'C{i}', SourceRegion(1, (100+(i-1)*30, 200, 125+(i-1)*30, 225)))
                      for i in range(1, 4))
        for bbox in ((100, 180, 125, 190), (130, 150, 155, 160)):
            with self.subTest(bbox=bbox):
                word = Word('Nearby', bbox, 12, 'Bold')
                page = PageData(1, 612, 792, [word], 10,
                                diagram_mappings=[DiagramStructure(nodes=nodes)])
                recognize_special_text(page)
                self.assertFalse(any(h.scope == 'cell_diagram' for h in page.heading_hints))

    def test_display_dialogue_is_a_separate_aside(self):
        quote = '"Wait here," she said, "until dawn." The traveler closed the door.'
        def draw(c):
            prose(c)
            picture(c, x=0, top=390, width=380, height=350)
            c.setFillColor(colors.Color(.4, .5, .6))
            c.setFont('Helvetica', 24)  # no Italic/Oblique in the face name
            for y, line in zip((640, 660, 680),
                               ('"Wait here," she said,', '"until dawn."', 'The traveler closed the door.')):
                c.drawCentredString(190, PAGE[1]-y, line)
        result = self.convert(draw)
        quote_block, = [b for b in result.blocks if b.role == 'quotation']
        self.assertEqual(quote_block.text, quote)
        self.assertIn('> '+quote, result.markdown)
        self.assertIn(quote, result.text)
        self.assertEqual(sum(quote in b.text for b in result.blocks), 1)

    def test_repeated_panel_labels_use_only_explicit_singleton_actualtext(self):
        def draw(c):
            prose(c)
            text(c, ['A right column also has its own independent paragraph.',
                     'The text here precedes the lower illustration.',
                     'A further line establishes this second column.'], x=330, top=110)
            picture(c, x=20, top=300, width=570, height=340)
            for x, top, prefix, tagged in ((80, 340, 'no', True), (350, 440, 'go', False),
                                           (180, 540, 'no', True)):
                c.setFillColor(colors.brown)
                c.rect(x-4, PAGE[1]-top-3, 75, 19, fill=1, stroke=0)
                c.setFillColor(colors.white)
                c.setFont('Helvetica-Bold', 14)
                if tagged:
                    c.addLiteral('/Span <</ActualText <FEFF004E>>> BDC')
                    c.drawString(x, PAGE[1]-top, 'n')
                    c.addLiteral('EMC')
                    c.drawString(x+stringWidth('n', 'Helvetica-Bold', 14), PAGE[1]-top, 'o pass')
                else:
                    # One replacement over multiple glyphs must be ignored.
                    c.addLiteral('/Span <</ActualText <FEFF005A>>> BDC')
                    c.drawString(x, PAGE[1]-top, prefix+' pass')
                    c.addLiteral('EMC')
            # A marked glyph in ordinary body text is intentionally unchanged.
            c.setFillColor(colors.black); c.setFont('Times-Roman', 10)
            c.addLiteral('/Span <</ActualText <FEFF0041>>> BDC')
            c.drawString(50, PAGE[1]-220, 'z')
            c.addLiteral('EMC')
            text(c, ['NORMAL PICTURE CAPTION'], x=360, top=315,
                 font='Helvetica-Bold', size=12, color=colors.blue)
        result = self.convert(draw)
        group, = [b for b in result.blocks if b.role == 'graphic_labels']
        self.assertEqual([b.text for b in group.children], ['No pass', 'go pass', 'No pass'])
        self.assertEqual(len(group.sources), 4)
        self.assertEqual(group.sources[1:], tuple(b.sources[0] for b in group.children))
        self.assertTrue(any(w.code == 'graphic_text' for w in result.warnings))
        for output in (result.markdown, result.text):
            self.assertEqual(output.count('No pass'), 2)
            self.assertIn('visual associations are not reconstructed', output)
            self.assertNotIn('NORMAL PICTURE CAPTION', output)
            self.assertLess(output.index('A right column'), output.index('No pass'))
        self.assertTrue(any(b.text == 'z' for b in result.blocks))

    def test_display_quote_does_not_claim_owned_text(self):
        from pdf_translator.diagram_geometry import DiagramNode, DiagramStructure
        from pdf_translator.models import SourceRegion
        from pdf_translator.page_data import FormRegion, TableData
        values = ['"Speak to them," she said,', '"before the dawn."', 'The captain closed the door.']
        def build():
            words = [Word(value, (100, 620+i*20, 280, 645+i*20), 25, 'Script')
                     for i, value in enumerate(values)]
            return PageData(1, 612, 792, words, 10)
        page = build()
        recognize_special_text(page)
        self.assertEqual(len(page.quotation_regions), 1)
        page = build()
        page.words = page.words[:2]
        recognize_special_text(page)
        self.assertFalse(page.quotation_regions)  # a quoted caption alone is insufficient
        for owner in ('table', 'frame', 'card', 'form', 'attachment', 'mapping'):
            with self.subTest(owner=owner):
                page = build()
                box = (90, 615, 290, 705)
                if owner == 'table':
                    page.tables = [TableData(box, [['A', 'B'], ['1', '2']])]
                elif owner == 'frame':
                    page.frames = [box]
                elif owner == 'card':
                    page.diagram_boxes = [box]
                elif owner == 'form':
                    page.form_regions = [FormRegion(box, ())]
                elif owner == 'attachment':
                    page.form_regions = [FormRegion((310, 615, 500, 705), (), (box,))]
                else:
                    page.diagram_mappings = [DiagramStructure(nodes=(DiagramNode('C1', SourceRegion(1, box)),))]
                recognize_special_text(page)
                self.assertFalse(page.quotation_regions)

    def test_unrelated_styled_captions_do_not_become_a_graphic_group(self):
        def draw(c):
            prose(c)
            picture(c, x=20, top=300, width=570, height=340)
            # No filled label panels and no duplicate source wording.
            text(c, ['FIRST PICTURE'], x=80, top=340, font='Helvetica-Bold', size=14, color=colors.blue)
            text(c, ['SECOND PICTURE'], x=350, top=440, font='Helvetica-Bold', size=14, color=colors.blue)
            text(c, ['THIRD PICTURE'], x=180, top=540, font='Helvetica-Bold', size=14, color=colors.blue)
        result = self.convert(draw)
        self.assertFalse(any(b.role == 'graphic_labels' for b in result.blocks))
        for label in ('FIRST PICTURE', 'SECOND PICTURE', 'THIRD PICTURE'):
            self.assertNotIn(label, result.text)


class OptionalLocalSpecialTextTests(unittest.TestCase):
    def convert(self, name, pages):
        path = ROOT/'example'/name
        if not path.exists():
            self.skipTest('Optional private example PDF absent')
        return convert_pdf(path, pages=pages, hyphen_resolver=lambda _: False)

    def test_introduction_diagram_title_and_correspondences(self):
        result = self.convert('2. Introduction.pdf', '8')
        scope, = [b for b in result.blocks if b.role == 'cell_diagram']
        self.assertEqual(scope.children[0].text, 'Plot Die')
        self.assertEqual(scope.children[0].role, 'aside_title')
        self.assertEqual(len([b for b in result.blocks if b.kind == 'heading' and b.text == 'Plot Die']), 1)
        diagram, = [b.diagram for b in scope.children if b.diagram and b.diagram.status == 'correspondence']
        self.assertEqual((len(diagram.nodes), len(diagram.connections)), (12, 6))
        for output in (result.markdown, result.text):
            self.assertEqual(output.count('C1 -> C7'), 1)
            self.assertLess(output.index('Plot Die', output.index('Plot Die')+1), output.index('(C1) 1'))

    def test_surges_quote_and_cross_page_paragraph(self):
        result = self.convert('8. Chapter 6 Surges.pdf', '1-2')
        quote = '“And now,” Jasnah said, voice hard and grim, “the lesson.” She whipped off her glove.'
        block, = [b for b in result.blocks if b.text == quote]
        self.assertEqual((block.kind, block.role, block.sources[0].page), ('aside', 'quotation', 1))
        for output in (result.markdown, result.text):
            self.assertEqual(output.count(quote), 1)
            self.assertIn('as shown on the Surge Scaling table.', output)
            self.assertLess(output.index('as shown on the Surge Scaling table.'), output.index(quote))
            self.assertLess(output.index(quote), output.index('Surge Size'))

    def test_combat_graphic_labels_and_each_source_region(self):
        result = self.convert('12. Chapter 10 Combat.pdf', '12')
        group, = [b for b in result.blocks if b.role == 'graphic_labels']
        self.assertEqual([b.text for b in group.children],
                         ['No line of effect', 'line of effect', 'No line of effect'])
        self.assertEqual(len(group.sources), 4)
        for source, child in zip(group.sources[1:], group.children):
            self.assertEqual(source, child.sources[0])
            self.assertEqual(source.page, 12)
        for output in (result.markdown, result.text):
            self.assertLess(output.index('when it suits the narrative.'), output.index('No line of effect'))
            self.assertEqual(output.count('No line of effect'), 2)
            self.assertIn('visual associations are not reconstructed', output)
        self.assertTrue(any(w.code == 'graphic_text' and w.pages == (12,) for w in result.warnings))


if __name__ == '__main__':
    unittest.main()
