"""Field boundaries supported by local typography and list geometry."""

from pathlib import Path
import unittest

import pdfplumber

from pdf_translator import convert_pdf
from pdf_translator.export import render
from pdf_translator.layout import _blocks_for_lines, layout_page
from pdf_translator.models import Glyph, Word, union_bbox
from pdf_translator.page_data import PageData
from pdf_translator.pdf_reader import _read_page
from tests.test_layout_recovery import line


ROOT = Path(__file__).resolve().parents[1]


def recover(rows, *, card=False, bounds=(50, 96, 290, 792)):
    page = PageData(1, 612, 792, [w for row in rows for w in row.words], 10)
    return _blocks_for_lines(rows, page, bounds, set(), lambda _: None, card=card)


def symbol_title(y=100):
    row = line('A Clear Title', y, x=63, font='Helvetica-Bold')
    box = (50, y, 58, y + 10)
    glyph = Glyph('↶', 'Symbols', box, symbol='reaction')
    row.words.insert(0, Word('[reaction]', box, 10, 'Symbols', glyphs=(glyph,)))
    row.bbox = union_bbox([word.bbox for word in row.words])
    return row


class FieldBoundaryTests(unittest.TestCase):
    def test_symbol_title_and_italic_label_are_separate_only_in_card(self):
        rows = [symbol_title(), line('Requirement: Rank 2+', 114, font='Helvetica-Oblique'),
                line('Use this ability.', 128)]
        card = recover(rows, card=True)
        self.assertEqual([b.text for b in card],
                         ['[reaction] A Clear Title', 'Requirement: Rank 2+', 'Use this ability.'])
        self.assertEqual(card[0].kind, 'heading')
        self.assertEqual(card[0].lines[0].words[0].glyphs[0].symbol, 'reaction')
        self.assertEqual([b.text for b in recover(rows)],
                         ['[reaction] A Clear Title Requirement: Rank 2+ Use this ability.'])

    def test_symbol_in_later_card_region_is_not_a_title(self):
        rows = [symbol_title(y=150), line('ordinary continuing text.', 164)]
        self.assertEqual(len(recover(rows, card=True)), 1)

    def test_bold_label_and_wrapped_italic_value_need_card_context(self):
        rows = [line([('Requirement:', 'Helvetica-Bold'), ('Pick a path', 'Helvetica-Oblique')], 100),
                line('and complete it', 112, font='Helvetica-Oblique'),
                line('Use the effect.', 126)]
        self.assertEqual([b.text for b in recover(rows, card=True)],
                         ['Requirement: Pick a path and complete it', 'Use the effect.'])
        self.assertEqual(len(recover(rows)), 1)

    def test_single_line_bold_label_and_italic_value(self):
        rows = [line([('Requirement:', 'Helvetica-Bold'), ('Rank 2+', 'Helvetica-Oblique')], 100),
                line('Use the effect.', 114)]
        self.assertEqual(len(recover(rows, card=True)), 2)
        self.assertEqual(len(recover(rows)), 1)

    def test_mixed_roman_value_is_not_an_italic_field(self):
        rows = [line([('Requirement:', 'Helvetica-Bold'), ('read', 'Helvetica-Oblique'),
                      ('this', 'Helvetica')], 100), line('continuation text.', 114)]
        self.assertEqual(len(recover(rows, card=True)), 1)

    def test_italic_subtitle_requires_heading_spacing_and_short_label(self):
        title = line('A Heading', 80, size=14)
        label = line('A Short Classification', 96, font='Helvetica-Oblique')
        body = line('The rules begin on a separate line.', 112)
        blocks = recover([title, label, body])
        self.assertEqual([b.text for b in blocks],
                         ['A Heading', 'A Short Classification', 'The rules begin on a separate line.'])
        self.assertEqual(len(recover([label, body])), 1)
        self.assertEqual(len(recover([title, label, line('The rules continue.', 109)])), 2)

    def test_separate_list_introduction_requires_position_and_numbered_item(self):
        rows = [line('The first sentence continues on', 100), line('a short final line.', 114),
                line('Follow these steps:', 128), line('1. The first step.', 145),
                line('2. The second step.', 159), line('The separate prose continues here.', 177)]
        blocks = recover(rows)
        self.assertEqual([b.text for b in blocks[:4]], [
            'The first sentence continues on a short final line.', 'Follow these steps:',
            '1. The first step.', '2. The second step.'])
        self.assertEqual([b.kind for b in blocks[:4]], ['paragraph', 'paragraph', 'list_item', 'list_item'])
        md, txt = render(blocks)
        self.assertIn('a short final line.\n\nFollow these steps:\n\n1.', md)
        self.assertIn('a short final line.\n\nFollow these steps:\n\n1.', txt)

    def test_wrapped_inline_list_introduction_remains_part_of_paragraph(self):
        rows = [line('The first sentence ends. Follow these steps to', 100),
                line('complete the task:', 114), line('1. The first step.', 131)]
        blocks = recover(rows)
        self.assertEqual(blocks[0].text,
                         'The first sentence ends. Follow these steps to complete the task:')
        self.assertEqual(len(blocks), 2)

    def test_short_line_and_colon_without_numbered_list_do_not_create_boundary(self):
        for following in ('ordinary continuation.', '◆ a bullet item.'):
            with self.subTest(following=following):
                rows = [line('The first sentence continues on', 100), line('a short final line.', 114),
                        line('Follow these steps:', 128), line(following, 145)]
                blocks = recover(rows)
                self.assertIn('a short final line. Follow these steps:', blocks[0].text)


