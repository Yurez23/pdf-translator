"""Section-scoped aside order without changing assembled content or sources."""

from copy import deepcopy
from pathlib import Path
import unittest

from pdf_translator import convert_pdf
from pdf_translator.aside_order import order_asides
from pdf_translator.models import Block, Line, SourceRegion, Word
from pdf_translator.page_data import HeadingHint, PageData


ROOT = Path(__file__).resolve().parents[1]


def block(kind, text, page, box, *, role="", level=0, sources=None, font="Body", children=None):
    word = Word(text, box, 10, font, color=(0.0,))
    return Block(kind, text, tuple(sources or [SourceRegion(page, box)]),
                 role=role, outline_level=level, level=level,
                 lines=[Line([word], box, 10)], body_size=10, children=children or [])


def frame(page, box):
    return block("aside", "inset", page, box, children=[block("paragraph", "content", page, box)])


def pages(*numbers):
    return [PageData(number, 612, 792, [], 10) for number in numbers]


def cross_page():
    return [block("heading", "section", 1, (57, 594, 160, 607), level=4),
            block("paragraph", "first", 1, (57, 610, 283, 698)),
            frame(1, (318, 396, 554, 722)),
            block("paragraph", "second", 2, (57, 53, 285, 102)),
            block("paragraph", "third", 2, (57, 106, 291, 206)),
            block("heading", "next", 2, (57, 332, 209, 349), level=3)]


def cross_column(*, quotation=False):
    start = block("heading", "section", 1, (57, 54 if not quotation else 228, 190, 67 if not quotation else 242), level=4)
    left = (57, 69, 291, 170) if not quotation else (57, 378, 291, 413)
    right = (318, 53, 549, 167) if not quotation else (318, 79, 541, 141)
    sources = [SourceRegion(1, left)]
    if quotation:
        sources.append(SourceRegion(1, (318, 53, 524, 76)))
    previous = block("paragraph", "first", 1, left, sources=sources)
    inset = (block("aside", "quote", 1, (93, 454, 269, 546), role="quotation")
             if quotation else frame(1, (57.5, 182, 293.5, 691)))
    return [start, previous, inset, block("paragraph", "second", 1, right),
            block("heading", "next", 1, (318, 174 if not quotation else 148, 435, 188 if not quotation else 162), level=4)]


def opener():
    title = (79, 186, 272, 207)
    return [frame(1, (318, 54, 554, 188)),
            block("heading", "title", 1, title, level=2),
            block("heading", "label", 1, (132, 207, 219, 221), role="title_label"),
            block("paragraph", "opening", 1, (56, 278, 288, 340)),
            block("paragraph", "more", 1, (56, 343, 286, 444)),
            block("heading", "following", 2, (57, 54, 210, 70), level=3)]


class AsideOrderTests(unittest.TestCase):
    def assert_move(self, original, expected, source_pages):
        saved = deepcopy(original)
        result = order_asides(original, source_pages)
        self.assertEqual([b.text for b in result], expected)
        self.assertEqual(original, saved)
        self.assertEqual({id(b) for b in original}, {id(b) for b in result})
        self.assertTrue(all(next(b for b in result if b is original[i]) == saved[i]
                            for i in range(len(original))))

    def test_frame_crosses_page_only_to_established_section_boundary(self):
        self.assert_move(cross_page(), ["section", "first", "second", "third", "inset", "next"], pages(1, 2))

    def test_frame_crosses_column_at_page_opening(self):
        self.assert_move(cross_column(), ["section", "first", "second", "inset", "next"], pages(1))

    def test_quote_moves_after_joined_paragraph_and_separate_final_paragraph(self):
        original = cross_column(quotation=True)
        joined = original[1]
        self.assert_move(original, ["section", "first", "second", "quote", "next"], pages(1))
        self.assertEqual(len(joined.sources), 2)

    def test_confirmed_page_banner_precedes_right_hand_frame(self):
        source_pages = pages(1, 2)
        source_pages[0].heading_hints.append(HeadingHint((79, 186, 272, 207), scope="section", protect=True))
        self.assert_move(opener(), ["title", "label", "opening", "more", "inset", "following"], source_pages)

    def test_cross_page_requires_selected_next_page_and_explicit_scope_end(self):
        original = cross_page()
        self.assertEqual(order_asides(original[:3], pages(1, 2)), original[:3])
        original[-1].outline_level = 5
        self.assertEqual(order_asides(original, pages(1, 2)), original)
        self.assertEqual(order_asides(original[:-1], pages(1, 2)), original[:-1])

    def test_style_and_intervening_structure_are_barriers(self):
        for kind in ("list_item", "table", "footnote"):
            original = cross_column()
            original[3].kind = kind
            self.assertEqual(order_asides(original, pages(1)), original)
        original = cross_column()
        original[3].lines[0].words[0].font = "Other"
        self.assertEqual(order_asides(original, pages(1)), original)
        original = cross_page()
        original[3].lines[0].words[0].color = (1.0,)
        self.assertEqual(order_asides(original, pages(1, 2)), original)
        original = cross_page()
        for paragraph in (original[1], original[3]):
            paragraph.lines[0].words[0].color = None
        self.assertEqual(order_asides(original, pages(1, 2)), original)

    def test_quote_without_existing_column_join_and_diagram_are_untouched(self):
        original = cross_column(quotation=True)
        original[1].sources = original[1].sources[:1]
        self.assertEqual(order_asides(original, pages(1)), original)
        original = cross_column()
        original[2].role = "diagram"
        self.assertEqual(order_asides(original, pages(1)), original)

    def test_ambiguous_opener_and_already_ordered_control_stay_unchanged(self):
        original = opener()
        self.assertEqual(order_asides(original, pages(1, 2)), original)
        source_pages = pages(1, 2)
        source_pages[0].heading_hints.append(HeadingHint((79, 186, 272, 207), scope="section"))
        original[2].role = "aside_title"
        self.assertEqual(order_asides(original, source_pages), original)
        ordered = opener()[1:5] + opener()[:1] + opener()[5:]
        self.assertEqual(order_asides(ordered, source_pages), ordered)

    def test_banner_does_not_split_opening_from_list_table_or_page_continuation(self):
        source_pages = pages(1, 2)
        source_pages[0].heading_hints.append(HeadingHint((79, 186, 272, 207), scope="section"))
        for kind in ("list_item", "table"):
            original = opener()
            original.insert(4, block(kind, "independent structure", 1, (57, 345, 285, 400)))
            self.assertEqual(order_asides(original, source_pages), original)
        original = opener()
        original[-1] = block("paragraph", "following page text", 2, (57, 54, 285, 95))
        self.assertEqual(order_asides(original, source_pages), original)


