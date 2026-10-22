"""General layout regressions and optional local Introduction checks."""

from collections import Counter
from pathlib import Path
import unittest

from pdf_translator import convert_pdf
from pdf_translator.export import render
from pdf_translator.geometry import group_lines
from pdf_translator.headings import assign_heading_levels
from pdf_translator.layout import _blocks_for_lines
from pdf_translator.models import Line, Word, union_bbox
from pdf_translator.page_data import PageData

ROOT = Path(__file__).resolve().parents[1]
INTRO = ROOT / 'example/2. Introduction.pdf'


def line(parts, y, *, x=50, size=10, font='Helvetica'):
    if isinstance(parts, str):
        parts = [(parts, font)]
    words = []
    for text, family in parts:
        for token in text.split():
            box = (x, y, x + len(token) * size * .48, y + size)
            words.append(Word(token, box, size, family))
            x = box[2] + size * .25
    return Line(words, union_bbox([w.bbox for w in words]), size)


def label(name, text, y, **kwargs):
    return line([(name, 'Helvetica-Bold'), (text, 'Helvetica')], y, **kwargs)


def recover(lines):
    page = PageData(1, 612, 792, [w for row in lines for w in row.words], 10)
    warnings = []
    blocks = _blocks_for_lines(lines, page, (50, 0, 290, 792), set(), warnings.append)
    assign_heading_levels(blocks, {1}, warnings.append)
    return blocks, warnings


