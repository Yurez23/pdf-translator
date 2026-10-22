"""Synthetic painted-table regressions plus optional, read-only local examples."""

from collections import Counter
from copy import deepcopy
import math
from pathlib import Path
from types import SimpleNamespace
import unittest

from pdf_translator.geometry import contains
from pdf_translator.models import Word
from pdf_translator.page_data import PageData, TableData
from pdf_translator.table_recovery import recover_painted_tables

ROOT = Path(__file__).resolve().parents[1]


def rectangle(box, *, fill=True, stroke=False):
    return dict(zip(("x0", "top", "x1", "bottom"), box), fill=fill, stroke=stroke)


def cell_words(text, box, *, size=8):
    result = []
    for index, line in enumerate(text.splitlines()):
        x, y = box[0]+4, box[1]+3+index*(size+1)
        for token in line.split():
            width = len(token)*size*.42
            result.append(Word(token, (x, y, x+width, y+size), size, "ExampleFont"))
            x += width+size*.25
    return result


def painted_fixture(*, tail=True):
    xs = [40, 90, 155, 215, 300]
    bands = [(100, 124), (124, 140), (140, 156), (156, 172)]
    rows = [
        ["Kind", "Travel\nrate", "Load\nlimit", "Cost"],
        ["Alpha", "2 mi", "120 lb", "18 cr"],
        ["Beta", "4 mi", "50 lb", "400 cr"],
        ["Gamma", "6 mi", "100 lb", "Reward"],
    ]
    rects, words = [], []
    for r, (top, bottom) in enumerate(bands):
        for c, (left, right) in enumerate(zip(xs, xs[1:])):
            box = (left, top, right, bottom)
            if r in (0, 2):
                rects.append(rectangle(box))
            if r != 3 or tail:
                words.extend(cell_words(rows[r][c], box))
    return SimpleNamespace(rects=rects, curves=[]), words, rows


def grid_fixture(*, groups=3, per_group=3):
    width = groups*per_group*26
    strip = (40, 100, 40+width, 112)
    rects = [rectangle(strip)]
    curves, words = [], []
    labels, values = [], []
    for group in range(groups):
        name = ["North", "East", "South", "West"][group]
        centre = 40+(group+.5)*per_group*26
        words.append(Word(name, (centre-10, 102, centre+10, 111), 9))
        for column in range(per_group):
            x = 40+(group*per_group+column+.5)*26
            label = chr(ord("a")+column)*2
            value = str(group*per_group+column+1)
            labels.append(label)
            values.append(value)
            words.append(Word(label, (x-4, 112.5, x+4, 121.5), 9))
            words.append(Word(value, (x-4, 123, x+4, 132), 9))
            if column == per_group//2:
                curves.append(rectangle((x-10, 110.5, x+10, 136), fill=False, stroke=True))
    return SimpleNamespace(rects=rects, curves=curves), words, labels, values


def source_tokens(words):
    return Counter(token for word in words for token in word.text.split())


def table_tokens(tables):
    return Counter(token for table in tables for row in table.rows for cell in row if cell
                   for token in cell.split())


