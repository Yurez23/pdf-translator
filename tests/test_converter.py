"""Small generated PDFs exercise real extraction, not just implementation details."""

import contextlib
import collections
import io
import subprocess
import sys
import tempfile
import re
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

import pdfplumber
from reportlab.lib import colors
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Table, TableStyle

from pdf_translator import Block, ConversionError, ConversionResult, SourceRegion, convert_pdf
from pdf_translator.cli import _read_choice, main
from pdf_translator.converter import parse_pages
from pdf_translator.export import render
from pdf_translator.text import join_lines, normalize

ROOT = Path(__file__).resolve().parents[1]
PAGE = (612, 792)


def lines(canvas, text, *, x=50, top=110, size=10, leading=14, font="Helvetica"):
    canvas.setFont(font, size)
    for i, line in enumerate(text):
        canvas.drawString(x, PAGE[1] - top - i * leading, line)


class TextTests(unittest.TestCase):
    def join(self, fragments, lexicon=()):
        warnings = []
        text = join_lines(fragments, set(lexicon), 3, warnings.append)
        return text, warnings

    def test_normalization_preserves_typography(self):
        self.assertEqual(normalize("  ﬁne\u00a0text\t— café  "), "fine text — café")

    def test_sentences_on_adjacent_lines_join(self):
        self.assertEqual(self.join(["A sentence.", "Another sentence."])[0], "A sentence. Another sentence.")

    def test_soft_hyphen_joins_without_space(self):
        self.assertEqual(self.join(["A discre\u00ad", "tionary hyphen."])[0], "A discretionary hyphen.")

    def test_attested_word_rejoins(self):
        text, warnings = self.join(["A char-", "acter appears."], ["character"])
        self.assertEqual(text, "A character appears.")
        self.assertFalse(warnings)

    def test_real_compound_keeps_hyphen(self):
        text, warnings = self.join(["Use a well-", "known rule."], ["well-known"])
        self.assertEqual(text, "Use a well-known rule.")
        self.assertFalse(warnings)

    def test_ambiguous_hyphen_reports_page(self):
        text, warnings = self.join(["a fort-", "ress"], [])
        self.assertEqual(text, "a fort-ress")
        self.assertEqual(warnings[0].pages, (3,))
        self.assertEqual(warnings[0].code, "ambiguous_hyphen")

    def test_resolver_can_join_or_keep_with_context(self):
        for choice, expected in [(True, "A fortress stands."), (False, "A fort-ress stands.")]:
            with self.subTest(choice=choice):
                issues, warnings = [], []
                def resolve(issue):
                    issues.append(issue)
                    return choice
                text = join_lines(["A fort-", "ress stands."], set(), 11, warnings.append, resolve)
                self.assertEqual(text, expected)
                self.assertEqual(len(issues), 1)
                self.assertEqual((issues[0].page, issues[0].hyphenated, issues[0].joined), (11, "fort-ress", "fortress"))
                self.assertIn("A fort-\nress stands.", issues[0].context)
                self.assertFalse(warnings)

    def test_known_hyphens_do_not_ask(self):
        def unexpected(issue):
            self.fail("A known word or compound should not prompt")
        self.assertEqual(join_lines(["char-", "acter"], {"character"}, 1, self.fail, unexpected), "character")
        self.assertEqual(join_lines(["well-", "known"], {"well-known"}, 1, self.fail, unexpected), "well-known")

    def test_numeric_dash_is_not_dehyphenated(self):
        self.assertEqual(self.join(["10-", "20 units"])[0], "10-20 units")

    def test_page_selection(self):
        self.assertEqual(parse_pages(None, 5), [1, 2, 3, 4, 5])
        self.assertEqual(parse_pages("5, 1-3,2", 5), [1, 2, 3, 5])
        self.assertEqual(parse_pages([3, 1], 3), [1, 3])

    def test_invalid_page_selection(self):
        for pages in ("", "0", "3-1", "1-20", "-1", "1,,2", "a", [], [True]):
            with self.subTest(pages=pages), self.assertRaises(ConversionError):
                parse_pages(pages, 5)

    def test_markdown_escape_and_table(self):
        source = (SourceRegion(1, (0, 0, 1, 1)),)
        md, txt = render([Block("paragraph", "# literal *text*", source), Block("table", "", source, rows=[["A|B", "C"], ["x", "y"]])])
        self.assertIn(r"\# literal \*text\*", md)
        self.assertIn(r"A\|B", md)
        self.assertIn("A|B\tC\nx\ty", txt)


class PdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "book.pdf"

    def pdf(self, draw, **kwargs):
        canvas = Canvas(str(self.path), pagesize=PAGE, **kwargs)
        draw(canvas)
        canvas.save()
        return convert_pdf(self.path)

    def test_paragraphs_headings_and_indentation(self):
        def draw(c):
            lines(c, ["Chapter title"], size=18, top=70)
            lines(c, ["One sentence ends here.", "Another continues in the same paragraph."], top=110)
            lines(c, ["A separate indented paragraph", "continues without a hard line break."], x=62, top=150)
        result = self.pdf(draw)
        self.assertIn("# Chapter title", result.markdown)
        self.assertIn("One sentence ends here. Another continues in the same paragraph.", result.text)
        self.assertIn("\n\nA separate indented paragraph continues without a hard line break.", result.text)
        self.assertTrue(all(b.sources for b in result.blocks))

    def test_hanging_list_does_not_eat_next_paragraph(self):
        def draw(c):
            lines(c, ["- First item starts here and"], top=100)
            lines(c, ["continues on the next line."], x=62, top=114)
            lines(c, ["- Second item."], top=140)
            lines(c, ["A new paragraph follows the list."], x=62, top=168)
            lines(c, ["It has its own continuation."], top=182)
        result = self.pdf(draw)
        self.assertIn("- First item starts here and continues on the next line.", result.markdown)
        paragraph = next(b for b in result.blocks if b.text.startswith("A new paragraph"))
        self.assertEqual(paragraph.kind, "paragraph")
        self.assertIn("It has its own continuation.", paragraph.text)

    def test_two_columns_and_spanning_title(self):
        left = [f"Left {i} has words that belong to this column" for i in range(6)]
        right = [f"Right {i} has words that belong to this column" for i in range(6)]
        def draw(c):
            c.setFont("Helvetica-Bold", 18)
            c.drawCentredString(306, 730, "A title spanning both columns")
            lines(c, left, x=50)
            lines(c, right, x=325)
        result = self.pdf(draw)
        self.assertLess(result.text.index("Left 5"), result.text.index("Right 0"))
        self.assertLess(result.text.index("A title"), result.text.index("Left 0"))

    def test_distant_caption_is_not_part_of_heading(self):
        def draw(c):
            lines(c, ["Section title"], size=18, top=80)
            lines(c, ["Portrait caption"], x=425, size=9, top=80)
            lines(c, ["The main text continues in the left column.", "It remains independent from the portrait caption."], top=110)
        result = self.pdf(draw)
        title = next(b for b in result.blocks if b.kind == "heading")
        self.assertEqual(title.text, "Section title")
        self.assertIn("Portrait caption", result.text)

    def test_running_headers_and_footers(self):
        def draw(c):
            for number in range(1, 4):
                lines(c, [f"Running chapter {number}"], top=30)
                lines(c, [f"Body on physical page {number}."])
                lines(c, [str(100 + number)], top=765)
                c.showPage()
        result = self.pdf(draw)
        self.assertNotIn("Running chapter", result.text)
        self.assertNotIn("101", result.text)
        self.assertIn("Body on physical page 3.", result.text)

    def test_page_continuation_and_selected_page_gaps(self):
        def draw(c):
            lines(c, ["The paragraph continues onto the next page with"], top=700)
            c.showPage()
            lines(c, ["more text and a completed sentence."])
            c.showPage()
            lines(c, ["another selected page begins here."])
        self.pdf(draw)
        result = convert_pdf(self.path, pages="1-2")
        self.assertIn("with more text", result.text)
        self.assertEqual(len(result.blocks[0].sources), 2)
        separated = convert_pdf(self.path, pages="1,3")
        self.assertIn("with\n\nanother", separated.text)
        self.assertNotIn("more text", separated.text)

    def test_empty_page_warning(self):
        def draw(c):
            lines(c, ["Readable page."])
            c.showPage()
            c.showPage()
        result = self.pdf(draw)
        self.assertIn(("empty_page", (2,)), [(w.code, w.pages) for w in result.warnings])

    def test_no_text_and_invalid_input(self):
        with self.assertRaisesRegex(ConversionError, "OCR"):
            self.pdf(lambda c: c.showPage())
        self.path.write_bytes(b"not a PDF")
        with self.assertRaisesRegex(ConversionError, "повреждён"):
            convert_pdf(self.path)
        with self.assertRaisesRegex(ConversionError, "не найден"):
            convert_pdf(self.directory / "missing.pdf")

    def test_encrypted_pdf(self):
        with self.assertRaisesRegex(ConversionError, "защищён"):
            self.pdf(lambda c: lines(c, ["Private text."]), encrypt=StandardEncryption("password", ownerPassword="owner"))

    def test_table_and_merged_cells(self):
        def draw(c, merged=False):
            table = Table([["Tier", "Name"], ["1", "First"], ["2", "Second"]], colWidths=[100, 180], rowHeights=24)
            commands = [("GRID", (0, 0), (-1, -1), 0.5, colors.black)]
            if merged:
                commands += [("SPAN", (0, 0), (1, 0))]
            table.setStyle(TableStyle(commands))
            table.wrapOn(c, 500, 600)
            table.drawOn(c, 50, 600)
        result = self.pdf(draw)
        self.assertIn("| Tier | Name |", result.markdown)
        self.assertIn("1\tFirst", result.text)
        self.assertEqual(sum(b.kind == "table" for b in result.blocks), 1)
        result = self.pdf(lambda c: draw(c, True))
        self.assertTrue(any(w.code == "complex_table" for w in result.warnings))
        self.assertIn("Second", result.text)

    def test_resolver_changes_table_cell_in_both_formats(self):
        def draw(c):
            table = Table([["Place", "Notes"], ["Castle", "A fort-\nress stands."]], colWidths=[100, 180], rowHeights=[24, 40])
            table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
            table.wrapOn(c, 500, 600)
            table.drawOn(c, 50, 600)
        self.pdf(draw)
        issues = []
        def resolve(issue):
            issues.append(issue)
            return True
        result = convert_pdf(self.path, hyphen_resolver=resolve)
        self.assertEqual(len(issues), 1)
        self.assertIn("A fortress stands.", result.markdown)
        self.assertIn("A fortress stands.", result.text)
        self.assertEqual(result.blocks[0].rows[1][1], "A fortress stands.")
        self.assertFalse(any(w.code == "ambiguous_hyphen" for w in result.warnings))

    def test_resolver_handles_cross_page_hyphen_once(self):
        def draw(c):
            lines(c, ["A distant fort-"], top=700)
            c.showPage()
            lines(c, ["ress stands here."])
        self.pdf(draw)
        issues = []
        def resolve(issue):
            issues.append(issue)
            return True
        result = convert_pdf(self.path, hyphen_resolver=resolve)
        self.assertEqual(len(issues), 1)
        self.assertIn("A distant fortress stands here.", result.text)

    def test_sidebar_kept_separate_from_other_column(self):
        def draw(c):
            c.setFillColor(colors.lightgrey)
            c.rect(50, 420, 235, 120, fill=1, stroke=1)
            c.setFillColor(colors.black)
            lines(c, ["Sidebar text has several words to explain", "a rule in an entirely separate box."], top=280, x=60)
            lines(c, [f"Main flow {i} continues through the right column" for i in range(10)], x=325, top=270)
        result = self.pdf(draw)
        aside = next(b for b in result.blocks if b.kind == "aside")
        self.assertIn("a rule in an entirely separate box.", aside.text)
        self.assertNotIn("Main flow", aside.text)

    def test_footnotes_and_rotated_text_warn(self):
        def draw(c):
            lines(c, ["Normal body has enough words to be the dominant font size on this page."])
            c.line(50, 115, 170, 115)
            lines(c, ["1 A small footnote.", "Its continuation stays separate."], top=695, size=8, leading=11)
            c.saveState()
            c.translate(230, 200)
            c.rotate(90)
            c.setFont("Helvetica", 8)
            c.drawString(0, 0, "Artist credit")
            c.restoreState()
        result = self.pdf(draw)
        self.assertTrue(any(b.kind == "footnote" for b in result.blocks))
        self.assertTrue(any(w.code == "rotated_text" for w in result.warnings))

    def test_only_small_name_like_vertical_margin_credits_are_removed(self):
        def draw(c):
            lines(c, ["Body text establishes the normal font and its column boundaries."])
            for x, y, size, text in [(20, 150, 8, "DENMAN ROOKE"), (580, 150, 8, "ANOTHER ARTIST"),
                                     (230, 150, 8, "USEFUL DIAGRAM"), (20, 350, 10, "LARGE LABEL"),
                                     (580, 350, 8, "AXIS 123"), (20, 500, 8, "SINGLE")]:
                c.saveState()
                c.translate(x, y)
                c.rotate(90)
                c.setFont("Helvetica", size)
                c.drawString(0, 0, text)
                c.restoreState()
        result = self.pdf(draw)
        self.assertNotIn("NAMNED", result.text)
        self.assertNotIn("REHTONA", result.text)
        self.assertIn("MARGAID", result.text)
        self.assertIn("EGRAL", result.text)
        self.assertIn("321", result.text)  # Existing raw rotated-text order is retained.
        self.assertIn("ELGNIS", result.text)
        self.assertTrue(any(w.code == "rotated_text" for w in result.warnings))

    def test_api_has_no_output_side_effects(self):
        self.pdf(lambda c: lines(c, ["A document."]))
        before = set(self.directory.iterdir())
        convert_pdf(self.path)
        self.assertEqual(before, set(self.directory.iterdir()))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def hyphen_pdf(self, repeat=1):
        source = self.directory / "hyphens.pdf"
        c = Canvas(str(source), pagesize=PAGE)
        for i in range(repeat):
            lines(c, ["A remote fort-", "ress stands here."], top=100 + i * 60)
        c.save()
        return source

    def test_interactive_choices_apply_to_each_occurrence(self):
        source = self.hyphen_pdf(repeat=2)
        with patch("sys.stdin", io.StringIO("1\n2\n")):
            code, _, stderr = self.run_cli(str(source), "--output-dir", str(self.directory / "out"))
        self.assertEqual(code, 0)
        self.assertEqual(stderr.count("Неоднозначный перенос, страница PDF 1"), 2)
        for suffix in (".md", ".txt"):
            text = (self.directory / "out" / ("hyphens" + suffix)).read_text(encoding="utf-8")
            self.assertIn("A remote fortress stands here.", text)
            self.assertIn("A remote fort-ress stands here.", text)

    def test_invalid_answer_reprompts(self):
        source = self.hyphen_pdf()
        with patch("sys.stdin", io.StringIO("wrong\n1\n")):
            code, _, stderr = self.run_cli(str(source), "--output-dir", str(self.directory / "out"))
        self.assertEqual(code, 0)
        self.assertIn("Нажмите 1, 2 или Q", stderr)

    def test_cancel_or_eof_never_writes_partial_results(self):
        source = self.hyphen_pdf(repeat=2)
        for answers, expected in [("1\nq\n", 130), ("1\n", 1)]:
            out = self.directory / ("cancel" if expected == 130 else "eof")
            with self.subTest(answers=answers), patch("sys.stdin", io.StringIO(answers)):
                code, _, stderr = self.run_cli(str(source), "--output-dir", str(out))
                self.assertEqual(code, expected)
                self.assertFalse(out.exists())
                self.assertNotIn("Traceback", stderr)

    def test_explicit_noninteractive_modes(self):
        source = self.hyphen_pdf()
        for mode, word in [("join", "fortress"), ("keep", "fort-ress")]:
            out = self.directory / mode
            with self.subTest(mode=mode), patch("pdf_translator.cli._read_choice", side_effect=AssertionError("must not ask")):
                code, _, _ = self.run_cli(str(source), "--output-dir", str(out), "--hyphens", mode)
                self.assertEqual(code, 0)
                self.assertIn(word, (out / "hyphens.txt").read_text(encoding="utf-8"))

    @unittest.skipUnless(sys.platform == "win32", "Windows console key reader")
    def test_windows_terminal_reads_single_key_without_enter(self):
        with patch("sys.stdin.isatty", return_value=True), patch("msvcrt.getwch", return_value="1") as get_key:
            self.assertEqual(_read_choice(), "1")
            get_key.assert_called_once_with()

    def test_real_subprocess_accepts_piped_choice(self):
        source = self.hyphen_pdf()
        out = self.directory / "process"
        result = subprocess.run([sys.executable, "-m", "pdf_translator", str(source), "--output-dir", str(out)],
                                input="1\n", capture_output=True, encoding="utf-8", cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fortress", (out / "hyphens.txt").read_text(encoding="utf-8"))

    @patch("pdf_translator.cli.convert_pdf", return_value=ConversionResult("# café\n", "café\n", (), ()))
    def test_utf8_outputs_and_overwrite(self, convert):
        out = str(self.directory)
        code, stdout, _ = self.run_cli("book.pdf", "--output-dir", out)
        self.assertEqual(code, 0)
        self.assertEqual((self.directory / "book.txt").read_text(encoding="utf-8"), "café\n")
        before = (self.directory / "book.md").read_bytes()
        self.assertEqual(self.run_cli("book.pdf", "--output-dir", out)[0], 1)
        self.assertEqual((self.directory / "book.md").read_bytes(), before)
        self.assertEqual(convert.call_count, 1)
        self.assertEqual(self.run_cli("book.pdf", "--output-dir", out, "--overwrite")[0], 0)

    @patch("pdf_translator.cli.convert_pdf")
    def test_existing_txt_does_not_leave_new_md(self, convert):
        (self.directory / "book.txt").write_text("keep", encoding="utf-8")
        self.assertEqual(self.run_cli("book.pdf", "--output-dir", str(self.directory))[0], 1)
        self.assertFalse((self.directory / "book.md").exists())
        convert.assert_not_called()

    def test_source_cannot_be_overwritten_even_with_nonstandard_extension(self):
        source = self.directory / "book.txt"
        source.write_bytes(b"original input")
        code, _, stderr = self.run_cli(str(source), "--output-dir", str(self.directory), "--overwrite")
        self.assertEqual(code, 1)
        self.assertIn("совпадает", stderr)
        self.assertEqual(source.read_bytes(), b"original input")

    def test_missing_file_has_no_traceback(self):
        code, _, stderr = self.run_cli(str(self.directory / "missing.pdf"))
        self.assertEqual(code, 1)
        self.assertIn("не найден", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_module_entrypoint(self):
        result = subprocess.run([sys.executable, "-m", "pdf_translator", "--help"], cwd=ROOT, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--pages", result.stdout)


@unittest.skipUnless((ROOT / "example.pdf").exists(), "Local reference PDF is not distributed with the source")
class ReferencePdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = convert_pdf(ROOT / "example.pdf")

    def test_chapter_opening_and_reading_order(self):
        text = self.result.text
        self.assertIn("As your character adventures across Roshar, you grow not only by gaining levels", text)
        self.assertLess(text.index("Purpose and Obstacle"), text.index("Your purpose is your core need."))
        self.assertNotIn("AA\n", text)

    def test_hanging_list_and_following_paragraph(self):
        self.assertIn("Save Others. You have an unrelenting need to defend those who can’t protect themselves.", self.result.text)
        block = next(b for b in self.result.blocks if b.text.startswith("Advancing Your Goal."))
        self.assertNotIn("Before deciding on a goal", block.text)

    def test_tables_and_no_running_footers(self):
        tables = [b for b in self.result.blocks if b.kind == "table"]
        companion = next(b for b in tables if b.sources[0].page == 7)
        self.assertEqual(companion.rows[0], ["PC Tier", "Role and Number", "NPC Profile Examples", "Development Bonuses"])
        self.assertIn("Chasmfiend", " ".join(companion.rows[-1]))
        self.assertNotIn("Chapter 8: Goals and Rewards", self.result.text)

    def test_portrait_label_does_not_join_ideals_heading(self):
        ideals = next(b for b in self.result.blocks if b.kind == "heading" and b.text.startswith("Ideals"))
        self.assertEqual(ideals.text, "Ideals")

    def test_artist_names_and_figure_captions_are_filtered(self):
        self.assertFalse(any(b.role == "rotated" for b in self.result.blocks))
        self.assertFalse(any(w.code == "rotated_text" for w in self.result.warnings))
        for fragment in ("EKOOR", "NAMNED", "IRAASOKAH", "REGREBEES", "NENÖKTYR"):
            self.assertNotIn(fragment, self.result.text)
        for caption in ("navani", "kholin", "heraldry"):
            self.assertNotIn(caption, self.result.text.casefold())
            self.assertNotIn(caption, self.result.markdown.casefold())
        self.assertIn("earn their ongoing loyalty", self.result.text)

    def test_all_other_source_letters_and_digits_are_preserved(self):
        def characters(value):
            return collections.Counter(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())
        expected = collections.Counter()
        # Explicit locations in this reference book, independent of the detector.
        labels = {8: [(363, 207, 434, 222)], 11: [(405, 53, 474, 78)]}
        with pdfplumber.open(ROOT / "example.pdf") as pdf:
            for page in pdf.pages:
                clean = page.dedupe_chars(tolerance=.5, extra_attrs=("fontname", "size"))
                for word in clean.extract_words():
                    if word["top"] > 740:  # Running footer.
                        continue
                    if not word["upright"] and (word["x1"] < 50 or word["x0"] > 562):
                        continue  # The five known vertical artist credits.
                    x, y = (word["x0"]+word["x1"])/2, (word["top"]+word["bottom"])/2
                    if any(a <= x <= c and b <= y <= d for a,b,c,d in labels.get(page.page_number, [])):
                        continue
                    expected.update(characters(word["text"]))
        self.assertEqual(characters(self.result.text), expected)

    def test_all_seven_tables_keep_their_98_source_cells(self):
        tables = [b for b in self.result.blocks if b.kind == "table"]
        checked, cells = 0, 0
        with pdfplumber.open(ROOT / "example.pdf") as pdf:
            for page in pdf.pages:
                for table in page.dedupe_chars(tolerance=.5, extra_attrs=("fontname", "size")).find_tables():
                    rows = table.extract(x_tolerance=2, y_tolerance=3)
                    keep = [i for i in range(max(map(len, rows))) if any(i < len(r) and r[i] for r in rows)]
                    rows = [[r[i] if i < len(r) else "" for i in keep] for r in rows]
                    actual = next(b for b in tables if b.sources[0].page == page.page_number
                                  and all(abs(a-c) < 1 for a,c in zip(b.sources[0].bbox, table.bbox)))
                    self.assertEqual(len(actual.rows), len(rows))
                    for expected_row, actual_row in zip(rows, actual.rows):
                        self.assertEqual(len(expected_row), len(actual_row))
                        for expected, value in zip(expected_row, actual_row):
                            self.assertEqual(re.sub(r"\s", "", expected or ""), re.sub(r"\s", "", value))
                            cells += 1
                    checked += 1
        self.assertEqual((checked, cells), (7, 98))

    def test_all_paragraphs_are_single_lines_and_have_geometry(self):
        for block in self.result.blocks:
            self.assertNotIn("\n", block.text)
            for source in block.sources:
                self.assertGreaterEqual(source.page, 1)
                self.assertLessEqual(source.page, 11)
                self.assertLess(source.bbox[0], source.bbox[2])


if __name__ == "__main__":
    unittest.main()
