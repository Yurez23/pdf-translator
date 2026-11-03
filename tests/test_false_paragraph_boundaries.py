"""Regression coverage for false boundaries at physical line and page wraps."""

import unittest

from pdf_translator.continuations import merge_continuations
from pdf_translator.layout import _blocks_for_lines
from pdf_translator.models import Block, SourceRegion, union_bbox
from pdf_translator.page_data import PageData
from tests.test_layout_recovery import label, line, recover


class FalseParagraphBoundaryTests(unittest.TestCase):
    def test_inline_colon_and_wrapped_bold_term_do_not_create_fields(self):
        for opening in ('Use the', [('Use the', 'Helvetica'), ('extended', 'Helvetica-Bold')]):
            with self.subTest(opening=opening):
                blocks, _ = recover([
                    line(opening, 100),
                    label('range', 'for the following checks:', 114),
                    line('◆ Check one.', 128),
                    line('◆ Check two.', 142),
                ])
                self.assertEqual([b.kind for b in blocks], ['paragraph', 'list_item', 'list_item'])
                self.assertTrue(blocks[0].text.endswith('range for the following checks:'))

    def test_inline_italic_colon_keeps_numeric_value_and_next_entry(self):
        blocks, _ = recover([
            line([('Throw.', 'Helvetica-Bold'), ('Choose a target.', 'Helvetica'),
                  ('Impact:', 'Helvetica-Oblique')], 100),
            line('3 points of damage.', 114),
            label('Dodge.', 'Move out of the way.', 130),
        ])
        self.assertEqual([b.text for b in blocks], [
            'Throw. Choose a target. Impact: 3 points of damage.',
            'Dodge. Move out of the way.',
        ])

    def test_empty_bold_field_retains_boundary(self):
        blocks, _ = recover([
            line('First field:', 100, font='Helvetica-Bold'),
            line('Second field:', 114, font='Helvetica-Bold'),
            line('An independent description.', 128),
        ])
        self.assertEqual([b.text for b in blocks], [
            'First field:', 'Second field:', 'An independent description.',
        ])

    def test_hanging_last_line_stays_with_entry_before_flush_left_paragraph(self):
        blocks, _ = recover([
            label('Prepare.', 'Keep all of the required equipment packed securely', 100),
            line('and place it within reach before the', 114, x=62),
            line('journey begins.', 128, x=62),
            line('The next paragraph starts at the margin.', 142),
        ])
        self.assertEqual([b.kind for b in blocks], ['list_item', 'paragraph'])
        self.assertTrue(blocks[0].text.endswith('before the journey begins.'))

    def test_prior_rule_does_not_interrupt_same_style_small_text(self):
        rows = [line('Follow the instructions to', 565, size=8),
                line('complete the task.', 577, size=8),
                line('* See the reference.', 598, size=8)]
        page = PageData(1, 612, 792, [], 10, separators=[553])
        blocks = _blocks_for_lines(rows, page, (50, 0, 290, 792), set(), lambda _: None)
        self.assertEqual([(b.kind, b.text) for b in blocks], [
            ('paragraph', 'Follow the instructions to complete the task.'),
            ('footnote', '* See the reference.'),
        ])

    def test_rule_between_lines_still_introduces_unmarked_footnote(self):
        for rule, note in [(575, 'Supporting note.'), (553, '* Supporting note.')]:
            with self.subTest(rule=rule, note=note):
                rows = [line('The independent text trails off', 565, size=8),
                        line(note, 577, size=8)]
                page = PageData(1, 612, 792, [], 10, separators=[rule])
                blocks = _blocks_for_lines(rows, page, (50, 0, 290, 792), set(), lambda _: None)
                self.assertEqual([b.kind for b in blocks], ['paragraph', 'footnote'])


class CapitalizedPageWrapTests(unittest.TestCase):
    @staticmethod
    def blocks(*, after='District on the far shore.', side_right=285, page=2):
        def paragraph(number, rows):
            return Block('paragraph', ' '.join(row.text for row in rows),
                         (SourceRegion(number, union_bbox([row.bbox for row in rows])),),
                         lines=rows, container_left=318, container_width=231)

        before = paragraph(1, [
            line('Visitors who want to see the old city', 685, x=330),
            line('can take the evening ferry toward the Northern', 699, x=318),
        ])
        title = Block('heading', 'Route map', (SourceRegion(page, (60, 54, 140, 65)),),
                      role='aside_title')
        diagram = Block('aside', 'Diagram', (SourceRegion(page, (55, 80, side_right, 345)),),
                        role='diagram', children=[title])
        quote = Block('aside', 'Quotation', (SourceRegion(page, (60, 380, 280, 420)),),
                      role='quotation')
        return [before, title, diagram, quote, paragraph(page, [line(after, 53, x=318)])]

    def test_capitalized_name_wraps_beside_diagram_without_moving_it(self):
        before, title, diagram, quote, after = self.blocks()
        blocks = merge_continuations([before, title, diagram, quote, after], set(), lambda _: None,
                                     selected_pages={1, 2})
        self.assertEqual(blocks, [before, title, diagram, quote])
        self.assertTrue(before.text.endswith('the Northern District on the far shore.'))
        self.assertEqual([s.page for s in before.sources], [1, 2])

    def test_capitalized_start_requires_lateral_material_and_adjacent_selection(self):
        for options, selection in [
            ({'side_right': 340}, {1, 2}),
            ({'page': 3}, {1, 3}),
            ({'after': '3. Start a new item.'}, {1, 2}),
            ({}, {2}),
        ]:
            with self.subTest(options=options, selection=selection):
                blocks = self.blocks(**options)
                self.assertEqual(merge_continuations(blocks, set(), lambda _: None,
                                                     selected_pages=selection), blocks)