class RecoveryRulesTests(unittest.TestCase):
    def test_larger_italic_side_column_does_not_interleave_with_main_prose(self):
        from pdf_translator.layout import layout_page
        rows=[]
        for i in range(12):
            rows.append(line('The main text stays together in its column.',100+i*14))
        for i in range(5):
            rows.append(line('A separate literary quotation.',105+i*16,x=330,size=12,font='Times-Italic'))
        page=PageData(1,612,792,[w for row in rows for w in row.words],10)
        blocks=layout_page(page,set(),lambda _:None)
        main=next(b for b in blocks if b.text.startswith('The main'))
        self.assertNotIn('literary',main.text)
        quotes=[b for b in blocks if 'literary' in b.text]
        self.assertTrue(quotes)
        self.assertTrue(all(b.kind=='aside' for b in quotes))
        self.assertNotIn('main text',quotes[0].text)

    def test_completed_label_with_two_hanging_lines_is_one_item(self):
        blocks, _ = recover([label('Field Guide.', 'You are reading it now!', 100),
            line('It provides the first part of the rules', 114, x=62),
            line('and explains the remaining rules.', 128, x=62)])
        self.assertEqual([b.kind for b in blocks], ['list_item'])
        self.assertIn('now! It provides', blocks[0].text)

    def test_completed_dialogue_with_one_hanging_line_and_peer(self):
        blocks, _ = recover([label('Avery:', 'Are we ready?', 100),
            line('Let us begin!', 114, x=62), label('Narrator:', 'The door opens.', 128)])
        self.assertEqual(blocks[0].kind, 'list_item')
        self.assertEqual(blocks[0].text, 'Avery: Are we ready? Let us begin!')
        self.assertEqual(blocks[1].text, 'Narrator: The door opens.')

    def test_colon_does_not_borrow_a_new_paragraphs_indent(self):
        blocks, _ = recover([label('Note:', 'This is a complete sentence.', 100),
            line('Another paragraph begins here', 114, x=62), line('and returns to the left.', 128)])
        self.assertEqual([b.kind for b in blocks], ['paragraph', 'paragraph'])

    def test_bold_phrase_wrapping_at_a_period_is_not_a_new_entry(self):
        blocks, _ = recover([line([('You can raise the', 'Helvetica'), ('dramatic', 'Helvetica-Bold')], 100),
            label('stakes.', 'Then continue the same paragraph.', 114)])
        self.assertEqual(len(blocks), 1)
        self.assertIn('dramatic stakes. Then', blocks[0].text)

    def test_new_bold_term_after_an_unfinished_line_stays_in_paragraph(self):
        blocks, _ = recover([line('These games are often called', 100),
            label('one-shots.', 'They may also be longer.', 114)])
        self.assertEqual(len(blocks), 1)
        self.assertIn('called one-shots.', blocks[0].text)

    def test_missing_inline_glyph_indent_does_not_start_a_paragraph(self):
        blocks, _ = recover([line('If the result has an or', 100),
            line('symbol, consult the rule.', 114, x=62), line('Next Heading', 135, size=15)])
        self.assertEqual(len(blocks), 2)
        self.assertIn('or symbol,', blocks[0].text)

    def test_real_indented_paragraph_after_unfinished_text_stays_separate(self):
        blocks, _ = recover([line('An unfinished paragraph with', 100),
            line('a new indented paragraph', 114, x=62), line('whose next line returns left.', 128)])
        self.assertEqual(len(blocks), 2)

    def test_dialogue_and_standalone_italic_narration_are_separate(self):
        blocks, _ = recover([label('Host:', 'Make a test.', 100),
            line('And raise the stakes.', 114, x=62),
            line('The player rolls the dice.', 132, font='Helvetica-Oblique')])
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0].text, 'Host: Make a test. And raise the stakes.')
        self.assertEqual(blocks[1].text, 'The player rolls the dice.')

    def test_inline_italics_on_a_normal_line_gap_do_not_split(self):
        blocks, _ = recover([line('A title is mentioned here.', 100),
            line('An Entire Italic Title', 114, font='Helvetica-Oblique'),
            line('continues in ordinary prose.', 128)])
        self.assertEqual(len(blocks), 1)

    def test_small_diamond_bullets_share_the_prose_row(self):
        words = []
        for y, text in [(100, 'First item.'), (114, 'Second item with'), (128, 'its continuation.'),
                        (142, 'Third item.')]:
            words.extend(line(text, y, x=62).words)
        for y in (100, 114, 142):
            words.append(Word('◆', (51, y + 1, 55, y + 7), 6, 'ZapfDingbats'))
        words.extend(line('After the list.', 160).words)
        rows = group_lines(words, 10)
        blocks, _ = recover(rows)
        self.assertEqual([b.kind for b in blocks], ['list_item'] * 3 + ['paragraph'])
        self.assertEqual(blocks[1].text, '◆ Second item with its continuation.')
        self.assertIn('- Second item with its continuation.', render(blocks)[0])
        self.assertEqual(sum(b.text.count('◆') for b in blocks), 3)

    def test_distant_marker_is_not_attached_to_unrelated_text(self):
        words = line('Unrelated text', 100, x=120).words + [Word('◆', (50, 101, 54, 107), 6)]
        self.assertEqual(len(group_lines(words, 10)), 2)

    def test_graphic_labels_preserved_but_excluded_from_heading_bands(self):
        numbers = line('1 2 3 4', 220, size=24)
        for i, w in enumerate(numbers.words):
            w.bbox = (50 + i * 40, 220, 64 + i * 40, 244)
        numbers.bbox = union_bbox([w.bbox for w in numbers.words])
        blocks, warnings = recover([line('Main Title', 80, size=22), line('Body text.', 112),
            numbers, line('Z', 270, size=64, font='GenericSymbolFont'),
            line('Subsection', 360, size=16), line('More body text.', 390)])
        self.assertEqual({b.text: b.level for b in blocks if b.kind == 'heading'},
                         {'Main Title': 1, 'Subsection': 2})
        self.assertEqual([b.text for b in blocks if b.role == 'graphic_label'], ['1 2 3 4', 'Z'])
        self.assertTrue(any(w.code == 'graphic_text' for w in warnings))
        expected = Counter(c for r in [numbers] for c in r.text if c.isalnum())
        self.assertEqual(expected, Counter(c for c in blocks[2].text if c.isalnum()))

    def test_single_numeric_and_letter_titles_remain_possible(self):
        blocks, _ = recover([line('2026', 80, size=24), line('Body text.', 112),
            line('I', 180, size=18), line('More body text.', 210)])
        self.assertEqual({b.text: b.level for b in blocks if b.kind == 'heading'}, {'2026': 1, 'I': 2})

    def test_normal_words_in_a_symbol_capable_font_are_not_demoted(self):
        blocks, _ = recover([line('Ordinary Title', 80, size=24, font='Symbola'),
                             line('Body text.', 112)])
        self.assertEqual(blocks[0].kind, 'heading')
        self.assertEqual(blocks[0].level, 1)

    def test_graphic_warning_survives_safe_telemetry(self):
        from pdf_translator.telemetry import Observer
        class Sink:
            def __init__(self):
                self.items = []
            def write(self, value):
                self.items.append(value)
        sink = Sink()
        Observer(sink).emit('conversion.warning', code='graphic_text', pages=(8,))
        self.assertEqual(sink.items[0]['code'], 'graphic_text')
        self.assertEqual(sink.items[0]['pages'], [8])


