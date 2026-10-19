"""PDF-specific resource ownership, extraction and exception translation."""

import math
import re
from collections import Counter

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError
from pdfplumber.utils.exceptions import PdfminerException

from .captions import remove_figure_captions
from .geometry import body_size, contains
from .page_data import PageData, TableData
from .page_selection import parse_pages
from .preprocessing import integrate_dropcaps, remove_marginal_credits
from .models import BBox, ConversionError, ConversionWarning, Word
from .text import normalize
from .telemetry import emit, span


def _text_color(value) -> tuple[float, ...] | None:
    channels = (value,) if isinstance(value, (int, float)) else value
    if not isinstance(channels, (tuple, list)) or not all(isinstance(v, (int, float)) for v in channels):
        return None
    return tuple(round(float(v), 3) for v in channels)


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
            angles = Counter(round(math.degrees(math.atan2(c["matrix"][1], c["matrix"][0]))) % 360 for c in chars)
            angle = angles.most_common(1)[0][0] if angles else 0
            colors = Counter(_text_color(c.get("non_stroking_color")) for c in chars)
            color = colors.most_common(1)[0][0] if colors else None
            words.append(Word(text, (item["x0"], item["top"], item["x1"], item["bottom"]), size, font, item.get("upright", True), angle, color))
    data = PageData(page.page_number, page.width, page.height, words, body_size(words))
    data.images = [(im["x0"], im["top"], im["x1"], im["bottom"]) for im in page.images if not im.get("imagemask")]
    with span("conversion.credits_filter"):
        remove_marginal_credits(data)
    words = data.words
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
    with span("conversion.dropcaps"):
        integrate_dropcaps(data)
    with span("conversion.captions_filter"):
        remove_figure_captions(data)
    return data


def read_pdf(source, pages, warn):
    try:
        with pdfplumber.open(source, unicode_norm="NFC") as pdf:
            if pdf.doc.encryption:
                raise ConversionError("PDF защищён шифрованием. Сохраните незашифрованную копию перед конвертацией.")
            if not pdf.pages:
                raise ConversionError("В PDF нет страниц.")
            numbers = parse_pages(pages, len(pdf.pages))
            selected_pages = set(numbers)
            emit("conversion.opened", pages_total=len(pdf.pages), pages_selected=len(numbers))
            extracted = []
            for number in range(1, len(pdf.pages) + 1):
                page = pdf.pages[number - 1]
                try:
                    # Full-document context keeps heading levels stable for
                    # --pages. Unselected pages cannot emit warnings or prompt.
                    with span("conversion.page_extract", page=number, selected=number in selected_pages):
                        data = _read_page(page, warn if number in selected_pages else lambda _: None)
                        extracted.append(data)
                        emit("conversion.page_stats", words=len(data.words), tables=len(data.tables))
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
    return numbers, extracted
