"""PDF-specific resource ownership, extraction and exception translation."""

import math
import re
from collections import Counter

import pdfplumber
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError
from pdfplumber.utils.exceptions import PdfminerException

from .captions import remove_background_captions, remove_figure_captions
from .diagrams import card_panels
from .forms import attach_form_labels, detect_form_regions
from .geometry import body_size, contains, group_lines
from .heading_context import learn_heading_context
from .page_data import PageData, TableData
from .page_selection import parse_pages
from .pdf_symbols import glyph_page, glyph_provenance, recover_symbols
from .pdf_diagrams import extract_paths, extract_mapping_geometry, extract_arrows
from .diagram_mappings import recover_cell_mappings
from .preprocessing import integrate_dropcaps, remove_marginal_credits
from .table_recovery import recover_painted_tables
from .table_structure import CellPaint, recover_table_structure
from .models import BBox, ConversionError, ConversionWarning, Line, Word
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
    for left, space, right in zip(chars, chars[1:], chars[2:]):
        if (not space["text"].isspace() or not left["text"].isalpha()
                or not right["text"].isalpha() or not _same_baseline(left, right)
                or not _same_baseline(left, space)
                or len({c["fontname"] for c in (left, space, right)}) != 1):
            continue
        tolerance = left["size"] * .025
        if (abs(right["x0"] - left["x1"]) <= tolerance
                and any(c["x0"] - tolerance <= space["x0"]
                        and space["x1"] <= c["x1"] + tolerance for c in (left, right))):
            ignored.add(id(space))
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


def _same_baseline(left, right):
    return (left["upright"] and right["upright"]
            and all(c["matrix"][0] > 0 and abs(c["matrix"][1]) < .01 for c in (left, right))
            and abs(left["matrix"][5] - right["matrix"][5]) <= min(left["size"], right["size"]) * .02
            and left.get("non_stroking_color") == right.get("non_stroking_color"))


def _join_split_words(items, chars):
    """Repair word boundaries only between consecutive source glyphs."""
    positions = {id(c): i for i, c in enumerate(chars)}
    runs_with_spaces = {c["text_run"] for c in chars
                        if c.get("text_run") is not None and c["text"].isspace()}
    pending = None
    for item in items:
        if pending is None:
            pending = item
            continue
        left, right = pending["chars"][-1], item["chars"][0]
        if (positions[id(right)] != positions[id(left)] + 1
                or not _same_baseline(left, right) or left.get("symbol") or right.get("symbol")
                or not _split_boundary(pending, item, runs_with_spaces)):
            yield pending
            pending = item
            continue
        pending = dict(pending, text=pending["text"] + item["text"],
                       chars=pending["chars"] + item["chars"],
                       x1=max(pending["x1"], item["x1"]),
                       top=min(pending["top"], item["top"]),
                       bottom=max(pending["bottom"], item["bottom"]))
    if pending is not None:
        yield pending


def _split_boundary(before, after, runs_with_spaces):
    left, right = before["chars"][-1], after["chars"][0]
    gap = (right["x0"] - left["x1"]) / left["size"]
    if not -.025 <= gap <= .36:
        return False
    if before["text"].isdecimal() and after["text"].isdecimal():
        # A smaller digit in a different face can fall outside the word's
        # top band even though it shares the baseline and touches the number.
        return (gap <= .025 and .65 <= right["size"] / left["size"] <= .85
                and left["fontname"] != right["fontname"])
    if not (left["text"].isalpha() and right["text"].isalpha()
            and left["fontname"] == right["fontname"] and abs(left["size"] - right["size"]) < .05):
        return False
    run = left.get("text_run")
    if run is not None and run == right.get("text_run"):
        # A tight join can be split by a blank from another baseline.
        # For wider tracking require a run that explicitly encodes spaces.
        return gap <= .025 or run in runs_with_spaces
    # A sheared glyph can have its own text-show operation in a positioned
    # script. Require it to lean into its predecessor and a spaced right run.
    return (len(before["chars"]) >= 2 and abs(left["matrix"][2]) >= left["size"] * .05
            and left["x0"] < before["chars"][-2]["x1"]
            and right.get("text_run") in runs_with_spaces)


