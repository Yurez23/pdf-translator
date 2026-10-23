"""PDF-03: geometry-backed forms own their text and preserve prose continuity."""

import tempfile
import unittest
import unicodedata
from collections import Counter
from pathlib import Path

import pdfplumber
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Table, TableStyle

from pdf_translator import convert_pdf
from pdf_translator.continuations import merge_continuations
from pdf_translator.export import render
from pdf_translator.models import Block, SourceRegion
from pdf_translator.models import Word
from pdf_translator.page_data import FormRegion, PageData
from pdf_translator.forms import attach_form_labels
from tests.test_continuations import prose
from tests.test_converter import PAGE, lines
from tests.test_symbols import draw_icon


FIELD_VALUES = ("Aster", "North", "7", "14", "Watchful", "3")
FIELD_LABELS = ("NAME", "ORIGIN", "LEVEL", "STRENGTH", "NOTES", "DEFENCE")


def draw_form(canvas, *, x=50, top=170, scale=1, table=False,
              duplicate=False, value_size=12, hyphen=False, labels=FIELD_LABELS):
    """Independent, differently sized panels, not a rectangular table grid."""
    def rectangle(left, y, width, height):
        canvas.rect(x + left * scale, PAGE[1] - top - (y + height) * scale,
                    width * scale, height * scale)
    rectangle(0, 0, 512, 500 if table else 320)
    panels = [(12, 18, 300, 80), (330, 18, 170, 80),
              (12, 118, 205, 80), (235, 118, 265, 80),
              (12, 218, 125, 80), (155, 218, 345, 80)]
    for i, (left, y, width, height) in enumerate(panels):
        rectangle(left, y, width, height)
        if duplicate:
            rectangle(left + 1, y + 1, width - 2, height - 2)
        lines(canvas, [labels[i]], x=x + (left + 8) * scale,
              top=top + (y + 15) * scale, font="Helvetica-Bold", size=8 * scale)
        value = FIELD_VALUES[i]
        if hyphen and i == 5:
            lines(canvas, ["fort-", "ress"], x=x + (left + 8) * scale,
                  top=top + (y + 40) * scale, size=value_size * scale,
                  leading=14 * scale, font="Helvetica-Oblique")
        else:
            lines(canvas, [value], x=x + (left + 8) * scale,
                  top=top + (y + 45) * scale, size=value_size * scale,
                  font="Helvetica-Oblique")
    if table:
        if table == "span":
            for y in (350, 376, 428):
                canvas.line(x + 30, PAGE[1] - top - y, x + 450, PAGE[1] - top - y)
            canvas.line(x + 240, PAGE[1] - top - 402, x + 450, PAGE[1] - top - 402)
            for left in (30, 240, 450):
                canvas.line(x + left, PAGE[1] - top - 350,
                            x + left, PAGE[1] - top - 428)
            for left, y, text in ((38, 367, "ITEM"), (248, 367, "COUNT"),
                                  (38, 405, "Rope"), (248, 393, "2"), (248, 419, "3")):
                lines(canvas, [text], x=x + left, top=top + y, size=9)
            return
        rows = [["ITEM", "COUNT"], ["Rope", "2"], ["Torch", ""]]
        style = [("GRID", (0, 0), (-1, -1), .6, "black"),
                 ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
        grid = Table(rows, colWidths=[210, 210], rowHeights=[26] * len(rows))
        grid.setStyle(TableStyle(style))
        grid.wrapOn(canvas, 420, 26 * len(rows))
        grid.drawOn(canvas, x + 30, PAGE[1] - top - 460)


def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.children)


def form_block(page=2, *, box=(50, 170, 562, 490)):
    child = Block("paragraph", "Form-owned value", (SourceRegion(page, box),))
    return Block("aside", child.text, child.sources, role="form_insert", children=[child])