@unittest.skipUnless(INTRO.is_file(), 'Local Introduction PDF is not distributed')
class IntroductionRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = convert_pdf(INTRO, hyphen_resolver=lambda _: False)

    def test_document_hierarchy_excludes_diagram(self):
        headings = {b.text: b.level for b in self.result.blocks if b.kind == 'heading' and not b.role}
        for name, level in [('Introduction', 1), ('The Cosmere RPG', 2), ('Adventures on Roshar', 3),
                            ('Plot Die', 3), ('Raising the Stakes', 4)]:
            self.assertEqual(headings[name], level)
        self.assertFalse({'C', 'O O', '1 2 3 4 5 6', '2 4'} & headings.keys())
        self.assertFalse(any(w.code == 'heading_depth' for w in self.result.warnings))

    def test_first_book_list_entry_is_complete(self):
        b = next(b for b in self.result.blocks if b.text.startswith('Stormlight Handbook.'))
        self.assertEqual(b.kind, 'list_item')
        self.assertIn('now! It contains', b.text)

    def test_paragraphs_are_not_split_at_wrapped_bold_or_inline_graphics(self):
        for phrase in ('raising the stakes. When the stakes', 'called one-shots.',
                       'If you roll an [opportunity] or [complication] symbol,'):
            with self.subTest(phrase=phrase):
                self.assertTrue(any(phrase in b.text for b in self.result.blocks))

    def test_three_diamond_items_and_following_prose(self):
        entries = [b for b in self.result.blocks if b.kind == 'list_item' and b.sources[0].page == 8]
        self.assertEqual(len(entries), 3)
        self.assertTrue(entries[1].text.endswith('obstacle, or goals.'))
        self.assertTrue(entries[2].text.endswith('as determined by the GM.'))
        following = next(b for b in self.result.blocks if b.text.startswith('The stakes can only'))
        self.assertEqual(following.kind, 'paragraph')

    def test_replies_and_narration_do_not_mix(self):
        for prefix, ending in [('Rico (playing Aj): Well,', 'Let’s get in there after them!'),
                              ('GM: Okay,', 'And raise the stakes again.'),
                              ('GM: Alright, looks', 'Who wants to take a fast turn?')]:
            with self.subTest(prefix=prefix):
                b = next(b for b in self.result.blocks if b.text.startswith(prefix))
                self.assertTrue(b.text.endswith(ending))
                self.assertNotIn('Mia rolls', b.text)
                self.assertNotIn('The players briefly discuss', b.text)
        self.assertTrue(any(b.text.startswith('Mia rolls a 3') for b in self.result.blocks))
        self.assertTrue(any(b.text.startswith('The players briefly discuss') for b in self.result.blocks))

    def test_framed_sidebars_and_source_geometry_are_preserved(self):
        self.assertEqual(sum(b.kind == 'aside' and bool(b.children) and b.role != 'quotation'
                             for b in self.result.blocks), 4)
        # A multi-paragraph literary quotation is a separate container, not
        # an extra framed sidebar.
        self.assertTrue(any(b.role == 'quotation' and b.children for b in self.result.blocks))
        for b in self.result.blocks:
            self.assertNotIn('\n', b.text)
            self.assertTrue(b.sources)


if __name__ == '__main__':
    unittest.main()
