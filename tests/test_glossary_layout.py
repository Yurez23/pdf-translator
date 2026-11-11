"""Three-column reading and hanging definitions through real PDF extraction."""

from pathlib import Path
import tempfile
import unittest

from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.layout import layout_page
from pdf_translator.page_data import PageData
from tests.test_layout_recovery import line


class GlossaryLayoutTests(unittest.TestCase):
    def convert(self, draw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'columns.pdf'
            canvas = Canvas(str(path), pagesize=(612, 792))
            draw(canvas)
            canvas.save()
            return convert_pdf(path, hyphen_resolver=lambda _: False)

    def test_three_columns_keep_definitions_references_and_wrapped_labels(self):
        expected = []

        def draw(canvas):
            canvas.setFont('Helvetica-Bold', 20)
            canvas.drawString(160, 748, 'Reference definitions across columns')
            for column, x in enumerate((50, 225, 400)):
                for entry in range(4):
                    name = f'Term{column}{entry}'
                    y = 680 - entry * 110
                    canvas.setFont('Helvetica-Bold', 10)
                    canvas.drawString(x, y, name + ' (a wrapped')
                    canvas.drawString(x + 12, y - 14, 'label):')
                    end = x + 12 + canvas.stringWidth('label):', 'Helvetica-Bold', 10)
                    canvas.setFont('Helvetica', 10)
                    canvas.drawString(end + 3, y - 14, 'The definition')
                    canvas.drawString(x + 12, y - 28, 'continues with all its words.')
                    canvas.setFont('Helvetica-Bold', 10)
                    canvas.drawString(x + 12, y - 42, f'{column + 10}, {entry + 20}')
                    canvas.setFont('Helvetica-BoldOblique', 10)
                    canvas.drawString(x + 12, y - 56, name + ', alias:')
                    end = x + 12 + canvas.stringWidth(name + ', alias:', 'Helvetica-BoldOblique', 10)
                    canvas.setFont('Helvetica', 10)
                    canvas.drawString(end + 3, y - 56, 'See original.')
                    expected.extend([f'{name} (a wrapped label): The definition continues with all its words. {column + 10}, {entry + 20}',
                                     f'{name}, alias: See original.'])
        result = self.convert(draw)
        entries = [b.text for b in result.blocks if b.kind != 'heading']
        self.assertEqual(entries, expected)
        for output in (result.markdown, result.text):
            self.assertEqual(output.split('\n\n')[1:], [*expected[:-1], expected[-1] + '\n'])
        self.assertFalse(any(b.kind == 'heading' and b.text[:1].isdigit() for b in result.blocks))

    def test_wide_title_and_two_columns_keep_their_reading_order(self):
        def draw(canvas):
            canvas.setFont('Helvetica-Bold', 20)
            canvas.drawString(160, 740, 'An ordinary chapter heading')
            canvas.setFont('Helvetica', 10)
            for x, token in ((50, 'Left'), (330, 'Right')):
                for i in range(10):
                    canvas.drawString(x, 680 - i * 14, f'{token} column sentence number {i}.')
        result = self.convert(draw)
        self.assertLess(result.text.index('number 9.'), result.text.index('Right column'))
        self.assertEqual(result.text.count('Left column'), 10)
        self.assertEqual(result.text.count('Right column'), 10)

    def test_aligned_word_spaces_do_not_split_a_single_prose_column(self):
        rows = [line('Some ordinary words form one complete sentence.', 100 + i * 14)
                for i in range(12)]
        page = PageData(1, 612, 792, [w for row in rows for w in row.words], 10)
        blocks = layout_page(page, set(), lambda _: None)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text.count('one complete sentence.'), 12)

    def test_isolated_bold_colon_in_prose_does_not_establish_definition_layout(self):
        rows = [line('This paragraph introduces a short explanation.', 100),
                line([('Note:', 'Helvetica-Bold'), ('a phrase within ordinary prose', 'Helvetica')], 114),
                line('that continues on the following line.', 128)]
        page = PageData(1, 612, 792, [w for row in rows for w in row.words], 10)
        blocks = layout_page(page, set(), lambda _: None)
        self.assertTrue(any('ordinary prose that continues' in b.text for b in blocks))
        self.assertFalse(any(b.role == 'labelled_entry' for b in blocks))

    def test_dialogue_with_hanging_speakers_keeps_separate_italic_narration(self):
        rows = []
        for i in range(4):
            y = 100 + i * 60
            rows += [line([('Speaker:', 'Helvetica-Bold'), ('a complete sentence.', 'Helvetica')], y),
                     line('Another complete sentence.', y + 14, x=62),
                     line('Narration remains a separate paragraph.', y + 28, font='Helvetica-Oblique')]
        page = PageData(1, 612, 792, [w for row in rows for w in row.words], 10)
        blocks = layout_page(page, set(), lambda _: None)
        self.assertEqual(sum(b.text == 'Narration remains a separate paragraph.' for b in blocks), 4)


class OptionalGlossaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1] / 'example/18. Glossary and Index.pdf'
        if not source.is_file():
            raise unittest.SkipTest('Local glossary PDF not supplied')
        cls.result = convert_pdf(source, hyphen_resolver=lambda _: False)

    def entry(self, prefix, page):
        entries = [b for b in self.result.blocks if b.text.startswith(prefix)
                   and b.sources[0].page == page]
        self.assertEqual(len(entries), 1, prefix)
        return entries[0].text

    def test_reward_and_primary_sense_are_separate_complete_entries(self):
        reward = self.entry('reward:', 7)
        sense = self.entry('sense, primary:', 7)
        self.assertEqual(reward, 'reward: When you conclude a goal, you gain a powerful reward such as a fabrial, traveling companion, or noble title. 21, 279–285, 356')
        self.assertEqual(sense, 'sense, primary: Your primary sense is the one you most heavily rely on to navigate the world, such as sight or hearing. 54')
        for output in (self.result.markdown, self.result.text):
            self.assertIn(reward + '\n\n', output)
            self.assertIn(sense + '\n\n', output)

    def test_actions_and_attack_references_stay_with_their_entries(self):
        agility = self.entry('Agility (', 1)
        aid = self.entry('Aid ', 1)
        attack = self.entry('attack test:', 1)
        self.assertTrue(agility.endswith('acrobatic feats. 60'))
        self.assertIn('[reaction]', aid)
        self.assertTrue(aid.endswith('advantage. 305'))
        self.assertTrue(attack.endswith('attack deals. 305–306'))
        self.assertEqual(self.entry('attack multiple targets:', 1), 'attack multiple targets: 306')
        self.assertEqual(self.entry('ancestry:', 1), 'ancestry: Your ancestry can grant special talents and abilities. 16, 30–31')

    def test_wrapped_italic_labels_start_at_their_first_line(self):
        self.assertEqual(self.entry('character creation, higher-level:', 2),
                         'character creation, higher-level: 357–358')
        self.assertEqual(self.entry('Complication, adversary rolling a:', 2),
                         'Complication, adversary rolling a: 346–347')

    def test_primary_sense_profile_and_reactions_remain_independent(self):
        for prefix in ('primary sense:', 'profile:', 'Reactive Strike ', 'Progression (', 'Ready ('):
            with self.subTest(prefix=prefix):
                entry = self.entry(prefix, 6)
                self.assertEqual(entry.count(':'), 1)
                self.assertIn(entry, self.result.text)
                escaped = entry.replace('[', r'\[').replace(']', r'\]')
                self.assertIn(escaped, self.result.markdown)


if __name__ == '__main__':
    unittest.main()
