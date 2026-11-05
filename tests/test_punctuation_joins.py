"""PDF-26 punctuation joins preserve literal source spaces and KEEP choices."""

import unittest

from pdf_translator.text import join_lines, normalize


class PunctuationJoinTests(unittest.TestCase):
    def test_closed_dash_wrap_has_no_added_space(self):
        self.assertEqual(join_lines(['a closed—','continuation'],set(),1,self.fail),'a closed—continuation')
        self.assertEqual(join_lines(['a spaced —','continuation'],set(),1,self.fail),'a spaced — continuation')
        self.assertEqual(join_lines(['—','value'],set(),1,self.fail),'— value')

    def test_period_after_bracket_or_game_symbol(self):
        self.assertEqual(normalize('Ability (Costs 1 Focus) . Next.'),'Ability (Costs 1 Focus). Next.')
        self.assertEqual(join_lines(['Use [reaction]','. Next.'],set(),1,self.fail),'Use [reaction]. Next.')
        self.assertEqual(normalize('value 1 . 5'),'value 1 . 5')

    def test_source_ellipsis_and_hyphen_decisions_are_preserved(self):
        self.assertEqual(normalize('friend … uh…'),'friend … uh…')
        decisions=[]
        self.assertEqual(join_lines(['Whe-','never'],set(),1,self.fail,
                                   lambda issue: decisions.append(issue) or False),'Whe-never')
        self.assertEqual(len(decisions),1)
        self.assertEqual(join_lines(['exper-','tise'],{'expertise','exper-tise'},1,self.fail),'exper-tise')
