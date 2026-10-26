"""PDF-07: structural evidence for complete-sentence paragraph continuations."""

import copy
import tempfile
import unittest
from pathlib import Path

from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Table, TableStyle

from pdf_translator import convert_pdf
from pdf_translator.continuations import merge_continuations
from pdf_translator.export import render
from pdf_translator.models import Block, Line, SourceRegion, Word
from pdf_translator.paragraph_conventions import infer_paragraph_conventions
from tests.test_converter import PAGE, lines


def paragraph(text="An earlier sentence. More details.", *, page=1, top=110, x=50,
              indent=0, font="Helvetica", size=10, width=240, last_width=230):
    rows = []
    for i, part in enumerate((text, "The final statement.")):
        box = (x + (indent if i == 0 else 0), top + i*14, x + (width if i == 0 else last_width), top + i*14 + size)
        rows.append(Line([Word(part, box, size, font)], box, size))
    return Block("paragraph", " ".join(l.text for l in rows),
                 (SourceRegion(page, (x, top, x+width, top+14+size)),),
                 lines=rows, container_left=x, container_width=width, body_size=size)


def training():
    return [paragraph(page=p, top=110+i*28, indent=12 if i else 0)
            for p in (1, 2) for i in range(3)]


class ParagraphConventionTests(unittest.TestCase):
    def merge(self, a=None, b=None, *, witnesses=None, between=(), **kwargs):
        a = a or paragraph(page=3, top=680)
        b = b or paragraph("Capitalised next sentence.", page=4)
        return merge_continuations([a, *between, b], set(), self.fail,
                                   paragraph_conventions=infer_paragraph_conventions(
                                       training() if witnesses is None else witnesses), **kwargs)

    def test_capitalised_complete_sentence_with_independent_indent_evidence(self):
        a, b = paragraph(page=3, top=680), paragraph("Capitalised next sentence.", page=4)
        expected = a.text + " " + b.text
        original_lines = a.lines + b.lines
        sources = a.sources + b.sources
        result = self.merge(a, b)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].text, expected)
        self.assertEqual(result[0].sources, sources)
        self.assertEqual(result[0].lines, original_lines)
        self.assertTrue(all(x is y for x, y in zip(result[0].lines, original_lines)))
        self.assertEqual(render(result), (expected+"\n", expected+"\n"))

    def test_punctuation_does_not_decide_the_paragraph_boundary(self):
        for ending in (".", "?", "!", "…", "Dr.", '.”', '?)', '!]', ':'):
            with self.subTest(ending=ending):
                a = paragraph(page=3, top=680)
                a.text = "An exact ending" + ending
                b = paragraph("Capitalised next sentence.", page=4)
                expected = a.text + " " + b.text
                self.assertEqual(self.merge(a, b)[0].text, expected)

    def test_new_first_line_indent_is_a_boundary_for_both_full_and_short_ends(self):
        for width in (230, 90):
            with self.subTest(width=width):
                self.assertEqual(len(self.merge(paragraph(page=3, top=680, last_width=width),
                                                paragraph(page=4, indent=12))), 2)

    def test_short_complete_last_line_is_ambiguous(self):
        self.assertEqual(len(self.merge(paragraph(page=3, top=680, last_width=150))), 2)

    def test_absent_weak_or_single_container_convention_does_not_authorize_merge(self):
        for samples in ([], training()[:2], training()[:3]):
            with self.subTest(count=len(samples)):
                self.assertEqual(len(self.merge(witnesses=samples)), 2)

    def test_flush_spaced_or_inconsistent_peers_veto_the_indent_rule(self):
        for change in ("flush", "space", "indent"):
            samples = training()
            peer = samples[1]
            if change == "flush":
                peer.lines[0].bbox = (50, 138, 290, 148)
            elif change == "space":
                for line in peer.lines:
                    line.bbox = tuple(v + (8 if i in (1, 3) else 0) for i, v in enumerate(line.bbox))
            else:
                peer.lines[0].bbox = (70, 138, 290, 148)
            with self.subTest(change=change):
                self.assertEqual(len(self.merge(witnesses=samples)), 2)

    def test_style_changes_are_not_continuations(self):
        for font, size, color in (("Courier", 10, None), ("Helvetica", 12, None),
                                  ("Helvetica", 10, (1., 0., 0.)), ("Helvetica-Oblique", 10, None)):
            b = paragraph(page=4, font=font, size=size)
            for line in b.lines:
                for word in line.words:
                    word.color = color
            with self.subTest(font=font, size=size, color=color):
                self.assertEqual(len(self.merge(b=b)), 2)

    def test_subset_prefixes_and_geometry_noise(self):
        b = paragraph(page=4, x=77, font="AAAAAA+Helvetica", size=10.01, width=240.02)
        b.lines[0].bbox = (77.08, 110.02, 317, 120.03)
        self.assertEqual(len(self.merge(b=b)), 1)

    def test_same_page_column_boundary(self):
        self.assertEqual(len(self.merge(b=paragraph(page=3, x=318))), 1)

    def test_same_column_and_forward_vertical_jump_are_not_reading_boundaries(self):
        for x, top in ((50, 710), (318, 710)):
            with self.subTest(x=x):
                self.assertEqual(len(self.merge(b=paragraph(page=3, x=x, top=top))), 2)

    def test_omitted_or_empty_page_is_not_complete_sentence_evidence(self):
        self.assertEqual(len(self.merge(b=paragraph(page=5), empty_pages={4}, selected_pages={3,4,5})), 2)
        self.assertEqual(len(self.merge(b=paragraph(page=5), selected_pages={3,5})), 2)

    def test_all_intervening_structures_are_barriers_in_the_new_branch(self):
        for kind, role in (("heading", ""), ("table", ""), ("aside", ""),
                           ("aside", "quotation"), ("aside", "diagram"),
                           ("aside", "form_insert"), ("raw", "rotated"),
                           ("footnote", ""), ("list_item", ""), ("paragraph", "")):
            item = paragraph("Intervening content.", page=3, top=720)
            item.kind, item.role = kind, role
            with self.subTest(kind=kind, role=role):
                result = self.merge(between=(item,), selected_pages={3,4}, insert_pages={3})
                if kind == "paragraph":
                    # The intervening prose may itself continue on the next
                    # page; it must never be skipped to join the earlier block.
                    self.assertNotIn("Capitalised", result[0].text)
                    self.assertEqual([s.page for s in result[0].sources], [3])
                else:
                    self.assertEqual(len(result), 3)

    def test_quoted_and_labelled_fragments_are_not_plain_prose_evidence(self):
        for text in ('“Independent quotation.”', '"Independent quotation."'):
            self.assertEqual(len(self.merge(b=paragraph(text, page=4))), 2)
        for role in ("labelled_entry", "quotation", "diagram_node"):
            b = paragraph(page=4); b.role = role
            self.assertEqual(len(self.merge(b=b)), 2)

    def test_complete_list_entry_is_not_extended(self):
        a = paragraph(page=3, top=680); a.kind = "list_item"
        self.assertEqual(len(self.merge(a)), 2)

    def test_profile_inference_does_not_mutate_source_blocks(self):
        samples = training(); before = copy.deepcopy(samples)
        infer_paragraph_conventions(samples)
        self.assertEqual(samples, before)