class PaintedCellTests(unittest.TestCase):
    def test_recovers_multiline_header_gap_and_one_trailing_row(self):
        page, words, rows = painted_fixture()
        tables = recover_painted_tables(page, words, [])
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].rows, rows)
        self.assertEqual(tables[0].bbox, (40, 100, 300, 172))
        self.assertFalse(tables[0].repaired_gaps)
        self.assertEqual(table_tokens(tables), source_tokens(words))

    def test_contiguous_explicit_bands_need_no_numeric_inference(self):
        xs = [10, 60, 110, 160]
        rows = [["Name", "Place", "Kind"], ["Alpha", "North", "Stone"]]
        rects, words = [], []
        for r, row in enumerate(rows):
            for i, text in enumerate(row):
                box = (xs[i], 100+r*16, xs[i+1], 116+r*16)
                rects.append(rectangle(box))
                words.extend(cell_words(text, box))
        tables = recover_painted_tables(SimpleNamespace(rects=rects, curves=[]), words, [])
        self.assertEqual([table.rows for table in tables], [rows])

    def test_scaled_translated_geometry_and_different_text(self):
        page, words, rows = painted_fixture()
        def transformed(box):
            return tuple(v*1.4+(17 if i % 2 == 0 else 23) for i, v in enumerate(box))
        page.rects = [rectangle(transformed((r['x0'], r['top'], r['x1'], r['bottom']))) for r in page.rects]
        words = [Word(w.text, transformed(w.bbox), w.size*1.4, w.font) for w in words]
        tables = recover_painted_tables(page, words, [])
        self.assertEqual([table.rows for table in tables], [rows])

    def test_replaces_contained_single_row_fragments_without_losing_text(self):
        page, words, rows = painted_fixture()
        existing = [TableData((40, 100, 300, 124), [rows[0]]),
                    TableData((40, 140, 300, 156), [rows[2]])]
        snapshot = deepcopy((page, words, existing))
        tables = recover_painted_tables(page, words, existing)
        self.assertEqual([table.rows for table in tables], [rows])
        self.assertEqual((page, words, existing), snapshot)

    def test_complete_existing_table_wins_and_recovery_is_idempotent(self):
        page, words, rows = painted_fixture()
        existing = TableData((40, 100, 300, 172), deepcopy(rows))
        result = recover_painted_tables(page, words, [existing])
        self.assertEqual(len(result), 1)
        self.assertIs(result[0], existing)
        again = recover_painted_tables(page, words, result)
        self.assertIs(again[0], existing)
        recovered = recover_painted_tables(page, words, [])
        self.assertIs(recover_painted_tables(page, words, recovered)[0], recovered[0])

    def test_disjoint_existing_table_is_preserved(self):
        page, words, _ = painted_fixture()
        existing = TableData((350, 100, 500, 150), [["A", "B"], ["C", "D"]])
        result = recover_painted_tables(page, words, [existing])
        self.assertEqual(len(result), 2)
        self.assertTrue(any(t is existing for t in result))

    def test_overlap_extending_beyond_candidate_is_not_replaced_or_duplicated(self):
        page, words, _ = painted_fixture()
        existing = TableData((30, 100, 300, 200), [["Alpha", "2 mi"]])
        self.assertEqual(recover_painted_tables(page, words, [existing]), [existing])

    def test_unrepresented_existing_token_blocks_replacement(self):
        page, words, _ = painted_fixture()
        existing = TableData((40, 100, 300, 124), [["Unknown source text", "Kind"]])
        self.assertEqual(recover_painted_tables(page, words, [existing]), [existing])

    def test_existing_token_multiplicity_is_preserved(self):
        page, words, rows = painted_fixture()
        a = TableData((40, 100, 300, 124), [rows[0]])
        b = TableData((40, 100, 300, 124), [rows[0]])
        self.assertEqual(recover_painted_tables(page, words, [a, b]), [a, b])

    def test_duplicate_underpaint_does_not_duplicate_cells(self):
        page, words, rows = painted_fixture()
        page.rects += deepcopy(page.rects)
        page.rects += [rectangle((40, 100, 300, 124)), rectangle((40, 140, 300, 156))]
        result = recover_painted_tables(page, words, [])
        self.assertEqual([t.rows for t in result], [rows])

    def test_incomplete_intervening_row_declines_whole_candidate(self):
        page, words, _ = painted_fixture()
        words = [w for w in words if not (124 < w.bbox[1] < 140 and w.bbox[0] > 215)]
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_missing_explicit_cell_declines_whole_candidate(self):
        page, words, _ = painted_fixture()
        words = [w for w in words if not (140 < w.bbox[1] < 156 and w.bbox[0] > 215)]
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_incomplete_tail_stays_available_to_ordinary_layout(self):
        page, words, rows = painted_fixture()
        words = [w for w in words if not (w.bbox[1] > 156 and w.bbox[0] > 215)]
        result = recover_painted_tables(page, words, [])
        self.assertEqual([t.rows for t in result], [rows[:3]])
        unclaimed = [w for w in words if not contains(result[0].bbox, w)]
        self.assertEqual(source_tokens(unclaimed), Counter("Gamma 6 mi 100 lb".split()))
        self.assertEqual(table_tokens(result)+source_tokens(unclaimed), source_tokens(words))

    def test_tail_is_not_extrapolated_twice(self):
        page, words, rows = painted_fixture()
        extra = []
        for i, text in enumerate(["Delta", "8 mi", "90 lb", "800 cr"]):
            xs = [40, 90, 155, 215, 300]
            extra.extend(cell_words(text, (xs[i], 172, xs[i+1], 188)))
        result = recover_painted_tables(page, words+extra, [])
        self.assertEqual([t.rows for t in result], [rows])
        self.assertTrue(all(not contains(result[0].bbox, w) for w in extra))

    def test_arbitrary_prose_after_table_is_not_a_tail(self):
        page, words, rows = painted_fixture(tail=False)
        for i, text in enumerate(["Here", "is more", "ordinary", "prose"]):
            xs = [40, 90, 155, 215, 300]
            words.extend(cell_words(text, (xs[i], 156, xs[i+1], 172)))
        result = recover_painted_tables(page, words, [])
        self.assertEqual([t.rows for t in result], [rows[:3]])

    def test_misaligned_tail_is_not_claimed(self):
        page, words, rows = painted_fixture()
        for w in words:
            if w.bbox[1] > 156 and w.bbox[0] < 90:
                a, b, c, d = w.bbox
                w.bbox = (a+8, b, c+8, d)
        result = recover_painted_tables(page, words, [])
        self.assertEqual([t.rows for t in result], [rows[:3]])

    def test_changed_tail_font_size_is_not_claimed(self):
        page, words, rows = painted_fixture()
        for w in words:
            if w.bbox[1] > 156:
                w.size = 11
        result = recover_painted_tables(page, words, [])
        self.assertEqual([t.rows for t in result], [rows[:3]])

    def test_long_gap_does_not_bridge_independent_regions(self):
        page, words, _ = painted_fixture()
        for r in page.rects[4:]:
            r['top'] += 16
            r['bottom'] += 16
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_word_crossing_column_boundary_declines_candidate(self):
        page, words, _ = painted_fixture()
        words.append(Word("Spanning", (80, 143, 120, 151), 8))
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_ambiguous_boundary_centre_declines_candidate(self):
        page, words, _ = painted_fixture()
        words.append(Word("x", (89.5, 143, 90.5, 151), 8))
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_layout_ownership_tolerance_cannot_swallow_unassigned_word(self):
        page, words, _ = painted_fixture()
        words.append(Word("x", (37, 110, 41.5, 118), 8))
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_rotated_or_non_upright_text_declines_candidate(self):
        for field, value in [("upright", False), ("angle", 180)]:
            with self.subTest(field=field):
                page, words, _ = painted_fixture()
                setattr(words[0], field, value)
                self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_single_painted_row_is_insufficient(self):
        page, words, _ = painted_fixture()
        page.rects = page.rects[:4]
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_framed_sidebar_and_plain_paragraphs_are_not_tables(self):
        box = (40, 100, 300, 300)
        words = []
        for i in range(9):
            words.extend(cell_words("A paragraph inside a frame.", (40, 105+i*18, 300, 125+i*18)))
        for rects in ([], [rectangle(box)], [rectangle(box, fill=False, stroke=True)]):
            with self.subTest(rects=rects):
                self.assertEqual(recover_painted_tables(SimpleNamespace(rects=rects, curves=[]), words, []), [])

    def test_separated_card_panels_are_not_table_cells(self):
        rects, words = [], []
        for r in range(2):
            for c in range(4):
                box = (40+c*70, 100+r*50, 90+c*70, 135+r*50)
                rects.append(rectangle(box))
                words.extend(cell_words("Card\n2 units", box))
        page = SimpleNamespace(rects=rects, curves=[])
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_full_width_row_fills_without_cell_geometry_are_insufficient(self):
        page, words, _ = painted_fixture()
        page.rects = [rectangle((40, 100, 300, 124)), rectangle((40, 140, 300, 156))]
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_malformed_and_stroked_only_shapes_are_ignored(self):
        page, words, _ = painted_fixture()
        page.rects = [dict(x0=0), rectangle((0, 0, math.inf, 20)),
                      rectangle((0, 0, 100, 0)), *[dict(r, fill=False, stroke=True) for r in page.rects]]
        self.assertEqual(recover_painted_tables(page, words, []), [])


