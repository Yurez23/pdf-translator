"""Conservative local note placement and verified symbol rows in frames."""

from copy import deepcopy
from pathlib import Path
import unittest

from pdf_translator import convert_pdf
from pdf_translator.export import render
from pdf_translator.layout import _locally_attached_notes, layout_page
from pdf_translator.models import Block, Glyph, SourceRegion, Word
from pdf_translator.page_data import PageData, TableData
from pdf_translator.symbols import LABELS


def block(kind, text, box, *, rows=None, extra=()):
    return Block(kind, text, (SourceRegion(1, box), *extra), rows=rows or [])


class NoteAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.page = PageData(1, 612, 792, [], 10)
        self.table = block('table', '', (50, 50, 550, 120), rows=[['A', 'B'], ['1', '*']])
        self.note = block('footnote', '* Note.', (50, 128, 150, 136))
        self.heading = block('heading', 'A rule (*)', (50, 170, 150, 183))
        self.first = block('paragraph', 'First part.', (50, 190, 270, 210))
        self.last = block('paragraph', 'Last part.', (50, 214, 260, 235))
        self.rule_note = block('footnote', '* Rule note.', (47, 242, 260, 260))

    def attached(self, blocks):
        return _locally_attached_notes(blocks, self.page)

    def test_marker_matches_adjacent_table_or_heading_and_keeps_prose(self):
        blocks = [self.table, self.note, self.heading, self.first, self.last, self.rule_note]
        before = deepcopy(blocks)
        self.assertEqual(self.attached(blocks), {id(self.note), id(self.rule_note)})
        self.assertEqual(blocks, before)
        # A normalized section can also retain the original table header box.
        self.table.sources += (SourceRegion(1, (50, 40, 550, 50)),)
        self.assertIn(id(self.note), self.attached(blocks))

    def test_unmatched_distant_misaligned_or_bottom_notes_remain_page_notes(self):
        for table, note in (
            (block('table', '', (50, 50, 550, 120), rows=[['A', 'B'], ['1', '2']]), self.note),
            (self.table, block('footnote', '† Note.', (50, 128, 150, 136))),
            (self.table, block('footnote', '* Note.', (50, 140, 150, 148))),
            (self.table, block('footnote', '* Note.', (90, 128, 150, 136))),
            (block('table', '', (50, 500, 550, 570), rows=[['A', '*']]),
             block('footnote', '* Bottom note.', (50, 578, 150, 586))),
        ):
            with self.subTest(note=note.text, box=note.sources[0].bbox):
                self.assertNotIn(id(note), self.attached([table, note]))

    def test_rule_requires_matching_trailing_heading_marker_and_uninterrupted_flow(self):
        headings = [block('heading', 'A rule', (50, 170, 150, 183)),
                    block('heading', 'A * rule', (50, 170, 150, 183)),
                    block('heading', 'A rule (†)', (50, 170, 150, 183))]
        for heading in headings:
            self.assertNotIn(id(self.rule_note), self.attached([heading, self.last, self.rule_note]))
        interrupted = block('aside', 'Other material', (50, 211, 260, 213))
        self.assertNotIn(id(self.rule_note), self.attached([self.heading, interrupted, self.last, self.rule_note]))
        other_column = block('paragraph', 'Elsewhere.', (318, 214, 520, 235))
        self.assertNotIn(id(self.rule_note), self.attached([self.heading, other_column, self.rule_note]))

    def test_synthetic_page_keeps_matched_table_note_in_both_exports(self):
        page = PageData(1, 612, 792, [
            Word('* Local note.', (50, 128, 120, 136), 8, 'Times-Italic'),
            Word('Section', (50, 150, 110, 164), 14, 'Times-Bold'),
            Word('Body text.', (50, 170, 120, 180), 10, 'Times-Roman'),
        ], 10, tables=[TableData((50, 50, 300, 120), [['Key', 'Value'], ['Entry', '*']])])
        blocks = layout_page(page, set(), lambda _: None, lambda _: False)
        self.assertEqual([b.kind for b in blocks], ['table', 'footnote', 'heading', 'paragraph'])
        md, txt = render(blocks)
        for export in (md, txt):
            self.assertLess(export.index('Entry'), export.index('Local note.'))
            self.assertLess(export.index('Local note.'), export.index('Section'))


