"""Read a PDF, recover its structure, and return UI-independent results."""

import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError
from pdfplumber.utils.exceptions import PdfminerException

from .export import render
from .layout import PageData, TableData, body_size, contains, integrate_dropcaps, layout_page, merge_continuations, remove_running_margins
from .models import BBox, ConversionError, ConversionResult, ConversionWarning, Word
from .text import normalize, vocabulary


def parse_pages(pages: str | Sequence[int] | None, total: int) -> list[int]:
    if pages is None:
        return list(range(1, total + 1))
    if isinstance(pages, str):
        selected: set[int] = set()
        for part in pages.split(","):
            match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
            if not match:
                raise ConversionError("Страницы задаются числами или диапазонами, например: 1,3-5.")
            start, end = int(match[1]), int(match[2] or match[1])
            if not 1 <= start <= end <= total:
                raise ConversionError(f"Недопустимый диапазон страниц: {part.strip()}. В PDF страниц: {total}.")
            selected.update(range(start, end + 1))
    else:
        if any(type(n) is not int or not 1 <= n <= total for n in pages):
            raise ConversionError(f"Номера страниц должны быть целыми числами от 1 до {total}.")
        selected = set(pages)
    if not selected:
        raise ConversionError("Не выбраны страницы для обработки.")
    return sorted(selected)


def _read_page(page, warn) -> PageData:
    clean = page.dedupe_chars(tolerance=0.5, extra_attrs=("fontname", "size"))
    words = []
    for item in clean.extract_words(x_tolerance=2, x_tolerance_ratio=0.18, y_tolerance=3, return_chars=True, expand_ligatures=True):
        chars = item.get("chars", [])
        weights: Counter[float] = Counter(round(c["size"], 1) for c in chars)
        size = weights.most_common(1)[0][0] if weights else 10.0
        font = Counter(c["fontname"] for c in chars).most_common(1)[0][0] if chars else ""
        text = normalize(item["text"])
        if text:
            words.append(Word(text, (item["x0"], item["top"], item["x1"], item["bottom"]), size, font, item.get("upright", True)))
    data = PageData(page.page_number, page.width, page.height, words, body_size(words))
    if not words:
        warn(ConversionWarning("empty_page", "На странице нет извлекаемого текста; возможно, требуется OCR.", (page.page_number,)))
        return data
    if any("\ufffd" in w.text or re.search(r"\(cid:\d+\)", w.text) for w in words):
        warn(ConversionWarning("unmapped_glyph", "В текстовом слое есть символы без корректной кодировки; проверьте страницу.", (page.page_number,)))
    try:
        for table in clean.find_tables():
            rows = table.extract(x_tolerance=2, y_tolerance=3)
            if len(rows) >= 2 and max(map(len, rows), default=0) >= 2 and sum(bool(c and c.strip()) for row in rows for c in row) >= 4:
                if not any(all(abs(a-b) < 2 for a,b in zip(table.bbox, t.bbox)) for t in data.tables):
                    data.tables.append(TableData(table.bbox, rows))
    except (ValueError, TypeError, IndexError) as exc:
        warn(ConversionWarning("table_detection", f"Не удалось разобрать таблицы ({type(exc).__name__}); текст сохранён обычными блоками.", (page.page_number,)))
    # Filled boxes/curves frequently delimit sidebars; table cell fills do not.
    for shape in list(page.rects) + list(page.curves):
        if not shape.get("fill"):
            continue
        box: BBox = (shape["x0"], shape["top"], shape["x1"], shape["bottom"])
        width, height = box[2]-box[0], box[3]-box[1]
        if not (80 < width < page.width * 0.8 and data.body_size * 3 < height < page.height * 0.8):
            continue
        if any(contains(t.bbox, Word("", box, 0)) for t in data.tables):
            continue
        if sum(len(w.text) for w in words if contains(box, w)) < 30:
            continue
        if not any(all(abs(a-b) < 3 for a,b in zip(box, other)) for other in data.frames):
            data.frames.append(box)
    data.separators = [shape["top"] for shape in page.lines if shape["width"] > data.body_size * 4 and shape["height"] < 2]
    integrate_dropcaps(data)
    return data


def convert_pdf(input_path: str | Path, *, pages: str | Sequence[int] | None = None) -> ConversionResult:
    """Convert selected 1-based PDF pages without network access or file writes.

    ``pages`` accepts e.g. ``"1,3-5"`` or a sequence of physical page numbers.
    Warnings are structured data; the CLI is responsible for displaying them.
    """
    source = Path(input_path)
    if not source.is_file():
        raise ConversionError(f"PDF не найден: {source}")
    warnings: list[ConversionWarning] = []
    seen: set[tuple] = set()

    def warn(item: ConversionWarning) -> None:
        key = (item.code, item.message, item.pages)
        if key not in seen:
            seen.add(key)
            warnings.append(item)

    try:
        with pdfplumber.open(source, unicode_norm="NFC") as pdf:
            if pdf.doc.encryption:
                raise ConversionError("PDF защищён шифрованием. Сохраните незашифрованную копию перед конвертацией.")
            if not pdf.pages:
                raise ConversionError("В PDF нет страниц.")
            numbers = parse_pages(pages, len(pdf.pages))
            extracted = []
            for number in numbers:
                page = pdf.pages[number - 1]
                try:
                    extracted.append(_read_page(page, warn))
                finally:
                    page.close()
    except ConversionError:
        raise
    except PDFPasswordIncorrect as exc:
        raise ConversionError("PDF защищён паролем. Сохраните незашифрованную копию перед конвертацией.") from exc
    except PdfminerException as exc:
        underlying = exc.args[0] if exc.args else exc
        if isinstance(underlying, PDFPasswordIncorrect):
            raise ConversionError("PDF защищён паролем. Сохраните незашифрованную копию перед конвертацией.") from exc
        raise ConversionError("Не удалось прочитать PDF: файл повреждён или имеет неподдерживаемую структуру.") from exc
    except (PDFSyntaxError, OSError, ValueError, TypeError, KeyError, IndexError, EOFError) as exc:
        raise ConversionError(f"Не удалось прочитать PDF: файл повреждён или имеет неподдерживаемую структуру ({type(exc).__name__}).") from exc
    if not any(page.words for page in extracted):
        raise ConversionError("В выбранных страницах нет извлекаемого текста. Для сканов требуется OCR, который пока не поддерживается.")
    lexicon = vocabulary(w.text for page in extracted for w in page.words)
    remove_running_margins(extracted)
    blocks = []
    for page in extracted:
        blocks.extend(layout_page(page, lexicon, warn))
    blocks = merge_continuations(blocks, lexicon, warn)
    markdown, text = render(blocks)
    if not text.strip():
        raise ConversionError("После обработки не осталось текста для экспорта.")
    return ConversionResult(markdown, text, tuple(warnings), tuple(blocks))