class GroupedGridTests(unittest.TestCase):
    def test_retains_all_groups_labels_values_and_explicit_merged_spans(self):
        page, words, labels, values = grid_fixture()
        result = recover_painted_tables(page, words, [])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].rows, [
            ["North", None, None, "East", None, None, "South", None, None], labels, values])
        self.assertEqual(table_tokens(result), source_tokens(words))

    def test_supports_varied_group_and_cell_counts(self):
        for groups, count in [(2, 2), (2, 4), (4, 2), (4, 5)]:
            with self.subTest(groups=groups, count=count):
                page, words, labels, values = grid_fixture(groups=groups, per_group=count)
                result = recover_painted_tables(page, words, [])
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].rows[1:], [labels, values])
                self.assertEqual(table_tokens(result), source_tokens(words))

    def test_partial_ruling_table_is_replaced_and_inputs_are_unchanged(self):
        page, words, labels, values = grid_fixture()
        existing = [TableData((41, 100, page.rects[0]["x1"], 134), [["North East South", None],
                    [labels[0]+"\n"+values[0], labels[-1]+"\n"+values[-1]]])]
        before = deepcopy((page, words, existing))
        result = recover_painted_tables(page, words, existing)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].rows[1:], [labels, values])
        self.assertEqual((page, words, existing), before)

    def test_confirmed_grid_exports_composite_headers_without_raw_fallback(self):
        from pdf_translator.layout import _table_blocks
        page, words, _, _ = grid_fixture()
        data = PageData(1, 612, 792, words, 9, tables=recover_painted_tables(page, words, []))
        warnings = []
        blocks = _table_blocks(data, set(), warnings.append)
        self.assertEqual([b.kind for b in blocks], ["table"])
        self.assertEqual(warnings, [])
        self.assertEqual(blocks[0].rows, [
            [f"{group} / {label}" for group in ("North", "East", "South") for label in ("aa", "bb", "cc")],
            [str(i) for i in range(1, 10)]])

    def test_no_outline_corroboration_is_insufficient(self):
        page, words, _, _ = grid_fixture()
        page.curves = []
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_missing_group_outline_is_insufficient(self):
        page, words, _, _ = grid_fixture()
        page.curves.pop()
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_misaligned_label_number_pair_declines(self):
        page, words, _, _ = grid_fixture()
        value = next(w for w in words if w.text == "5")
        a, b, c, d = value.bbox
        value.bbox = (a+8, b, c+8, d)
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_missing_value_declines_without_inventing_it(self):
        page, words, _, _ = grid_fixture()
        words = [w for w in words if w.text != "5"]
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_non_numeric_value_declines(self):
        page, words, _, _ = grid_fixture()
        next(w for w in words if w.text == "5").text = "unknown"
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_unaligned_group_heading_declines(self):
        page, words, _, _ = grid_fixture()
        words[0].bbox = (41, 102, 61, 111)
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_extra_text_inside_grid_declines_instead_of_losing_it(self):
        page, words, _, _ = grid_fixture()
        words.append(Word("Footnote", (43, 132, 74, 136), 4))
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_prose_with_numbers_but_no_painted_header_declines(self):
        page, words, _, _ = grid_fixture()
        page.rects = []
        self.assertEqual(recover_painted_tables(page, words, []), [])

    def test_normal_filled_cards_cannot_substitute_for_stroked_cell_outlines(self):
        page, words, _, _ = grid_fixture()
        page.curves = [dict(r, fill=True) for r in page.curves]
        self.assertEqual(recover_painted_tables(page, words, []), [])