def _frame_cuts_body_flow(box: BBox, page: PageData, lines: list[Line]) -> bool:
    """Reject a fill bbox that repeatedly claims a neighbouring prose tail.

    A curved background's enclosing rectangle is not proof of text ownership.
    Require two aligned, adjacent body lines with ordinary spacing and the same
    face/colour across the cut; one ambiguous line leaves the frame unchanged.
    Confirmed tables, forms and diagram cards cannot supply this evidence.
    """
    size = page.body_size
    protected = [t.bbox for t in page.tables] + page.diagram_boxes
    protected += [b for form in page.form_regions for b in (form.bbox, *form.attachments)]
    candidates: list[tuple[bool, float, Line]] = []
    for line in lines:
        words = line.words
        if (len(words) < 5 or any(not w.upright or abs(w.size-size) > size*.05
                                 or any(g.symbol for g in w.glyphs)
                                 or any(contains(b, w) for b in protected) for w in words)):
            continue
        face = (words[0].font, words[0].color)
        if (any((w.font, w.color) != face for w in words)
                or any(not 0 <= b.bbox[0]-a.bbox[2] <= size*.65
                       for a, b in zip(words, words[1:]))):
            continue
        owned = [contains(box, w) for w in words]
        # Only a prefix or suffix may be the intrusion, never a whole line or
        # an interior span. Check an x edge, not a mixed-baseline top/bottom cut.
        if sum(a != b for a, b in zip(owned, owned[1:])) != 1:
            continue
        inside = [w for w, yes in zip(words, owned) if yes]
        outside = [w for w, yes in zip(words, owned) if not yes]
        suffix = owned[-1]
        edge = box[0] if suffix else box[2]
        if (not line.bbox[0] < edge < line.bbox[2]
                or len(outside) < 4 or sum(w.source_length for w in outside) < 20
                or sum(w.source_length for w in outside) < 4*sum(w.source_length for w in inside)
                or inside[-1].bbox[2]-inside[0].bbox[0] > size*3):
            continue
        anchor = outside[0].bbox[0] if suffix else outside[-1].bbox[2]
        for previous_suffix, previous_anchor, previous in candidates:
            if (suffix == previous_suffix and abs(anchor-previous_anchor) <= size*.5
                    and 0 <= line.bbox[1]-previous.bbox[3] <= size*.8
                    and (previous.words[0].font, previous.words[0].color) == face):
                return True
        candidates.append((suffix, anchor, line))
    return False