class FramedSymbolRowTests(unittest.TestCase):
    def icon(self, x, y=120, identity='opportunity', angle=90):
        box = (x, y, x+20, y+20)
        return Word(LABELS[identity], box, 20, 'Verified', False, angle,
                    glyphs=(Glyph('O', 'Verified', box, symbol=identity),))

    def page(self, symbols, *, below=True):
        words = [Word('Trigger sentence.', (330, 75, 475, 85), 10, 'Times-Roman'),
                 *symbols]
        if below:
            words.append(Word('Card title', (330, 180, 450, 190), 10, 'Times-Bold'))
        return PageData(1, 612, 792, words, 10, frames=[(310, 50, 560, 220)])

    def run_page(self, page):
        before = deepcopy(page.words)
        warnings = []
        blocks = layout_page(page, set(), warnings.append, lambda _: False)
        self.assertEqual(page.words, before)
        return blocks, warnings

    def test_verified_row_is_owned_once_between_existing_children(self):
        symbols = [self.icon(350), self.icon(390), self.icon(430)]
        blocks, warnings = self.run_page(self.page(symbols))
        card = next(b for b in blocks if b.kind == 'aside')
        self.assertEqual([c.text for c in card.children], [
            'Trigger sentence.', '[opportunity] [opportunity] [opportunity]', 'Card title'])
        row = card.children[1]
        self.assertEqual(row.role, 'symbol_row')
        self.assertEqual([s.bbox for s in row.sources[1:]], [w.bbox for w in symbols])
        self.assertEqual(row.lines[0].words, symbols)
        self.assertFalse(any(w.code == 'rotated_text' for w in warnings))
        md, txt = render(blocks)
        self.assertEqual(md.count(r'\[opportunity\]'), 3)
        self.assertEqual(txt.count('[opportunity]'), 3)

    def test_unconfirmed_rows_keep_the_rotated_fallback_and_warning(self):
        cases = [
            self.page([self.icon(350)]),
            self.page([self.icon(350), self.icon(390, identity='unknown')]),
            self.page([self.icon(350), self.icon(390, identity='complication')]),
            self.page([self.icon(350), self.icon(390, y=125)]),
            self.page([self.icon(350), self.icon(390, angle=270)]),
            self.page([self.icon(350), self.icon(390)], below=False),
            self.page([self.icon(250), self.icon(280)]),
            self.page([self.icon(305), self.icon(350)]),
            self.page([self.icon(350), self.icon(390), Word('Other', (430, 120, 450, 140), 10, 'Other', False, 90)]),
        ]
        for page in cases:
            with self.subTest(words=page.words):
                blocks, warnings = self.run_page(page)
                self.assertTrue(any(w.code == 'rotated_text' for w in warnings))
                self.assertTrue(any(b.role == 'rotated' for b in blocks))
                self.assertFalse(any(c.role == 'symbol_row' for b in blocks for c in b.children))

    def test_other_rotated_text_keeps_its_own_fallback_warning(self):
        page = self.page([self.icon(350), self.icon(390)])
        page.words.append(Word('AXIS', (570, 250, 580, 290), 10, 'Other', False, 90))
        blocks, warnings = self.run_page(page)
        self.assertTrue(any(c.role == 'symbol_row' for b in blocks for c in b.children))
        self.assertEqual([b.text for b in blocks if b.role == 'rotated'], ['AXIS'])
        self.assertTrue(any(w.code == 'rotated_text' for w in warnings))


class OptionalLocalNoteTests(unittest.TestCase):
    def test_source_notes_follow_their_table_or_complete_rule_in_both_exports(self):
        root = Path(__file__).resolve().parents[1] / 'example'
        cases = [
            ('9. Chapter 7 Items.pdf', '8', 'Grandbow', '*See the “Shardblades and Radiant Shardblades” section.', 'Improvised Weapons'),
            ('10. Chapter 8 Goals and Rewards.pdf', '7', 'Chasmfiend', '*See the “Animal Companions” appendix.', 'For example, if your companion'),
            ('12. Chapter 10 Combat.pdf', '4', 'second Disengage action.', '* To use this action, you must use [1 action] for the Ready action itself, plus the number of [1 action] required for the action you chose to prepare.', 'Recover'),
            ('14. Chapter 12 Endeavors.pdf', '3', 'Insurmountable', '*See the “Discovery Endeavors” section.', 'Setting the Threshold'),
        ]
        for name, selection, preceding, note_text, following in cases:
            with self.subTest(source=name):
                path = root / name
                if not path.is_file():
                    self.skipTest('Optional local PDF not supplied: ' + name)
                result = convert_pdf(path, pages=selection, hyphen_resolver=lambda _: False)
                note = next(b for b in result.blocks if b.kind == 'footnote' and b.text == note_text)
                self.assertEqual(note.sources[0].page, int(selection))
                i = result.blocks.index(note)
                before, after = result.blocks[i-1:i+2:2]
                before_text = ' '.join(cell for row in before.rows for cell in row) if before.kind == 'table' else before.text
                self.assertIn(preceding, before_text)
                self.assertIn(following, after.text)
                if name.startswith('12.'):
                    self.assertEqual(note.text.count('[1 action]'), 2)
                self.assertEqual(render(list(result.blocks)), (result.markdown, result.text))
                for export in (result.markdown, result.text):
                    escape = lambda text: text.replace('*', r'\*').replace('[', r'\[').replace(']', r'\]') if export is result.markdown else text
                    self.assertEqual(export.count(escape(note_text)), 1)
                    self.assertLess(export.index(preceding), export.index(escape(note_text)))
                    self.assertLess(export.index(escape(note_text)), export.index(following))


if __name__ == '__main__':
    unittest.main()