def local_page_data(page):
    """Use the reader's space repair, never OCR or provider calls."""
    from pdf_translator.pdf_reader import _overprinted_spaces
    from pdf_translator.text import normalize
    clean = page.dedupe_chars(tolerance=.5, extra_attrs=("fontname", "size"))
    ignored = _overprinted_spaces(clean.chars)
    if ignored:
        clean = clean.filter(lambda obj: id(obj) not in ignored)
    words = []
    for item in clean.extract_words(x_tolerance=2, x_tolerance_ratio=.18, y_tolerance=3,
                                    return_chars=True, expand_ligatures=True):
        chars = item.get("chars", [])
        size = Counter(round(c["size"], 1) for c in chars).most_common(1)[0][0] if chars else 10
        text = normalize(item["text"])
        if text:
            words.append(Word(text, tuple(item[k] for k in ("x0", "top", "x1", "bottom")), size,
                              upright=item.get("upright", True)))
    existing = [TableData(table.bbox, table.extract(x_tolerance=2, y_tolerance=3))
                for table in clean.find_tables()]
    return clean, words, existing


class OptionalLocalReproductionTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / "example/9. Chapter 7 Items.pdf").is_file(), "Optional local PDF absent")
    def test_items_physical_page_21_mounts(self):
        import pdfplumber
        with pdfplumber.open(ROOT / "example/9. Chapter 7 Items.pdf", unicode_norm="NFC") as pdf:
            page, words, existing = local_page_data(pdf.pages[20])
            result = recover_painted_tables(page, words, existing)
            tables = [t for t in result if t.bbox[0] < 60 and 590 < t.bbox[1] < 600]
            self.assertEqual(len(tables), 1)
            self.assertEqual(tables[0].rows, [
                ["Mount", "Travel Speed", "Carrying\nCapacity", "Purchase\nPrice"],
                ["Chull", "2 mph", "1,500 lb.", "200 mk"],
                ["Horse", "4 mph", "500 lb.", "4,000 mk"],
                ["Ryshadium", "6 mph", "1,000 lb.", "Reward only"],
            ])
            self.assertEqual(table_tokens(tables), source_tokens([w for w in words if contains(tables[0].bbox, w)]))
            for old in existing:
                if old.bbox[1] < 590:
                    self.assertTrue(any(t is old for t in result))

    def _check_local_grid(self, name, number, box, expected):
        import pdfplumber
        with pdfplumber.open(ROOT / "example" / name, unicode_norm="NFC") as pdf:
            page, words, existing = local_page_data(pdf.pages[number-1])
            result = recover_painted_tables(page, words, existing)
            tables = [t for t in result if abs(t.bbox[0]-box[0]) < 3 and abs(t.bbox[1]-box[1]) < 3]
            self.assertEqual(len(tables), 1)
            self.assertEqual(tables[0].rows, [
                ["Physical", None, None, "Cognitive", None, None, "Spiritual", None, None],
                ["str", "def", "spd", "int", "def", "wil", "awa", "def", "pre"], expected])
            self.assertEqual(table_tokens(tables), source_tokens([w for w in words if contains(tables[0].bbox, w)]))

    @unittest.skipUnless((ROOT / "example/17. Appendix Animal Companions.pdf").is_file(), "Optional local PDF absent")
    def test_companion_physical_page_1_grid(self):
        self._check_local_grid("17. Appendix Animal Companions.pdf", 1, (318, 324),
                               ["2", "14", "2", "0", "10", "0", "3", "13", "0"])

    @unittest.skipUnless((ROOT / "example/15. Chapter 13 Gamemastering.pdf").is_file(), "Optional local PDF absent")
    def test_gamemastering_physical_page_7_grid(self):
        self._check_local_grid("15. Chapter 13 Gamemastering.pdf", 7, (57, 92),
                               ["1", "14", "3", "2", "13", "1", "2", "13", "1"])


if __name__ == "__main__":
    unittest.main()