def _read_page(page, warn, *, defer_captions: bool = False) -> PageData:
    page = glyph_page(page)
    clean = page.dedupe_chars(tolerance=0.5, extra_attrs=("fontname", "size"))
    clean = recover_symbols(clean, warn)
    source_chars = clean.chars
    ignored_spaces = _overprinted_spaces(source_chars)
    if ignored_spaces:
        clean = clean.filter(lambda obj: id(obj) not in ignored_spaces)
    words = []
    items = clean.extract_words(x_tolerance=2, x_tolerance_ratio=0.18, y_tolerance=3, return_chars=True, expand_ligatures=True)
    for item in _join_split_words(items, source_chars):
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
            words.append(Word(text, (item["x0"], item["top"], item["x1"], item["bottom"]), size, font, item.get("upright", True), angle, color,
                              tuple(glyph_provenance(c) for c in chars)))
    data = PageData(page.page_number, page.width, page.height, words, body_size(words))
    data.images = [(im["x0"], im["top"], im["x1"], im["bottom"]) for im in page.images if not im.get("imagemask")]
    shapes = list(page.rects)+list(page.curves)
    outlines = []
    for shape in shapes:
        if not shape.get("stroke"):
            continue
        points = shape.get("pts", ())
        if shape.get("object_type") != "rect":
            # Near-rectangular closed panels may have small bevelled corners.
            # Curved icons/circles and open connector lines are not field boxes.
            if len(points) < 5 or math.dist(points[0],points[-1]) > data.body_size*.2:
                continue
            segments = [(a,b,math.dist(a,b)) for a,b in zip(points,points[1:])]
            total = sum(length for _,_,length in segments)
            straight = sum(length for a,b,length in segments
                           if min(abs(a[0]-b[0]), abs(a[1]-b[1])) <= data.body_size*.1)
            if not total or straight < total*.75:
                continue
        outlines.append((shape["x0"],shape["top"],shape["x1"],shape["bottom"]))
    data.form_regions = detect_form_regions(data, outlines, card_panels(data, shapes))
    attach_form_labels(data)
    with span("conversion.credits_filter"):
        remove_marginal_credits(data)
    words = data.words
    if not words:
        warn(ConversionWarning("empty_page", "На странице нет извлекаемого текста; возможно, требуется OCR.", (page.page_number,)))
        return data
    if any("\ufffd" in w.text or re.search(r"\(cid:\d+\)", w.text) for w in words):
        warn(ConversionWarning("unmapped_glyph", "В текстовом слое есть символы без корректной кодировки; проверьте страницу.", (page.page_number,)))
    data.diagram_boxes = card_panels(data, list(page.rects)+list(page.curves))
    cells,pointers,marks=extract_mapping_geometry(page,data.body_size)
    data.diagram_mappings=recover_cell_mappings(cells,pointers,words,marks,page=page.page_number,size=data.body_size)
    if data.diagram_boxes:
        data.diagram_paths, data.diagram_issues = extract_paths(page)
        data.diagram_arrows = extract_arrows(page,data.body_size)
        # Opaque banners occlude connector paths just as cards do. A hidden
        # stroke behind a label must not become a direct card-card edge.
        for shape in shapes:
            box=(shape['x0'],shape['top'],shape['x1'],shape['bottom'])
            if (shape.get('fill') and box[2]-box[0]>data.body_size*2
                    and box[3]-box[1]>data.body_size*.7
                    and (box[2]-box[0])*(box[3]-box[1])<data.width*data.height*.2
                    and any(contains(box,w) for w in words)
                    and not any(min(box[2],b[2])>max(box[0],b[0]) and
                                min(box[3],b[3])>max(box[1],b[1]) for b in data.diagram_boxes)):
                # Only linear closed panels: curved artwork's bbox is not
                # evidence of opaque paint throughout its interior.
                ops=shape.get('path',())
                if shape.get('object_type')=='rect' or (ops and all(op[0] in ('m','l','h') for op in ops)
                     and len({round(p[0],3) for p in shape.get('pts',())})==2
                     and len({round(p[1],3) for p in shape.get('pts',())})==2):
                    data.diagram_obstacles.append(box)
        warn(ConversionWarning("diagram_layout", "Текст карточек сохранён; извлекаемые связи обозначены нейтрально. Структура частичная: направление, игровые условия и неподтверждённые элементы требуют проверки по PDF.", (page.page_number,)))
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
                    data.tables.append(TableData(table.bbox, rows, repaired,
                                                 [list(row.cells) for row in table.rows]))
    except (ValueError, TypeError, IndexError) as exc:
        warn(ConversionWarning("table_detection", f"Не удалось разобрать таблицы ({type(exc).__name__}); текст сохранён обычными блоками.", (page.page_number,)))
    data.tables = [table for table in recover_painted_tables(clean,words,data.tables)
                   if not any(min(table.bbox[2],box[2]) > max(table.bbox[0],box[0])+1
                              and min(table.bbox[3],box[3]) > max(table.bbox[1],box[1])+1
                              for box in data.diagram_boxes)]
    paints = [CellPaint((r["x0"], r["top"], r["x1"], r["bottom"]),
                        _text_color(r.get("non_stroking_color")))
              for r in clean.rects if r.get("fill")]
    data.tables = [recover_table_structure(table, words, paints) for table in data.tables]
    frame_lines = group_lines([w for w in words if w.upright], data.body_size, split_gaps=True)
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
        if box not in data.diagram_boxes and _frame_cuts_body_flow(box, data, frame_lines):
            continue
        if not any(all(abs(a-b) < 3 for a,b in zip(box, other)) for other in data.frames):
            data.frames.append(box)
    for box in data.diagram_boxes:
        if not any(all(abs(a-b)<3 for a,b in zip(box,other)) for other in data.frames):
            data.frames.append(box)
    data.separators = [shape["top"] for shape in page.lines if shape["width"] > data.body_size * 4 and shape["height"] < 2]
    data.heading_rules = [(s['x0'],s['top'],s['x1'],s['bottom']) for s in page.lines
                          if s['width'] > data.body_size*4 and s['height'] < 2]
    data.heading_panels = [(s['x0'],s['top'],s['x1'],s['bottom']) for s in shapes
                           if s.get('fill') and s['x1']-s['x0'] >= data.body_size*6
                           and s['bottom']-s['top'] >= data.body_size*.7
                           and s['x1']-s['x0'] <= data.width*.95]
    with span("conversion.dropcaps"):
        integrate_dropcaps(data)
    if not defer_captions:
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
                        data = _read_page(page, warn if number in selected_pages else lambda _: None,
                                          defer_captions=True)
                        extracted.append(data)
                        emit("conversion.page_stats", words=len(data.words), tables=len(data.tables))
                finally:
                    page.close()
            learn_heading_context(extracted)
            for data in extracted:
                with span("conversion.captions_filter"):
                    remove_figure_captions(data)
            # Image-only pages can be dominated by labels rather than prose.
            # Reuse styles of captions confidently identified on other pages;
            # never hardcode a particular book's font, colour, or label text.
            known_styles = set().union(*(p.caption_styles for p in extracted))
            if known_styles:
                for data in extracted:
                    remove_figure_captions(data,known_styles=known_styles)
            # Composited backgrounds need their own stronger document evidence;
            # never relax the ordinary-image bounds or feed back weaker styles.
            with span("conversion.captions_filter"):
                remove_background_captions(extracted)
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