def full_line(canvas, prefix, *, ending=".", width=238, font="Helvetica", size=10):
    text = prefix
    while canvas.stringWidth(text + " ordinary" + ending, font, size) < width:
        text += " ordinary"
    return text + ending


class CompleteSentencePdfTests(unittest.TestCase):
    def make(self, draw, *, convention="indent", scale=1, offset=0):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        path = Path(directory.name)/"structural.pdf"
        c = Canvas(str(path), pagesize=(PAGE[0]*scale+offset, PAGE[1]*scale+offset))
        # A PDF coordinate transform exercises extraction under scale/position.
        for page in range(2):
            c.saveState(); c.translate(offset, offset); c.scale(scale, scale)
            for i in range(3):
                top = 110+i*(42 if convention == "spacing" else 28)
                lines(c, [full_line(c, f"Reference {page+1}{i+1}")],
                      x=50+(12 if i and convention == "indent" else 0), top=top)
                lines(c, ["Its final reference statement."], top=top+14)
            c.restoreState(); c.showPage()
        draw(c, scale, offset)
        c.save()
        return path

    def target(self, c, scale, offset, *, indent=0, short=False, font="Helvetica", ending=".",
               hyphen=False, barrier=None, interlude=None):
        c.saveState(); c.translate(offset, offset); c.scale(scale, scale)
        a = [full_line(c, "Earlier information"),
             "A short ending." if short else full_line(c, "The completed sentence", ending=ending)]
        lines(c, a, top=680); c.restoreState(); c.showPage()
        if interlude:
            interlude(c); c.showPage()
        c.saveState(); c.translate(offset, offset); c.scale(scale, scale)
        if barrier:
            barrier(c)
        b = [full_line(c, "Capitalised next sentence begins here"), "and retains every word and symbol."]
        lines(c, [b[0]], x=50+indent, font=font)
        lines(c, [b[1]], top=124, font=font)
        if hyphen:
            lines(c, ["A separate ambiguous zor-", "blax follows."], x=62, top=152)
        c.restoreState()
        return " ".join(a+b)

    def test_actual_extraction_merges_complete_sentence_and_preserves_source_lines(self):
        expected=[]
        path=self.make(lambda c,s,o:expected.append(self.target(c,s,o)))
        result=convert_pdf(path)
        joined=next(b for b in result.blocks if b.text.startswith("Earlier information"))
        self.assertEqual(joined.text,expected[0])
        self.assertEqual([s.page for s in joined.sources],[3,4])
        self.assertEqual(len(joined.lines),4)
        self.assertIn(expected[0],result.markdown);self.assertIn(expected[0],result.text)

    def test_actual_new_indent_short_end_and_style_change_remain_separate(self):
        for options in (dict(indent=12),dict(short=True),dict(font="Courier")):
            with self.subTest(options=options):
                path=self.make(lambda c,s,o:self.target(c,s,o,**options))
                result=convert_pdf(path)
                self.assertFalse(any({3,4} <= {s.page for s in b.sources} for b in result.blocks))

    def test_actual_flush_or_spacing_conventions_remain_ambiguous(self):
        for convention in ("flush","spacing"):
            with self.subTest(convention=convention):
                path=self.make(self.target,convention=convention)
                result=convert_pdf(path)
                self.assertFalse(any({3,4} <= {s.page for s in b.sources} for b in result.blocks))

    def test_actual_scale_and_position(self):
        for scale,offset in ((.75,8),(1.3,17)):
            with self.subTest(scale=scale):
                path=self.make(self.target,scale=scale,offset=offset)
                result=convert_pdf(path,pages="3-4")
                self.assertTrue(any([s.page for s in b.sources]==[3,4] for b in result.blocks))

    def test_selected_sides_do_not_reconstruct_absent_content(self):
        path=self.make(self.target)
        for pages, present, absent in (("3","Earlier information","Capitalised next"),
                                       ("4","Capitalised next","Earlier information")):
            with self.subTest(pages=pages):
                result=convert_pdf(path,pages=pages)
                self.assertIn(present,result.text);self.assertNotIn(absent,result.text)
                self.assertTrue(all(s.page==int(pages) for b in result.blocks for s in b.sources))

    def test_selected_callbacks_and_warnings_are_local_and_not_duplicated(self):
        path=self.make(lambda c,s,o:self.target(c,s,o,hyphen=True))
        calls=[]
        result=convert_pdf(path,pages="3-4",hyphen_resolver=lambda issue:calls.append(issue) or False)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].page,4)
        result=convert_pdf(path,pages="3",hyphen_resolver=lambda _:self.fail("Unselected callback"))
        self.assertFalse(any(w.code=="ambiguous_hyphen" for w in result.warnings))
        result=convert_pdf(path,pages="4")
        self.assertEqual(sum(w.code=="ambiguous_hyphen" for w in result.warnings),1)

    def test_actual_column_transition(self):
        def draw(c,s,o):
            for i in range(42):
                lines(c,[full_line(c,"Left column information")],top=110+i*14)
            lines(c,[full_line(c,"Capitalised next sentence begins here"), "and ends exactly here."],x=318)
            for i in range(2):
                lines(c,[full_line(c,"Right separate paragraph")],x=330,top=138+i*28)
                lines(c,["Its separate final statement."],x=318,top=152+i*28)
        path=self.make(draw)
        result=convert_pdf(path,pages="3")
        b=next(b for b in result.blocks if b.text.startswith("Left column"))
        self.assertIn("Capitalised next",b.text)
        self.assertEqual([s.page for s in b.sources],[3,3])
        self.assertNotIn("Right separate",b.text)

    def test_actual_headings_lists_quotes_and_tables_are_barriers(self):
        def table(c):
            t=Table([["ITEM","COUNT"],["Rope","3"]],colWidths=[118,118],rowHeights=[15,15])
            t.setStyle(TableStyle([("GRID",(0,0),(-1,-1),.5,__import__('reportlab.lib.colors',fromlist=['black']).black)]))
            t.wrapOn(c,236,30);t.drawOn(c,50,PAGE[1]-90)
        for kind,draw in (("heading",lambda c:lines(c,["A Structural Heading"],top=80,size=14)),
                          ("list_item",lambda c:lines(c,["1. A separate list entry."],top=80)),
                          ("aside",lambda c:lines(c,["An independent literary quotation."],top=80,size=14,font="Helvetica-Oblique")),
                          ("table",table)):
            with self.subTest(kind=kind):
                path=self.make(lambda c,s,o:self.target(c,s,o,barrier=draw))
                result=convert_pdf(path,pages="3-4")
                self.assertTrue(any(b.kind==kind for b in result.blocks))
                self.assertFalse(any({3,4}<={s.page for s in b.sources} for b in result.blocks))

    def test_actual_form_remains_a_barrier_after_a_complete_sentence(self):
        from tests.test_form_inserts import draw_form
        path=self.make(lambda c,s,o:self.target(c,s,o,interlude=draw_form))
        result=convert_pdf(path,pages="3-5")
        self.assertTrue(any(b.role=="form_insert" for b in result.blocks))
        self.assertFalse(any({3,5}<={s.page for s in b.sources} for b in result.blocks))
        self.assertFalse(any(w.code=="insert_continuation" for w in result.warnings))

    def test_actual_omitted_page_and_intervening_main_prose(self):
        path=self.make(lambda c,s,o:self.target(c,s,o,interlude=lambda c:lines(
            c,["Intervening main prose stays here.","It is an independent source fragment."])))
        for pages in ("3,5","3-5"):
            with self.subTest(pages=pages):
                result=convert_pdf(path,pages=pages)
                self.assertFalse(any({3,5}<={s.page for s in b.sources} for b in result.blocks))
                first=next(b for b in result.blocks if b.text.startswith("Earlier information"))
                self.assertNotIn("Capitalised",first.text)
                self.assertEqual("Intervening main prose" in result.text,pages=="3-5")

    def test_actual_abbreviation_and_closing_quotes_or_brackets_preserve_exact_text(self):
        for ending in (" Dr.",'."',"!)","?]"):
            with self.subTest(ending=ending):
                expected=[]
                path=self.make(lambda c,s,o:expected.append(self.target(c,s,o,ending=ending)))
                result=convert_pdf(path,pages="3-4")
                self.assertEqual(result.text,expected[0]+"\n")
                self.assertEqual(result.markdown,render(list(result.blocks))[0])


class OptionalEndeavorsTests(unittest.TestCase):
    def test_verified_physical_boundary_and_selected_sides(self):
        path=Path(__file__).resolve().parents[1]/"example/14. Chapter 12 Endeavors.pdf"
        if not path.is_file():self.skipTest("Private Endeavors PDF unavailable")
        for pages in (None,"4-5","4","5","3-4,6"):
            with self.subTest(pages=pages):
                result=convert_pdf(path,pages=pages,hyphen_resolver=lambda _:False)
                joined=[b for b in result.blocks if b.text.startswith("The investigative web replaces")]
                if pages in (None,"4-5"):
                    self.assertEqual([s.page for s in joined[0].sources],[4,5])
                    self.assertIn("where those lead. The endeavor then ends",joined[0].text)
                    self.assertNotIn("Depending on the story",joined[0].text)
                elif pages!="5":
                    self.assertEqual([s.page for s in joined[0].sources],[4])
                    self.assertNotIn("The endeavor then ends",result.text)
                else:
                    self.assertFalse(joined)
                    self.assertTrue(result.text.startswith("The endeavor then ends"))


if __name__=="__main__":unittest.main()
