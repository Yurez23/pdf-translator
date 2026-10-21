"""Offline card-panel regressions, independent of the optional local books.

Only text containment is required: connectors, action icons and graph topology
are deliberately outside these tests' contract. Run with standard unittest.
"""

from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import re
import tempfile
import unicodedata
import unittest

import pdfplumber
from reportlab.lib import colors
from reportlab.pdfgen.canvas import Canvas

from pdf_translator import convert_pdf
from pdf_translator.diagrams import card_panels
from pdf_translator.export import render
from pdf_translator.layout import layout_page
from pdf_translator.models import Word
from pdf_translator.page_data import PageData
from pdf_translator.pdf_reader import _read_page


ROOT = Path(__file__).resolve().parents[1]
PAGE = (612, 792)
TITLES = ("Amber Vigil", "Bronze Step", "Cobalt Guard", "Dawn Circle",
          "Ember Thread", "Frost Ward")
OUTSIDE = "Ordinary prose outside the cards remains independent."
LIGATURES = str.maketrans({"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
                          "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"})


def canonical(text):
    """Allow NFC, ligature expansion, whitespace and discretionary hyphens only.

    Keep visible line-end hyphens, as the tests choose False for ambiguous
    hyphenation. Do not fold case, discard punctuation/numbers or match fuzzily.
    """
    text = unicodedata.normalize("NFC", text.translate(LIGATURES))
    text = text.replace("\u00ad\n", "").replace("\u00ad", "")
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "-", text)
    return " ".join(text.split())


@dataclass(frozen=True)
class Card:
    bbox: tuple[float, float, float, float]
    title: str
    lines: tuple[str, ...]

    @property
    def text(self):
        return " ".join((self.title, *self.lines))


def cards(*, width=114, height=100, dx=124, dy=126):
    result = []
    for index, title in enumerate(TITLES):
        row, column = divmod(index, 2)
        x, top = 318 + column * dx, 160 + row * dy
        result.append(Card((x, top, x + width, top + height), title, (
            f"Prerequisite: Rank {index + 1}+",
            f"Spend {index + 11} focus to gain",
            f"a +{index + 2} bonus for {index + 21} feet.",
        )))
    return result


def shape(box, *, fill=True, color=(1, 1, 1)):
    return dict(zip(("x0", "top", "x1", "bottom"), box), fill=fill,
                non_stroking_color=color)


def detector_input(specs):
    """Simple geometry isolates the detector from PDF parsing and layout."""
    words = []
    for card in specs:
        x, y, _, _ = card.bbox
        words.append(Word(card.title, (x + 5, y + 5, x + 65, y + 14), 9,
                          "Helvetica-Bold"))
        words.append(Word(" ".join(card.lines), (x + 5, y + 20, x + 70, y + 29),
                          8.5, "Helvetica"))
    return PageData(1, *PAGE, words, 8.5), [shape(card.bbox) for card in specs]


def draw_line(canvas, text, x, top, *, bold=False, size=8.5):
    canvas.setFillColor(colors.black)
    canvas.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    canvas.drawString(x, PAGE[1] - top, text)


def draw_cards(canvas, specs, *, connectors=False, backplate=False):
    if backplate:
        canvas.setFillColor(colors.lightgrey)
        canvas.rect(308, PAGE[1] - 530, 258, 380, stroke=0, fill=1)
    if connectors:
        # Lines behind opaque cards are visible only in the gutters, but a
        # ruling detector sees them through the panels and invents cells.
        canvas.setStrokeColor(colors.black)
        for x in (375, 499):
            canvas.line(x, PAGE[1] - 160, x, PAGE[1] - 512)
        for y in (160, 260, 286, 386, 412, 512):
            canvas.line(318, PAGE[1] - y, 556, PAGE[1] - y)
    for card in specs:
        x0, top, x1, bottom = card.bbox
        canvas.setFillColor(colors.white)
        canvas.setStrokeColor(colors.black)
        canvas.rect(x0, PAGE[1] - bottom, x1 - x0, bottom - top, fill=1)
        draw_line(canvas, card.title, x0 + 5, top + 16, bold=True, size=9)
        for text, offset in zip(card.lines, (34, 54, 66)):
            draw_line(canvas, text, x0 + 5, top + offset)