@unittest.skipUnless((ROOT / "example/6. Chapter 4 Heroic Paths.pdf").exists()
                     and (ROOT / "example/7. Chapter 5 Radiant Paths.pdf").exists(),
                     "Local chapter PDFs not supplied")
class LocalAsideOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hero = convert_pdf(ROOT / "example/6. Chapter 4 Heroic Paths.pdf", pages="39-40",
                               hyphen_resolver=lambda _: False)
        cls.radiant = convert_pdf(ROOT / "example/7. Chapter 5 Radiant Paths.pdf", pages="32,58,68,71",
                                  hyphen_resolver=lambda _: False)

    def assert_order(self, result, *prefixes):
        for output in (result.markdown, result.text):
            cursor = 0
            for prefix in prefixes:
                cursor = output.index(prefix, cursor) + len(prefix)

    def test_cross_page_section_rejoins_prose_and_keeps_following_paragraph(self):
        self.assert_order(self.hero, "Vriztl Engineer", "The engineers of Thaylenah",
                          "After many generations of this diligent recordkeeping",
                          "Building a Vriztl Engineer", "Artifabrians", "Scholar Key Talent")
        first = next(b for b in self.hero.blocks if b.text.startswith("The engineers of Thaylenah"))
        self.assertEqual([s.page for s in first.sources], [39, 40])
        self.assertIn("future successes. After many generations", first.text)
        following = self.hero.blocks[self.hero.blocks.index(first) + 1]
        self.assertTrue(following.text.startswith("Building a Vriztl Engineer."))
        aside = next(b for b in self.hero.blocks if b.kind == "aside" and b.text.startswith("Artifabrians"))
        self.assertEqual([s.page for s in aside.sources], [39])
        for output in (self.hero.markdown, self.hero.text):
            self.assertIn("future successes. After many generations", output)

    def test_page_selection_excludes_the_intervening_page(self):
        result = convert_pdf(ROOT / "example/6. Chapter 4 Heroic Paths.pdf", pages="39,41",
                             hyphen_resolver=lambda _: False)
        first = next(i for i, b in enumerate(result.blocks) if b.text.startswith("The engineers of Thaylenah"))
        self.assertEqual([s.page for s in result.blocks[first].sources], [39])
        self.assertNotIn("After many generations of this diligent recordkeeping", result.text)
        aside = result.blocks[first + 1]
        self.assertEqual((aside.kind, aside.sources[0].page), ("aside", 39))
        self.assertTrue(aside.text.startswith("Artifabrians"))

    def test_quote_and_culture_frames_follow_their_sections(self):
        self.assert_order(self.radiant, "When you choose a squire", "You can have a maximum number of squires",
                          "They’re smart, strategic, and capable.", "Third Ideal (Elsecaller)")
        joined = next(b for b in self.radiant.blocks if b.text.startswith("When you choose a squire"))
        self.assertEqual([s.page for s in joined.sources], [32, 32])
        self.assert_order(self.radiant, "Reachers are characterized", "Reachers are ubiquitous",
                          "Roleplaying a Lightspren", "Lightspren Names")

    def test_banner_and_existing_opener_control(self):
        self.assert_order(self.radiant, "Truthwatcher\n", "Radiant Path", "A stormform creeps",
                          "Recommended Heroic Paths for Truthwatchers")
        self.assert_order(self.radiant, "Willshaper\n", "In a flash of light, a Willshaper",
                          "Recommended Heroic Paths for Willshapers", "Willshaper Philosophy")


if __name__ == "__main__":
    unittest.main()