class FormExtractionTests(unittest.TestCase):
    def test_caption_between_two_forms_has_no_ambiguous_double_owner(self):
        text = "A completed example records all selected starting values."
        tokens = text.split()
        width = sum(len(token)*7 for token in tokens)+4*(len(tokens)-1)
        cursor = (612-width)/2
        words = []
        for token in tokens:
            words.append(Word(token,(cursor,300,cursor+len(token)*7,312),12,"Helvetica-Oblique"))
            cursor += len(token)*7+4
        page = PageData(1,612,792,words,10,form_regions=[
            FormRegion((50,50,562,290),()),FormRegion((50,330,562,570),())])
        attach_form_labels(page)
        self.assertTrue(all(not form.attachments for form in page.form_regions))

    def convert(self, draw, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "form.pdf"
            canvas = Canvas(str(path), pagesize=PAGE)
            draw(canvas)
            canvas.save()
            return convert_pdf(path, **kwargs)

    def assert_form(self, result):
        forms = [b for b in result.blocks if b.role == "form_insert"]
        self.assertEqual(len(forms), 1)
        form = forms[0]
        self.assertEqual(form.kind, "aside")
        self.assertTrue(form.children)
        for value in FIELD_VALUES:
            self.assertIn(value, form.text)
        for label in FIELD_LABELS:
            self.assertIn(label, form.text)
            self.assertEqual(result.text.count(label), 1)
        self.assertFalse(any(w.code == "empty_page" for w in result.warnings))
        self.assertEqual(render(result.blocks), (result.markdown, result.text))
        return form

    def test_form_has_one_owner_and_excludes_outside_prose(self):
        def draw(c):
            lines(c, ["Independent prose above the form."], top=110)
            draw_form(c)
            lines(c, ["Independent prose below the form."], top=560)
        result = self.convert(draw)
        form = self.assert_form(result)
        self.assertNotIn("Independent prose", form.text)
        self.assertEqual(result.text.count("Independent prose"), 2)
        self.assertTrue(all(s.page == 1 for b in walk([form]) for s in b.sources))

    def test_form_geometry_survives_translation_scale_and_double_outlines(self):
        for scale, x, top, duplicate in [(1, 50, 170, True), (.78, 100, 230, False)]:
            with self.subTest(scale=scale, duplicate=duplicate):
                self.assert_form(self.convert(lambda c: draw_form(
                    c, x=x, top=top, scale=scale, duplicate=duplicate)))

    def test_form_labels_are_not_bound_to_english_words(self):
        labels = ("NOM", "ORIGINE", "NIVEAU", "FORCE", "REMARQUES", "DEFENSE")
        result = self.convert(lambda c: draw_form(c, labels=labels))
        forms = [b for b in result.blocks if b.role == "form_insert"]
        self.assertEqual(len(forms), 1)
        for label in labels:
            self.assertIn(label, forms[0].text)
            self.assertEqual(result.text.count(label), 1)

    def test_table_remains_structural_inside_form_with_empty_cell(self):
        result = self.convert(lambda c: draw_form(c, top=100, table=True))
        form = self.assert_form(result)
        tables = [b for b in walk(form.children) if b.kind == "table"]
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertIn(["Rope", "2"], table.rows)
        self.assertIn(["Torch", ""], table.rows)
        self.assertTrue(table.sources)
        self.assertEqual(result.text.count("Rope"), 1)
        self.assertIn("Rope\t2", result.text)
        self.assertIn("> |", result.markdown)

    def test_spanning_table_header_remains_in_form_with_its_scope(self):
        result = self.convert(lambda c: draw_form(c, top=100, table="span"))
        form = self.assert_form(result)
        tables = [b for b in walk(form.children) if b.kind == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows, [["ITEM", "COUNT"], ["Rope", "2"], ["Rope", "3"]])
        self.assertEqual(result.text.count("Rope"), 2)
        self.assertNotIn("complex_table", [w.code for w in result.warnings])

    def test_known_vector_symbols_keep_semantics_and_provenance_in_form(self):
        def draw(c):
            draw_form(c)
            draw_icon(c, "opportunity", 250, 430, extent=10)
            draw_icon(c, "complication", 290, 430, extent=10)
        result = self.convert(draw)
        form = self.assert_form(result)
        for identity in ("opportunity", "complication"):
            label = "[" + identity + "]"
            self.assertEqual(result.text.count(label), 1)
            self.assertIn(label, form.text)
            glyphs = [g for b in walk(form.children) for line in b.lines
                      for word in line.words for g in word.glyphs if g.symbol == identity]
            self.assertTrue(glyphs)
            self.assertTrue(all(g.origin == "vector" for g in glyphs))
        self.assertFalse(any(w.code == "unknown_game_symbol" for w in result.warnings))

    def test_repeated_top_fields_and_large_values_survive_margin_and_dropcap_filters(self):
        def draw(c):
            for index in range(3):
                draw_form(c, top=15, value_size=36)
                lines(c, [f"Different outside discussion number {index + 1}."], top=430)
                if index < 2:
                    c.showPage()
        result = self.convert(draw)
        forms = [b for b in result.blocks if b.role == "form_insert"]
        self.assertEqual(len(forms), 3)
        for form in forms:
            for label in FIELD_LABELS:
                self.assertIn(label, form.text)
            for value in FIELD_VALUES:
                self.assertIn(value, form.text)
        self.assertEqual(result.text.count("Shallan"), 0)
        self.assertEqual(result.text.count("NAME"), 3)

    def test_nearby_auxiliary_caption_attaches_but_real_heading_stays_outside(self):
        caption = "A completed example records the selected starting attributes."
        def draw(c):
            lines(c, ["This ordinary body paragraph establishes the normal reading size for the page.",
                      "The form example follows below with a caption in larger auxiliary lettering."], top=100)
            lines(c, ["Main Section"], top=155, size=16, font="Helvetica-Bold")
            draw_form(c)
            c.setFont("Helvetica-Oblique", 16)
            c.drawCentredString(306, PAGE[1] - 510, caption)
        result = self.convert(draw)
        form = self.assert_form(result)
        self.assertIn(caption, form.text)
        self.assertNotIn("Main Section", form.text)
        self.assertEqual(result.text.count(caption), 1)
        self.assertTrue(any(b.kind == "heading" and b.text == "Main Section"
                            for b in result.blocks))
        self.assertEqual(len(form.sources), 2)

    def test_same_page_main_text_band_crosses_form_end_to_end(self):
        def draw(c):
            lines(c, ["The ordinary rules explain how characters grow during"], top=140)
            draw_form(c)
            lines(c, ["character creation and how they use their starting skills."], top=540)
        result = self.convert(draw)
        self.assert_form(result)
        self.assertEqual([b.kind for b in result.blocks], ["paragraph", "aside"])
        self.assertIn("during character creation", result.blocks[0].text)
        self.assertEqual([s.page for s in result.blocks[0].sources], [1, 1])

    def test_hyphen_inside_form_is_resolved_once_for_both_exports(self):
        for choice, expected in [(True, "fortress"), (False, "fort-ress")]:
            issues = []
            with self.subTest(choice=choice):
                result = self.convert(lambda c: draw_form(c, hyphen=True),
                    hyphen_resolver=lambda issue: issues.append(issue) or choice)
                self.assertEqual(len(issues), 1)
                self.assertEqual(result.text.count(expected), 1)
                self.assertEqual(result.markdown.count(expected), 1)

    def test_fields_do_not_change_main_heading_levels_or_selection_context(self):
        def draw(c):
            lines(c, ["Main Chapter"], top=80, font="Helvetica-Bold", size=24)
            lines(c, ["A full ordinary paragraph establishes the body font."], top=120)
            c.showPage()
            draw_form(c, value_size=36)
            c.showPage()
            lines(c, ["Main Section"], top=80, font="Helvetica-Bold", size=16)
            lines(c, ["Ordinary explanatory text follows this section."], top=120)
        whole = self.convert(draw)
        selected = self.convert(draw, pages="3")
        self.assert_form(whole)
        headings = [b for b in whole.blocks if b.kind == "heading"]
        self.assertEqual([b.text for b in headings], ["Main Chapter", "Main Section"])
        self.assertEqual([b.level for b in headings], [1, 2])
        self.assertEqual(next(b.level for b in selected.blocks if b.kind == "heading"), 2)

    def test_uniform_table_and_card_grid_are_not_forms(self):
        def draw(c):
            grid = Table([["Label", "Value"], ["Speed", "10"], ["Weight", "20"],
                          ["Height", "30"]], colWidths=[180, 180], rowHeights=[45] * 4)
            grid.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), .7, "black")]))
            grid.wrapOn(c, 360, 180)
            grid.drawOn(c, 70, 400)
        result = self.convert(draw)
        self.assertFalse(any(b.role == "form_insert" for b in result.blocks))
        self.assertTrue(any(b.kind == "table" for b in result.blocks))

    def test_identical_cards_and_one_sidebar_are_not_forms(self):
        def draw(c):
            for row in range(3):
                for col in range(2):
                    x, top = 60 + col * 250, 120 + row * 100
                    c.rect(x, PAGE[1] - top - 80, 220, 80)
                    lines(c, [f"CARD {row * 2 + col + 1}"], x=x + 8, top=top + 20,
                          font="Helvetica-Bold")
                    lines(c, ["A complete short description."], x=x + 8, top=top + 44)
        result = self.convert(draw)
        self.assertFalse(any(b.role == "form_insert" for b in result.blocks))

    def test_short_labels_without_field_geometry_are_not_forms(self):
        def draw(c):
            for i, label in enumerate(FIELD_LABELS):
                lines(c, [label, FIELD_VALUES[i]], x=60 + (i % 2) * 250,
                      top=130 + (i // 2) * 100)
        result = self.convert(draw)
        self.assertFalse(any(b.role == "form_insert" for b in result.blocks))

    @staticmethod
    def interrupted_document(canvas):
        lines(canvas, ["The ordinary rules explain how characters grow during"], top=680)
        canvas.showPage()
        draw_form(canvas)
        canvas.showPage()
        lines(canvas, ["character creation and how they use their starting skills."], top=110)

    def test_end_to_end_paragraph_crosses_a_selected_form_only_page(self):
        result = self.convert(self.interrupted_document)
        form = self.assert_form(result)
        self.assertEqual([b.kind for b in result.blocks], ["paragraph", "aside"])
        self.assertIn("during character creation", result.blocks[0].text)
        self.assertEqual([s.page for s in result.blocks[0].sources], [1, 3])
        self.assertEqual({s.page for s in form.sources}, {2})
        self.assertLess(result.text.index("starting skills."), result.text.index("NAME"))

    def test_end_to_end_partial_page_selections_do_not_invent_context(self):
        for pages, form_count in [("2", 1), ("1-2", 1), ("2-3", 1), ("1,3", 0)]:
            with self.subTest(pages=pages):
                result = self.convert(self.interrupted_document, pages=pages)
                self.assertEqual(sum(b.role == "form_insert" for b in result.blocks), form_count)
                self.assertNotIn("during character creation", result.text)
                selected = {int(p) for p in pages.split(",")} if "," in pages else (
                    set(range(int(pages[0]), int(pages[-1]) + 1)))
                self.assertTrue(all(s.page in selected for b in walk(result.blocks) for s in b.sources))
                self.assertFalse(any(w.code in ("empty_page", "insert_continuation") for w in result.warnings))


class FormContinuationTests(unittest.TestCase):
    def merge(self, *blocks, selected_pages=None, insert_pages=None, resolver=None):
        warnings = []
        merged = merge_continuations(list(blocks), set(), warnings.append, resolver,
                                    selected_pages=selected_pages if selected_pages is not None else {1, 2, 3},
                                    insert_pages=insert_pages if insert_pages is not None else {2})
        return merged, warnings

    def test_selected_form_page_is_relocated_after_complete_paragraph(self):
        insert = form_block()
        merged, warnings = self.merge(prose("An unfinished sentence during"), insert,
                                     prose("character creation ends here.", page=3, top=100))
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].text, "An unfinished sentence during character creation ends here.")
        self.assertIs(merged[1], insert)
        self.assertEqual([s.page for s in merged[0].sources], [1, 3])
        self.assertEqual([s.page for s in insert.sources], [2])
        self.assertFalse(warnings)

    def test_omitted_form_page_remains_a_barrier(self):
        merged, warnings = self.merge(prose("An unfinished sentence during"),
                                     prose("character creation ends here.", page=3),
                                     selected_pages={1, 3})
        self.assertEqual(len(merged), 2)
        self.assertFalse(warnings)

    def test_complete_sentence_and_capital_after_comma_are_not_joined(self):
        for previous in ("A completed sentence.", "An unfinished line,", "A completed question?”"):
            with self.subTest(previous=previous):
                merged, warnings = self.merge(prose(previous), form_block(),
                    prose("Another unrelated paragraph.", page=3))
                self.assertEqual(len(merged), 3)
                self.assertFalse(warnings)

    def test_body_style_width_and_indent_require_agreement(self):
        for kwargs in ({"font": "Courier"}, {"font": "Helvetica-Bold"},
                       {"width": 170}, {"indent": 12}):
            with self.subTest(kwargs=kwargs):
                merged, _ = self.merge(prose("An unfinished sentence during"), form_block(),
                                       prose("character creation ends here.", page=3, **kwargs))
                self.assertEqual(len(merged), 3)

    def test_heading_list_table_quote_and_raw_are_barriers(self):
        for kind in ("heading", "list_item", "table", "aside", "raw"):
            with self.subTest(kind=kind):
                barrier = prose("Separate content", page=3, kind=kind)
                merged, _ = self.merge(prose("An unfinished sentence during"), form_block(),
                                       barrier, prose("character creation ends here.", page=3))
                self.assertEqual(len(merged), 4)

    def test_multiple_forms_keep_relative_order(self):
        first, second = form_block(2), form_block(3)
        merged, warnings = self.merge(prose("An unfinished sentence during"), first, second,
            prose("character creation ends here.", page=4), selected_pages={1, 2, 3, 4},
            insert_pages={2, 3})
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[1:], [first, second])
        self.assertEqual([s.page for s in merged[0].sources], [1, 4])
        self.assertFalse(warnings)

    def test_paragraph_can_continue_into_another_column_after_crossing_form(self):
        insert = form_block(2)
        merged, warnings = self.merge(prose("An unfinished sentence during"),insert,
            prose("character creation and more",page=3,top=100),
            prose("details ending here.",page=3,x=318,top=100))
        self.assertEqual(len(merged),2)
        self.assertEqual([s.page for s in merged[0].sources],[1,3,3])
        self.assertIn("more details",merged[0].text)
        self.assertIs(merged[1],insert)
        self.assertFalse(warnings)

    def test_already_crossed_form_does_not_warn_about_a_later_new_paragraph(self):
        merged, warnings = self.merge(prose("An unfinished sentence during"),form_block(2),
            prose("character creation and more",page=3,top=100),
            prose("another separately indented paragraph.",page=3,top=180,indent=12))
        self.assertEqual(len(merged),3)
        self.assertFalse(warnings)

    def test_missing_page_inside_chain_is_a_barrier(self):
        merged, warnings = self.merge(prose("An unfinished sentence during"), form_block(2),
            form_block(4), prose("character creation ends here.", page=5),
            selected_pages={1, 2, 4, 5}, insert_pages={2, 4})
        self.assertEqual(len(merged), 4)
        self.assertFalse(warnings)

    def test_unmarked_mixed_page_prose_cannot_be_skipped(self):
        merged, _ = self.merge(prose("An unfinished sentence during"), form_block(),
                              prose("A separate discussion.", page=2),
                              prose("character creation ends here.", page=3), insert_pages=set())
        self.assertEqual(len(merged), 4)

    def test_same_column_form_band_can_be_crossed_on_one_page(self):
        insert = form_block(1, box=(50, 200, 290, 400))
        merged, warnings = self.merge(prose("An unfinished sentence during", top=170), insert,
            prose("character creation ends here.", top=420), selected_pages={1}, insert_pages=set())
        self.assertEqual(len(merged), 2)
        self.assertIs(merged[1], insert)
        self.assertEqual([s.page for s in merged[0].sources], [1, 1])
        self.assertFalse(warnings)

    def test_form_overlapping_prose_column_rejects_same_page_route(self):
        merged, _ = self.merge(prose("An unfinished sentence during", top=170),
            form_block(1, box=(170, 160, 400, 450)),
            prose("character creation ends here.", top=420), selected_pages={1}, insert_pages=set())
        self.assertEqual(len(merged), 3)

    def test_attached_caption_cannot_overlap_the_preceding_body_fragment(self):
        insert = form_block(1, box=(50,200,290,400))
        insert.sources += (SourceRegion(1,(50,180,290,198)),)
        merged, _ = self.merge(prose("An unfinished sentence during",top=185),insert,
            prose("character creation ends here.",top=420),selected_pages={1},insert_pages=set())
        self.assertEqual(len(merged),3)

    def test_ambiguous_hyphen_boundary_callback_uses_continuation_page_once(self):
        for choice, expected in [(True, "fortress"), (False, "fort-ress")]:
            issues = []
            with self.subTest(choice=choice):
                merged, warnings = self.merge(prose("A remote fort-"), form_block(),
                    prose("ress guards this road.", page=3),
                    resolver=lambda issue: issues.append(issue) or choice)
                self.assertEqual(len(merged), 2)
                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0].page, 3)
                self.assertIn(expected, merged[0].text)
                self.assertFalse(warnings)

    def test_short_last_line_or_changed_size_is_not_joined(self):
        for short in (True, False):
            previous = prose("An unfinished sentence during")
            following = prose("character creation ends here.", page=3)
            if short:
                previous.lines[0].bbox = (50, 680, 140, 690)
            else:
                following.lines[0].size = 14
            with self.subTest(short=short):
                merged, _ = self.merge(previous, form_block(), following)
                self.assertEqual(len(merged), 3)

    def test_a_list_item_before_the_form_does_not_use_insert_branch(self):
        merged, _ = self.merge(prose("1. An unfinished list entry during", kind="list_item"),
                               form_block(), prose("character creation ends here.", page=3))
        self.assertEqual(len(merged), 3)

    def test_subset_font_prefixes_and_open_bracket_preserve_strong_continuation(self):
        merged, warnings = self.merge(prose("See (the earlier discussion of",
                                                   font="AAAAAA+Helvetica"), form_block(),
            prose('"Companions" section).', page=3, font="BBBBBB+Helvetica"))
        self.assertEqual(len(merged), 2)
        self.assertIn('of "Companions" section).', merged[0].text)
        self.assertFalse(warnings)

    def test_insert_flat_search_text_is_never_exported_twice(self):
        insert = form_block()
        insert.text = "Flat representation is not another output block"
        md, txt = render([insert])
        self.assertNotIn("Flat representation", md + txt)
        self.assertEqual(txt.count("Form-owned value"), 1)
        self.assertEqual(md.count("Form-owned value"), 1)

    def test_nested_quotation_after_form_remains_a_barrier(self):
        quotation = Block("aside", "Independent quotation.", (SourceRegion(3, (50, 60, 290, 90)),),
                          role="quotation", children=[prose("Independent quotation.", page=3)])
        merged, warnings = self.merge(prose("An unfinished sentence during"), form_block(), quotation,
                                     prose("character creation ends here.", page=3))
        self.assertEqual(len(merged), 4)
        self.assertFalse(warnings)

    def test_geometry_conflict_emits_one_warning_for_whole_insert_chain(self):
        merged, warnings = self.merge(prose("An unfinished sentence during"), form_block(2), form_block(3),
            prose("character creation ends here.", page=4, width=170),
            selected_pages={1, 2, 3, 4}, insert_pages={2, 3})
        self.assertEqual(len(merged), 4)
        self.assertEqual([w.code for w in warnings], ["insert_continuation"])
        self.assertEqual(warnings[0].pages, (1, 2, 3, 4))