def draw_table(canvas, *, top=600, x=50, width=210, row_height=40):
    """A real 3-by-2 white table, with styled text in every filled cell."""
    expected = []
    for index in range(6):
        row, column = divmod(index, 2)
        left, cell_top = x + column * width / 2, top + row * row_height
        canvas.setFillColor(colors.white)
        canvas.rect(left, PAGE[1] - cell_top - row_height,
                    width / 2, row_height, fill=1)
        title, value = f"Price Band {index + 1}", f"Value {index + 41} silver pieces."
        draw_line(canvas, title, left + 4, cell_top + 14, bold=True)
        draw_line(canvas, value, left + 4, cell_top + 28, size=8)
        expected.append((title, value))
    return expected


def fixture_pdf(draw):
    stream = BytesIO()
    canvas = Canvas(stream, pagesize=PAGE, invariant=1)
    draw(canvas)
    canvas.save()
    return stream.getvalue()


def read_fixture(payload):
    warnings = []
    with pdfplumber.open(BytesIO(payload)) as pdf:
        page = _read_page(pdf.pages[0], warnings.append)
    return page, warnings


def leaf_text(block):
    if block.children:
        return " ".join(leaf_text(child) for child in block.children)
    if block.kind == "table":
        return " ".join(cell for row in block.rows for cell in row)
    return block.text


def walk_blocks(blocks):
    """Containers may own card containers as well as ordinary text leaves."""
    for block in blocks:
        yield block
        yield from walk_blocks(block.children)


def same_box(a, b):
    return all(abs(x - y) < 0.1 for x, y in zip(a, b))


class CardPanelDetectorTests(unittest.TestCase):
    def test_small_four_card_grid_with_positive_gutters_is_supported(self):
        page,shapes=detector_input(cards()[:4])
        self.assertCountEqual(card_panels(page,shapes),[c.bbox for c in cards()[:4]])

    def test_separated_titled_white_panels_are_detected_once(self):
        specs = cards()
        page, shapes = detector_input(specs)
        self.assertCountEqual(card_panels(page, shapes), [card.bbox for card in specs])

    def test_input_order_and_duplicate_paint_operations_do_not_duplicate_cards(self):
        specs = cards()
        page, shapes = detector_input(specs)
        self.assertCountEqual(card_panels(page, list(reversed(shapes)) + shapes),
                              [card.bbox for card in specs])

    def test_touching_filled_table_cells_with_bold_labels_are_not_cards(self):
        page, shapes = detector_input(cards(dx=114, dy=100))
        self.assertEqual(card_panels(page, shapes), [],
                         "Shared table borders are not positive-width card gutters")

    def test_ordinary_unstyled_cells_are_not_cards(self):
        page, shapes = detector_input(cards())
        for word in page.words:
            word.font = "Helvetica"
        self.assertEqual(card_panels(page, shapes), [])

    def test_lone_titled_sidebar_is_not_a_diagram(self):
        page, shapes = detector_input(cards()[:1])
        self.assertEqual(card_panels(page, shapes), [])

    def test_overlapping_panels_are_not_a_separated_grid(self):
        for dx, dy in ((108, 126), (124, 94)):
            with self.subTest(dx=dx, dy=dy):
                page, shapes = detector_input(cards(dx=dx, dy=dy))
                self.assertEqual(card_panels(page, shapes), [])

    def test_outline_only_or_colored_shapes_are_not_white_card_panels(self):
        page, _ = detector_input(cards())
        for kwargs in ({"fill": False}, {"color": (0.8, 0.8, 0.8)}):
            with self.subTest(**kwargs):
                self.assertEqual(card_panels(page, [shape(c.bbox, **kwargs) for c in cards()]), [])


