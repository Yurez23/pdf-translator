"""PDF-specific resource ownership, extraction and exception translation."""

import math
import re
from collections import Counter

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError
from pdfplumber.utils.exceptions import PdfminerException

from .captions import remove_figure_captions
from .diagrams import card_panels
from .geometry import body_size, contains
from .page_data import PageData, TableData
from .page_selection import parse_pages
from .preprocessing import integrate_dropcaps, remove_marginal_credits
from .table_recovery import recover_painted_tables
from .models import BBox, ConversionError, ConversionWarning, Word
from .text import normalize
from .telemetry import emit, span


def _text_color(value) -> tuple[float, ...] | None:
    channels = (value,) if isinstance(value, (int, float)) else value
    if not isinstance(channels, (tuple, list)) or not all(isinstance(v, (int, float)) for v in channels):
        return None
    return tuple(round(float(v), 3) for v in channels)


def _repair_table_gaps(table, rows, words: list[Word]) -> bool:
    """Keep text in holes caused by spurious short ruling segments.

    Only recover words whose centres are inside the table but outside EVERY
    detected cell. Use the containing row band and horizontal cell interval;
    never overwrite extracted cell text or infer a missing value. The caller
    retains the complex/raw warning for such damaged grids.
    """
    recovered = False
    cells = [cell for row in table.rows for cell in row.cells if cell]
    for word in words:
        if not word.upright or not contains(table.bbox, word) or any(contains(cell, word) for cell in cells):
            continue
        x, y = (word.bbox[0]+word.bbox[2])/2, (word.bbox[1]+word.bbox[3])/2
        for index, row in enumerate(table.rows):
            boxes = [cell for cell in row.cells if cell]
            if not boxes or not (min(c[1] for c in boxes) <= y <= max(c[3] for c in boxes)):
                continue
            matches = [i for i, cell in enumerate(row.cells) if cell and cell[0] <= x <= cell[2]]
            if len(matches) == 1:
                column = matches[0]
                rows[index][column] = " ".join(filter(None, [rows[index][column], word.text]))
                recovered = True
                break
    return recovered


def _overprinted_spaces(chars) -> set[int]:
    """Identify non-advancing spaces painted on top of a word's next glyph.

    This is coordinate evidence, not dictionary-based spelling correction.
    Ordinary spaces (including narrow justified ones) are left untouched.
    """
    rows = {}
    for char in chars:
        if char.get("upright", True):
            rows.setdefault((round(char["top"], 1), char["fontname"], round(char["size"], 1)), []).append(char)
    ignored = set()
    for row in rows.values():
        letters = [c for c in row if c["text"].isalpha()]
        for space in (c for c in row if c["text"].isspace()):
            tolerance = max(.1, space["size"] * .02)
            right = [c for c in letters if abs(c["x0"]-space["x0"]) <= tolerance
                     and c["x1"] >= space["x1"]-tolerance]
            left = [c for c in letters if 0 <= space["x0"]-c["x1"] <= space["size"]*.2]
            if right and left:
                ignored.add(id(space))
    # Some PDFs paint a blank from a symbol font on top of the INITIAL glyph
    # in another face. It separates the word in extraction despite having no
    # independent advance in the rendered text. Do not decode the symbol font.
    by_top = {}
    for char in chars:
        if char.get('upright',True) and char['text'].isalpha():
            by_top.setdefault(int(char['top']),[]).append(char)
    for space in (c for c in chars if c.get('upright',True) and c['text'].isspace()):
        for band in range(int(space['top'])-1,int(space['top'])+2):
            if any(letter['fontname'] != space['fontname']
                   and abs(letter['x0']-space['x0']) <= max(.1,space['size']*.02)
                   and abs(letter['top']-space['top']) <= space['size']*.1
                   for letter in by_top.get(band,())):
                ignored.add(id(space))
                break
    return ignored


def _read_page(page, warn) -> PageData:
    clean = page.dedupe_chars(tolerance=0.5, extra_attrs=("fontname", "size"))
    ignored_spaces = _overprinted_spaces(clean.chars)
    if ignored_spaces:
        clean = clean.filter(lambda obj: id(obj) not in ignored_spaces)
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
    data.diagram_boxes = card_panels(data, list(page.rects)+list(page.curves))
    if data.diagram_boxes:
        warn(ConversionWarning("diagram_layout", "Карточки схемы сохранены отдельными блоками; графические связи и значки не восстановлены.", (page.page_number,)))
    try:
        for table in clean.find_tables():
            # Connector lines through a card grid are not table ruling. They
            # produce false cells that clip and duplicate the card text.
            if any(min(table.bbox[2],box[2]) > max(table.bbox[0],box[0])+1
                   and min(table.bbox[3],box[3]) > max(table.bbox[1],box[1])+1
                   for box in data.diagram_boxes):
                continue
            rows = table.extract(x_tolerance=2, y_tolerance=3)
            repaired = _repair_table_gaps(table, rows, words)
            if len(rows) >= 2 and max(map(len, rows), default=0) >= 2 and sum(bool(c and c.strip()) for row in rows for c in row) >= 4:
                if not any(all(abs(a-b) < 2 for a,b in zip(table.bbox, t.bbox)) for t in data.tables):
                    data.tables.append(TableData(table.bbox, rows, repaired))
    except (ValueError, TypeError, IndexError) as exc:
        warn(ConversionWarning("table_detection", f"Не удалось разобрать таблицы ({type(exc).__name__}); текст сохранён обычными блоками.", (page.page_number,)))
    data.tables = [table for table in recover_painted_tables(clean,words,data.tables)
                   if not any(min(table.bbox[2],box[2]) > max(table.bbox[0],box[0])+1
                              and min(table.bbox[3],box[3]) > max(table.bbox[1],box[1])+1
                              for box in data.diagram_boxes)]
    # Filled boxes/curves frequently delimit sidebars; table cell fills do not.
    for shape in list(page.rects) + list(page.curves):
        if not shape.get("fill"):
            continue
        box: BBox = (shape["x0"], shape["top"], shape["x1"], shape["bottom"])
        width, height = box[2]-box[0], box[3]-box[1]
        if not (80 < width < page.width * 0.9 and data.body_size * 3 < height < page.height * 0.8):
            continue
        if any(min(box[2],p[2]) > max(box[0],p[0])+1
               and min(box[3],p[3]) > max(box[1],p[1])+1
               and not all(abs(a-b)<3 for a,b in zip(box,p)) for p in data.diagram_boxes):
            continue  # Backgrounds/connectors overlapping cards are not text boxes.
        if any(contains(t.bbox, Word("", box, 0)) for t in data.tables):
            continue
        if sum(len(w.text) for w in words if contains(box, w)) < 30:
            continue
        if not any(all(abs(a-b) < 3 for a,b in zip(box, other)) for other in data.frames):
            data.frames.append(box)
    for box in data.diagram_boxes:
        if not any(all(abs(a-b)<3 for a,b in zip(box,other)) for other in data.frames):
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
            # Image-only pages can be dominated by labels rather than prose.
            # Reuse styles of captions confidently identified on other pages;
            # never hardcode a particular book's font, colour, or label text.
            known_styles = set().union(*(p.caption_styles for p in extracted))
            if known_styles:
                for data in extracted:
                    remove_figure_captions(data,known_styles=known_styles)
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