LOCAL_CHARACTER_PDF = Path(__file__).resolve().parents[1] / "example/3. Chapter 1 Character Creation.pdf"


@unittest.skipUnless(LOCAL_CHARACTER_PDF.is_file(), "Optional local Character Creation PDF absent")
class OptionalCharacterFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.choices = []
        cls.selected = convert_pdf(LOCAL_CHARACTER_PDF, pages="4-6",
            hyphen_resolver=lambda issue: cls.choices.append(issue) or False)
        cls.form_only = convert_pdf(LOCAL_CHARACTER_PDF, pages="5", hyphen_resolver=lambda _: False)

    def test_real_interrupted_paragraph_has_only_main_text_source_pages(self):
        joined = [b for b in self.selected.blocks if "during character creation, but" in b.text]
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0].kind, "paragraph")
        self.assertEqual([s.page for s in joined[0].sources], [4, 6])
        forms = [b for b in self.selected.blocks if b.role == "form_insert"]
        self.assertEqual(len(forms), 1)
        self.assertEqual({s.page for s in forms[0].sources}, {5})
        self.assertLess(self.selected.blocks.index(joined[0]), self.selected.blocks.index(forms[0]))
        self.assertTrue(all(issue.page in (4, 5, 6) for issue in self.choices))
        self.assertEqual(len({(i.page, i.context) for i in self.choices}), len(self.choices))
        self.assertFalse(any(w.code == "empty_page" and 5 in w.pages for w in self.selected.warnings))

    def test_real_form_matches_all_raw_printed_characters_except_page_footer(self):
        # This independently reads the source text, not a previous converter output
        # or the detector's own region bounds. Physical p5 is a standalone form;
        # its only text below 730pt is the chapter/page running footer.
        with pdfplumber.open(LOCAL_CHARACTER_PDF, unicode_norm="NFC") as pdf:
            raw = "".join(char["text"] for char in pdf.pages[4].chars if char["top"] < 730)
        def characters(text):
            ligatures = str.maketrans({"ﬀ":"ff", "ﬁ":"fi", "ﬂ":"fl", "ﬃ":"ffi", "ﬄ":"ffl", "ﬅ":"st", "ﬆ":"st"})
            return Counter(c for c in unicodedata.normalize("NFC", text.translate(ligatures)) if not c.isspace())
        form = next(b for b in self.form_only.blocks if b.role == "form_insert")
        self.assertEqual(characters(self.form_only.text), characters(raw))
        self.assertEqual(len(self.form_only.blocks), 1)
        self.assertIn("Shallan", form.text)
        self.assertIn("physical cognitive", form.text)
        self.assertIn("spiritual", form.text)
        for field in ("defense 11", "defense 15", "defense 16", "defleCt 1"):
            self.assertTrue(any(b.text == field for b in walk(form.children)), field)
        self.assertEqual(sum(b.kind == "aside" for b in form.children), 3)
        self.assertIn("The fronT of your compleTed characTer sheeT", form.text)
        self.assertTrue(any(b.kind == "table" for b in walk(form.children)))
        self.assertTrue(all(s.page == 5 for b in walk([form]) for s in b.sources))
        self.assertEqual(render(self.form_only.blocks), (self.form_only.markdown, self.form_only.text))
        self.assertFalse(any(w.code in ("empty_page", "insert_continuation") for w in self.form_only.warnings))


if __name__ == "__main__":
    unittest.main()