@unittest.skipUnless((ROOT / 'example/6. Chapter 4 Heroic Paths.pdf').exists()
                     and (ROOT / 'example/7. Chapter 5 Radiant Paths.pdf').exists()
                     and (ROOT / 'example/9. Chapter 7 Items.pdf').exists(),
                     'Local chapter PDFs not supplied')
class LocalSourceFieldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks = {}
        cls.pages = {}
        for part, pages in ((6, (8, 9, 25, 28, 32)), (7, (22,)), (9, (25, 27, 30, 31, 32, 33))):
            with pdfplumber.open(next((ROOT / 'example').glob(f'{part}. *.pdf')), unicode_norm='NFC') as pdf:
                for number in pages:
                    page = _read_page(pdf.pages[number - 1], lambda _: None)
                    cls.pages[part, number] = page
                    cls.blocks[part, number] = layout_page(page, set(), lambda _: None, lambda _: False)

    def node(self, part, page, name):
        diagram = next(b for b in self.blocks[part, page] if b.role == 'diagram')
        return next(b for b in diagram.children if b.role == 'diagram_node'
                    and b.text.split('] ', 1)[-1].startswith(name))

    def test_all_heroic_card_fields_and_positive_control(self):
        cards = {8: [('Watchful Eye', 3), ('Get ’Em Talking', 3), ('Quick Analysis', 2),
                     ('Baleful', 2), ('Gather Evidence', 3), ('Hardy', 2),
                     ('Sleuth’s Instincts', 3), ('Close the Case', 3)],
                 9: [('Surefooted', 2)], 25: [('Hunter’s Edge', 3)], 32: [('Resilient Hero', 3)]}
        for page, names in cards.items():
            for name, count in names:
                with self.subTest(page=page, name=name):
                    node = self.node(6, page, name)
                    self.assertEqual(len(node.children), count)
                    self.assertEqual(node.children[0].kind, 'heading')
                    self.assertTrue(node.children[0].text.endswith(name))
                    if count == 3:
                        self.assertTrue(node.children[1].text.startswith('Prerequisite:'))
                        self.assertFalse(node.children[2].text.startswith('Prerequisite:'))
                    self.assertEqual(node.text, ' '.join(child.text for child in node.children))
                    self.assertTrue(all(child.sources[0].page == page for child in node.children))
                    self.assertTrue(all(node.sources[0].bbox[0] - 1 <= child.sources[0].bbox[0]
                                        and child.sources[0].bbox[2] <= node.sources[0].bbox[2] + 1
                                        and node.sources[0].bbox[1] - 1 <= child.sources[0].bbox[1]
                                        and child.sources[0].bbox[3] <= node.sources[0].bbox[3] + 1
                                        for child in node.children))

    def test_radiant_card_preserves_title_prerequisite_effect_and_reaction(self):
        node = self.node(7, 22, 'Edgedancer’s Grace')
        self.assertEqual(len(node.children), 3)
        self.assertEqual(node.children[0].text, '[always active] Edgedancer’s Grace')
        self.assertTrue(node.children[1].text.endswith('Speak the First Ideal'))
        self.assertTrue(node.children[2].text.startswith('While you have Investiture,'))
        self.assertEqual(node.children[2].text.count('[reaction]'), 1)

    def test_fifteen_standalone_tier_labels_follow_their_headings(self):
        names = {30: ['Accelerator', 'Armor Augmenter'],
                 31: ['Ascender', 'Bindrial (Area)', 'Bindrial (Self)', 'Compressor'],
                 32: ['Cremrial', 'Cultivator', 'Disruptor', 'Drainer', 'Liferial', 'Lightrial'],
                 33: ['Painrial (Amplifying/Numbing)', 'Projectile', 'Surge Fabrial']}
        for page, labels in names.items():
            blocks = self.blocks[9, page]
            for name in labels:
                with self.subTest(page=page, name=name):
                    i = next(i for i, b in enumerate(blocks) if b.kind == 'heading' and b.text == name)
                    self.assertRegex(blocks[i + 1].text, r'^Tier [1-4] Unique Fabrial Effect$')
                    self.assertEqual(len(blocks[i + 1].lines), 1)
                    self.assertEqual(blocks[i + 2].kind, 'paragraph')
                    self.assertFalse(blocks[i + 2].text.startswith('Tier '))
                    self.assertEqual(blocks[i + 1].sources[0].page, page)

    def test_numbered_procedure_and_existing_same_phrase_control(self):
        blocks = self.blocks[9, 27]
        intro = next(i for i, b in enumerate(blocks) if b.text.startswith('Use the following steps'))
        self.assertTrue(blocks[intro - 1].text.endswith('Gems” in chapter 8).'))
        self.assertEqual(blocks[intro].text, 'Use the following steps to craft your chosen fabrial:')
        self.assertEqual([b.kind for b in blocks[intro + 1:intro + 6]], ['list_item'] * 5)
        earlier = next(b for b in self.blocks[9, 25] if 'Use the following steps to craft your' in b.text)
        self.assertIn('item instead. Use the following steps to craft your chosen item:', earlier.text)

    def test_animal_bond_column_and_ambiguous_following_sentence_stay_joined(self):
        # The normal document-level pipeline removes running margins and learns
        # the column/paragraph context before merging the left and right sides.
        blocks = convert_pdf(ROOT / 'example/6. Chapter 4 Heroic Paths.pdf',
                             pages='28', hyphen_resolver=lambda _: False).blocks
        passage = next(b for b in blocks if 'for narrative reasons.)' in b.text)
        self.assertIn('retreat to safety. (However, your animal companion can still die', passage.text)
        self.assertIn('for narrative reasons.) If you ever lose your animal companion', passage.text)
        self.assertEqual([s.page for s in passage.sources], [28, 28])


if __name__ == '__main__':
    unittest.main()