class CardPanelIntegrationTests(unittest.TestCase):
    def convert(self, payload):
        with tempfile.TemporaryDirectory(prefix="diagram-tests-") as directory:
            path = Path(directory) / "cards.pdf"
            path.write_bytes(payload)
            return convert_pdf(path, hyphen_resolver=lambda _: False)

    def assert_cards(self, blocks, specs, markdown, text, *, outside=()):
        """Check ownership, exact text, semantic children and export multiplicity."""
        asides = [block for block in walk_blocks(blocks)
                  if block.kind == "aside" and block.role == "diagram_node"]
        self.assertEqual(len(asides), len(specs),
                         [(b.kind, b.role, b.text[:100]) for b in blocks])
        all_leaves = canonical(" ".join(leaf_text(block) for block in blocks))
        expected_tokens = Counter(canonical(" ".join([c.text for c in specs] + list(outside))).split())
        self.assertEqual(Counter(all_leaves.split()), expected_tokens,
                         "Leaf blocks must not duplicate even fragments of card text")
        self.assertEqual(Counter(canonical(text).split()), expected_tokens,
                         "TXT must contain every ordinary word and number exactly as often as the source")
        for card in specs:
            with self.subTest(title=card.title):
                owners = [b for b in asides if any(same_box(s.bbox, card.bbox) for s in b.sources)]
                self.assertEqual(len(owners), 1, f"No unique card container for {card.title}")
                owner = owners[0]
                self.assertEqual(owner.role, "diagram_node")
                self.assertEqual(canonical(leaf_text(owner)), canonical(card.text))
                self.assertEqual(canonical(owner.text), canonical(card.text))
                self.assertTrue(owner.children, "A card must own its title and ordinary body blocks")
                self.assertEqual(owner.children[0].kind, "heading")
                self.assertEqual(owner.children[0].role, "aside_title")
                self.assertEqual(owner.children[0].text, card.title)
                for child in owner.children:
                    self.assertFalse(child.kind == "table" or child.role == "complex_table")
                    for source in child.sources:
                        self.assertEqual(source.page, 1)
                        a, b = card.bbox, source.bbox
                        self.assertTrue(a[0] - 1 <= b[0] <= b[2] <= a[2] + 1
                                        and a[1] - 1 <= b[1] <= b[3] <= a[3] + 1)
                for part in (card.title, *card.lines):
                    for output in (all_leaves, canonical(text), canonical(markdown)):
                        self.assertEqual(output.count(part), 1, (card.title, part, output))
                for other in specs:
                    if other != card:
                        self.assertNotIn(other.title, owner.text)
                # Require one continuous card quote at its actual nesting
                # depth. A diagram wrapper may add further quote prefixes.
                node_markdown, _ = render([owner])
                node_lines = node_markdown.rstrip("\n").splitlines()
                self.assertTrue(all(line.startswith(">") for line in node_lines))
                title_lines = [line for line in markdown.splitlines()
                               if f"**{card.title}**" in line]
                self.assertEqual(len(title_lines), 1)
                prefix = title_lines[0].split("**", 1)[0]
                self.assertRegex(prefix, r"^(?:> )+$")
                ancestors = prefix[:-2]
                self.assertIn("\n".join(ancestors + line for line in node_lines), markdown)
                for part in card.lines:
                    self.assertIn(part, node_markdown)
        self.assertNotIn("| ---", markdown)

    def test_reader_filters_real_spurious_ruling_tables_before_layout(self):
        specs = cards()
        payload = fixture_pdf(lambda c: draw_cards(c, specs, connectors=True))
        with pdfplumber.open(BytesIO(payload)) as pdf:
            table_boxes = [table.bbox for table in pdf.pages[0].find_tables()]
        self.assertTrue(any(sum(box[0] <= (c.bbox[0] + c.bbox[2]) / 2 <= box[2]
                                and box[1] <= (c.bbox[1] + c.bbox[3]) / 2 <= box[3]
                                for c in specs) >= 2 for box in table_boxes),
                        f"Fixture must reproduce a false multi-card ruling table: {table_boxes}")
        page, warnings = read_fixture(payload)
        self.assertCountEqual(page.diagram_boxes, [c.bbox for c in specs])
        self.assertEqual(page.tables, [], "Card text must not enter spurious table cells")
        self.assertEqual([(w.code, w.pages) for w in warnings], [("diagram_layout", (1,))])
        blocks = layout_page(page, set(), warnings.append, lambda _: False)
        self.assert_cards(blocks, specs, *render(blocks))

    def test_full_conversion_preserves_every_title_prerequisite_body_and_number_once(self):
        specs = cards()

        def draw(canvas):
            draw_line(canvas, OUTSIDE, 45, 120)
            draw_cards(canvas, specs, connectors=True)

        result = self.convert(fixture_pdf(draw))
        self.assert_cards(result.blocks, specs, result.markdown, result.text, outside=(OUTSIDE,))
        self.assertEqual(result.text.count(OUTSIDE), 1)
        self.assertTrue(all(OUTSIDE not in b.text for b in result.blocks if b.kind == "aside"))
        self.assertEqual(sum(w.code == "diagram_layout" for w in result.warnings), 1)

    def test_spurious_table_inside_one_panel_cannot_clip_its_ordinary_text(self):
        specs = cards()

        def draw(canvas):
            # Occluded ruling can form a smaller table within ONE node. A
            # filter that only removes tables covering several node centres
            # leaves these cells behind and consumes pieces of ordinary prose.
            canvas.rect(318, PAGE[1] - 260, 67, 100, stroke=1, fill=0)
            canvas.line(350, PAGE[1] - 160, 350, PAGE[1] - 260)
            for y in (185, 205, 225):
                canvas.line(318, PAGE[1] - y, 385, PAGE[1] - y)
            draw_cards(canvas, specs)

        payload = fixture_pdf(draw)
        with pdfplumber.open(BytesIO(payload)) as pdf:
            self.assertTrue(pdf.pages[0].find_tables(),
                            "Fixture must exercise actual internal false table cells")
        result = self.convert(payload)
        self.assert_cards(result.blocks, specs, result.markdown, result.text)

    def test_enclosing_decorative_fill_cannot_swallow_or_mix_card_containers(self):
        specs = cards()
        result = self.convert(fixture_pdf(lambda c: draw_cards(c, specs, backplate=True)))
        self.assert_cards(result.blocks, specs, result.markdown, result.text)

    def test_narrow_detected_cards_still_reach_aside_layout(self):
        # These fit the detector's accepted width range but must also survive
        # the reader's separate generic-sidebar size/content thresholds.
        specs = [Card(c.bbox, c.title, (f"Rank {index + 1}+ required.",
                                       f"Gain {index + 17} focus."))
                 for index, c in enumerate(cards(width=78))]

        def draw(canvas):
            for card in specs:
                x, top, right, bottom = card.bbox
                canvas.setFillColor(colors.white)
                canvas.rect(x, PAGE[1] - bottom, right - x, bottom - top, fill=1)
                draw_line(canvas, card.title, x + 4, top + 16, bold=True, size=8)
                for offset, text in zip((34, 54), card.lines):
                    draw_line(canvas, text, x + 4, top + offset, size=8)

        payload = fixture_pdf(draw)
        page, _ = read_fixture(payload)
        self.assertCountEqual(page.diagram_boxes, [c.bbox for c in specs])
        result = self.convert(payload)
        self.assert_cards(result.blocks, specs, result.markdown, result.text)

    def test_real_table_beside_cards_remains_a_table_with_all_values(self):
        specs = cards()
        expected_cells = []

        def draw(canvas):
            draw_cards(canvas, specs, connectors=True)
            expected_cells.extend(draw_table(canvas))

        result = self.convert(fixture_pdf(draw))
        tables = [b for b in walk_blocks(result.blocks) if b.kind == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(len([b for b in walk_blocks(result.blocks) if b.role == "diagram_node"]), 6)
        for title, value in expected_cells:
            for text in (" ".join(leaf_text(t) for t in tables), result.text):
                self.assertEqual(text.count(title), 1)
                self.assertEqual(text.count(value), 1)

    def test_ordinary_filled_table_cells_are_not_reclassified_as_diagram_cards(self):
        payload = fixture_pdf(lambda c: draw_table(c, top=160, x=60, width=240, row_height=70))
        page, warnings = read_fixture(payload)
        self.assertEqual(page.diagram_boxes, [])
        self.assertTrue(page.tables)
        self.assertFalse(any(w.code == "diagram_layout" for w in warnings))
        result = self.convert(payload)
        self.assertEqual(len([b for b in result.blocks if b.kind == "table"]), 1)
        self.assertFalse(any(b.role == "diagram_node" for b in walk_blocks(result.blocks)))
        for index in range(6):
            self.assertEqual(result.text.count(f"Price Band {index + 1}"), 1)
            self.assertEqual(result.text.count(f"Value {index + 41} silver pieces."), 1)

    def test_lone_sidebar_keeps_ordinary_aside_behavior(self):
        card = cards()[0]
        result = self.convert(fixture_pdf(lambda c: draw_cards(c, [card])))
        asides = [b for b in result.blocks if b.kind == "aside"]
        self.assertEqual(len(asides), 1)
        self.assertNotEqual(asides[0].role, "diagram_node")
        self.assertEqual(canonical(leaf_text(asides[0])), card.text)
        self.assertFalse(any(w.code == "diagram_layout" for w in result.warnings))


def independent_local_nodes(page, *, width_range=(110, 116), left_range=(315, 445),
                            height_range=(35, 120)):
    """Select known white panels by fixture geometry, without card_panels.

    This geometric oracle is restricted to the supplied physical pages.
    It uses neither production words, frames, tables nor detected diagram boxes.
    """
    boxes = set()
    for item in (*page.rects, *page.curves):
        box = tuple(item[key] for key in ("x0", "top", "x1", "bottom"))
        if (item.get("fill") and item.get("non_stroking_color") == (1, 1, 1)
                and left_range[0] <= box[0] <= left_range[1]
                and width_range[0] <= box[2] - box[0] <= width_range[1]
                and height_range[0] <= box[3] - box[1] <= height_range[1]):
            boxes.add(box)
    nodes = []
    clean = page.dedupe_chars(tolerance=0.5, extra_attrs=("fontname", "size"))
    # Explicitly exclude only graphical-font glyphs, never ordinary numbers
    # or punctuation that happen to resemble action icons.
    clean = clean.filter(lambda obj: obj.get("object_type") != "char"
                         or not any(tag in obj.get("fontname", "").lower()
                                    for tag in ("dingbat", "symbol", "icon")))
    for box in sorted(boxes, key=lambda b: (b[1], b[0])):
        text = clean.within_bbox(box).extract_text(x_tolerance=2, x_tolerance_ratio=0.18,
                                                  y_tolerance=3, expand_ligatures=True)
        nodes.append((box, canonical(text or "")))
    return nodes


class OptionalLocalCardRegressionTests(unittest.TestCase):
    def check_page(self, filename, physical_page, *, count=8, **geometry):
        path = ROOT / "example" / filename
        if not path.is_file():
            self.skipTest(f"Optional local PDF not supplied: {filename}")
        with pdfplumber.open(path, unicode_norm="NFC") as pdf:
            self.assertGreaterEqual(len(pdf.pages), physical_page)
            page = pdf.pages[physical_page - 1]
            expected = independent_local_nodes(page, **geometry)
            self.assertEqual(len(expected), count,
                             "Independent geometric oracle must cover every selected node")
            warnings = []
            data = _read_page(page, warnings.append)
        blocks = layout_page(data, set(), warnings.append, lambda _: False)
        for box, source_text in expected:
            with self.subTest(page=physical_page, box=tuple(round(x, 1) for x in box)):
                owners = [b for b in walk_blocks(blocks) if b.kind == "aside"
                          and any(same_box(source.bbox, box) for source in b.sources)]
                self.assertEqual(len(owners), 1,
                                 f"Missing independent card container: {source_text}")
                owner = owners[0]
                self.assertEqual(owner.role, "diagram_node")
                owner_leaves = [b for b in walk_blocks([owner]) if not b.children]
                for child in owner_leaves:
                    self.assertEqual(canonical(child.text),
                                     canonical("\n".join(line.text for line in child.lines)),
                                     "Converted child text must agree with its retained source lines")
                # Filter graphical-font words from output independently too.
                ordinary = [w for child in owner_leaves for line in child.lines for w in line.words
                            if not any(tag in w.font.lower() for tag in ("dingbat", "symbol", "icon"))]
                self.assertTrue(ordinary, "A diagram card must retain ordinary text children")
                ordinary_ids = {id(w) for w in ordinary}
                actual = canonical("\n".join(
                    " ".join(w.text for w in line.words if id(w) in ordinary_ids)
                    for child in owner_leaves for line in child.lines))
                self.assertEqual(actual, source_text)
                self.assertEqual(Counter(re.findall(r"\d+", actual)),
                                 Counter(re.findall(r"\d+", source_text)))
                owned_ids = {id(b) for b in walk_blocks([owner])}
                for block in walk_blocks(blocks):
                    if id(block) not in owned_ids and not block.children:
                        self.assertFalse(any(box[0] < (w.bbox[0] + w.bbox[2]) / 2 < box[2]
                                             and box[1] < (w.bbox[1] + w.bbox[3]) / 2 < box[3]
                                             for line in block.lines for w in line.words),
                                         "Card text leaked into another top-level block")

    def test_heroic_paths_physical_page_8(self):
        self.check_page("6. Chapter 4 Heroic Paths.pdf", 8)

    def test_radiant_paths_physical_page_14(self):
        self.check_page("7. Chapter 5 Radiant Paths.pdf", 14)

    def test_surges_physical_page_5(self):
        self.check_page("8. Chapter 6 Surges.pdf", 5)

    def test_four_card_enlightened_tree_physical_page_67(self):
        self.check_page("7. Chapter 5 Radiant Paths.pdf",67,count=4)

    def test_investigative_web_endeavors_physical_page_6(self):
        # The three starter cards are wider than the ten subsequent cards.
        # All thirteen still belong in individual diagram-node containers.
        self.check_page("14. Chapter 12 Endeavors.pdf", 6, count=13,
                        width_range=(138, 160), left_range=(50, 406),
                        height_range=(35, 140))

    def test_mass_combat_gamemastering_physical_page_17(self):
        self.check_page("15. Chapter 13 Gamemastering.pdf", 17, count=7,
                        width_range=(191, 193), left_range=(110, 328),
                        height_range=(70, 110))


if __name__ == "__main__":
    unittest.main()
