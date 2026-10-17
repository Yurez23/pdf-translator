"""Geometry-based reading order and paragraph recovery for one/two-column books.

This is intentionally a heuristic layout engine, not an OCR or language model.
All coordinates use PDF points, with a top-left origin (as in pdfplumber).
"""

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import median

from .models import BBox, Block, ConversionWarning, Line, SourceRegion, Word, union_bbox
from .text import is_list, join_lines, normalize

type Warn = Callable[[ConversionWarning], None]


@dataclass
class TableData:
    bbox: BBox
    rows: list[list[str | None]]


@dataclass
class PageData:
    number: int
    width: float
    height: float
    words: list[Word]
    body_size: float
    tables: list[TableData] = field(default_factory=list)
    frames: list[BBox] = field(default_factory=list)
    separators: list[float] = field(default_factory=list)
    dropcaps: list[BBox] = field(default_factory=list)


def contains(box: BBox, word: Word) -> bool:
    x = (word.bbox[0] + word.bbox[2]) / 2
    y = (word.bbox[1] + word.bbox[3]) / 2
    return box[0] - 1 <= x <= box[2] + 1 and box[1] - 1 <= y <= box[3] + 1


def body_size(words: list[Word]) -> float:
    sizes: Counter[float] = Counter()
    for word in words:
        if word.upright:
            sizes[round(word.size, 1)] += len(word.text)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def group_lines(words: list[Word], size: float, *, split_gaps: bool = False) -> list[Line]:
    """Group on baselines so mixed sizes/small capitals stay on the same line."""
    rows: list[list[Word]] = []
    baselines: list[float] = []
    for word in sorted(words, key=lambda w: (w.bbox[3], w.bbox[0])):
        candidates = [i for i in range(max(0, len(rows) - 4), len(rows)) if abs(baselines[i] - word.bbox[3]) <= max(2.0, size * 0.25)]
        if candidates:
            i = min(candidates, key=lambda j: abs(baselines[j] - word.bbox[3]))
            rows[i].append(word)
            baselines[i] = median(w.bbox[3] for w in rows[i])
        else:
            rows.append([word])
            baselines.append(word.bbox[3])
    lines: list[Line] = []
    for row in rows:
        groups: list[list[Word]] = [[]]
        for word in sorted(row, key=lambda w: w.bbox[0]):
            if split_gaps and groups[-1] and word.bbox[0] - groups[-1][-1].bbox[2] > size * 3:
                groups.append([])
            groups[-1].append(word)
        for group in groups:
            weights = Counter()
            for word in group:
                weights[round(word.size, 1)] += len(word.text)
            lines.append(Line(group, union_bbox([w.bbox for w in group]), weights.most_common(1)[0][0]))
    return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))


def integrate_dropcaps(page: PageData) -> None:
    """A large initial may share several lines, but belongs to the first word."""
    for cap in list(page.words):
        if not (cap.upright and len(cap.text) == 1 and cap.text.isalpha() and cap.size >= page.body_size * 2.5):
            continue
        candidates = [w for w in page.words if w is not cap and w.upright and w.text[:1].islower()
                      and 0 <= w.bbox[0] - cap.bbox[2] <= page.body_size * 2
                      and cap.bbox[1] - 2 <= w.bbox[1] <= cap.bbox[1] + cap.size * 0.5]
        if not candidates:
            continue
        first = min(candidates, key=lambda w: (w.bbox[1], w.bbox[0]))
        page.dropcaps.append(cap.bbox)
        first.text = cap.text + first.text
        first.bbox = (cap.bbox[0], first.bbox[1], first.bbox[2], first.bbox[3])
        first.size = page.body_size
        page.words.remove(cap)


def remove_running_margins(pages: list[PageData]) -> None:
    candidates: list[tuple[PageData, Line, tuple[str, str]]] = []
    occurrences: dict[tuple[str, str], set[int]] = {}
    for page in pages:
        for line in group_lines([w for w in page.words if w.upright], page.body_size, split_gaps=True):
            side = "top" if line.bbox[3] < page.height * 0.09 else "bottom" if line.bbox[1] > page.height * 0.91 else ""
            if not side:
                continue
            key = (side, normalize(re.sub(r"\d+", "", normalize(line.text).casefold())))
            occurrences.setdefault(key, set()).add(page.number)
            candidates.append((page, line, key))
    # Recto and verso pages often use different running titles.
    threshold = max(2, math.ceil(len(pages) * 0.3))
    remove: set[int] = set()
    for page, line, key in candidates:
        number = bool(re.fullmatch(r"\d{1,6}", line.text.strip()))
        if number or len(occurrences[key]) >= threshold:
            remove.update(id(w) for w in line.words)
    for page in pages:
        page.words = [w for w in page.words if id(w) not in remove]


def _gutter(words: list[Word], page: PageData) -> tuple[float, float, float] | None:
    body = [w for w in words if page.body_size * 0.8 <= w.size <= page.body_size * 1.15 and w.text.strip()]
    if len(body) < 24:
        return None
    low, high = int(page.width * 0.32), int(page.width * 0.68)
    coverage = [(x, sum(w.bbox[0] < x < w.bbox[2] for w in body)) for x in range(low, high + 1)]
    runs: list[list[int]] = []
    for x, count in coverage:
        if count <= max(0, int(len(body) * 0.002)):
            if not runs or runs[-1][-1] != x - 1:
                runs.append([])
            runs[-1].append(x)
    valid = []
    for run in runs:
        if len(run) < max(8, page.body_size * 0.8):
            continue
        split = (run[0] + run[-1]) / 2
        left = [w for w in body if w.bbox[2] <= split]
        right = [w for w in body if w.bbox[0] >= split]
        if min(len(left), len(right)) < max(12, len(body) * 0.1):
            continue
        # Both sides must occupy overlapping vertical ranges, not two successive paragraphs.
        overlap = min(max(w.bbox[3] for w in left), max(w.bbox[3] for w in right)) - max(min(w.bbox[1] for w in left), min(w.bbox[1] for w in right))
        if overlap >= page.body_size * 4:
            valid.append((len(run) - abs(split - page.width / 2) * 0.05, split, run[0], run[-1]))
    if not valid:
        return None
    _, split, x0, x1 = max(valid)
    return split, x0, x1


def _heading_level(size: float, body: float) -> int:
    return 1 if size >= body * 2 else 2 if size >= body * 1.5 else 3


def _terminal(text: str) -> bool:
    return bool(re.search(r"[.!?…:][\"'’”\])]*$", text.strip()))


def _lead_in(line: Line) -> bool:
    bold = []
    for word in line.words:
        if not any(s in word.font.lower() for s in ("bold", "demi", "black")):
            break
        bold.append(word.text)
    return bool(bold and len(bold) < len(line.words) and bold[-1].endswith((".", ":")))


def _blocks_for_lines(
    lines: list[Line], page: PageData, bounds: BBox, lexicon: set[str], warn: Warn,
    *, forced: str | None = None,
) -> list[Block]:
    if not lines:
        return []
    lines = sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))
    body = page.body_size
    result: list[Block] = []
    pending: list[Line] = []
    kind = "paragraph"
    normal = [line for line in lines if line.size <= body * 1.15]
    anchor = min((line.bbox[0] for line in normal), default=bounds[0])
    right = max((line.bbox[2] for line in normal), default=bounds[2])

    def flush() -> None:
        nonlocal pending
        if pending:
            bbox = union_bbox([line.bbox for line in pending])
            caps = [cap for cap in page.dropcaps if abs(cap[0] - bbox[0]) < body
                    and cap[1] <= pending[0].bbox[1] <= cap[3]]
            if caps:
                bbox = union_bbox([bbox, *caps])
            text = join_lines((line.text for line in pending), lexicon, page.number, warn)
            result.append(Block(forced or kind, text, (SourceRegion(page.number, bbox),), lines=pending, body_size=body, container_width=right-anchor))
            pending = []

    for line in lines:
        text = normalize(line.text)
        if not text:
            continue
        italic = all(any(s in w.font.lower() for s in ("italic", "oblique")) for w in line.words)
        is_heading = line.size >= body * 1.18 and len(text) <= 180 and not italic
        # Bold, short standalone labels are headings; bold lead-ins in prose are not.
        all_bold = all(any(s in w.font.lower() for s in ("bold", "demi", "black")) for w in line.words)
        if all_bold and len(text) < 80 and not _terminal(text) and not is_list(text):
            is_heading = True
        if is_heading:
            flush()
            level = _heading_level(line.size, body)
            prev_heading = result[-1] if result else None
            if (prev_heading and prev_heading.kind == (forced or "heading") and prev_heading.level == level
                    and prev_heading.lines and abs(prev_heading.lines[-1].size - line.size) < 0.5
                    and 0 <= line.bbox[1] - prev_heading.lines[-1].bbox[3] <= body * 0.45
                    and abs(line.bbox[0] - prev_heading.lines[-1].bbox[0]) < body):
                prev_heading.text += " " + text.replace("\u00ad", "")
                prev_heading.lines.append(line)
                prev_heading.sources = (SourceRegion(page.number, union_bbox([prev_heading.sources[0].bbox, line.bbox])),)
            else:
                result.append(Block(forced or "heading", text.replace("\u00ad", ""), (SourceRegion(page.number, line.bbox),),
                                    level=level, lines=[line], body_size=body))
            continue
        footnote = (line.size < body * 0.91 and line.bbox[1] > page.height * 0.72
                    and (bool(re.match(r"^(?:\d+[.)]?|[*†‡])\s*\w", text))
                         or any(0 <= line.bbox[1] - y < body * 5 for y in page.separators)))
        lead_in = _lead_in(line)
        new_kind = "footnote" if footnote else "list_item" if is_list(text) or lead_in else "paragraph"
        if pending:
            prev = pending[-1]
            gap = line.bbox[1] - prev.bbox[3]
            under_cap = any(cap[1] <= line.bbox[1] < cap[3] and abs(cap[0] - anchor) < body for cap in page.dropcaps)
            indent = (body * 0.65 < line.bbox[0] - anchor < body * 3
                      and line.bbox[0] > prev.bbox[0] + body * 0.5 and not under_cap)
            short_end = prev.bbox[2] < right - body * 3 and _terminal(prev.text)
            bold_entry = line.bold_start and _terminal(prev.text) and not prev.bold_start
            disconnected = (abs(line.bbox[3] - prev.bbox[3]) < body * 0.3
                            and (line.bbox[0] - prev.bbox[2] > body * 3 or prev.bbox[0] - line.bbox[2] > body * 3))
            boundary = (gap > body * 0.8 or is_list(text) or lead_in or footnote and kind != "footnote"
                        or indent and kind != "footnote" and (kind != "list_item" or short_end)
                        or kind == "list_item" and short_end and line.bbox[0] < prev.bbox[0] - body * 0.4
                        or bold_entry or disconnected)
            if boundary:
                flush()
        if not pending:
            kind = new_kind
        pending.append(line)
    flush()
    return result


def _table_blocks(page: PageData, lexicon: set[str], warn: Warn) -> list[Block]:
    result = []
    for table in page.tables:
        rows = [[join_lines((cell or "").splitlines(), lexicon, page.number, warn) for cell in row] for row in table.rows]
        # Ruled book tables sometimes have an extra, entirely empty decorative column.
        keep = [i for i in range(max(map(len, rows), default=0)) if any(i < len(row) and row[i] for row in rows)]
        rows = [[row[i] if i < len(row) else "" for i in keep] for row in rows]
        complex_table = any(cell is None for row in table.rows for cell in row)
        sources = (SourceRegion(page.number, table.bbox),)
        if complex_table:
            warn(ConversionWarning("complex_table", "Таблица с объединёнными ячейками: текст сохранён отдельным блоком; проверьте структуру.", (page.number,)))
            result.append(Block("raw", " / ".join(" | ".join(row) for row in rows), sources))
        else:
            result.append(Block("table", "", sources, rows=rows))
    return result


def _ordered_region(lines: list[Line], specials: list[Block], page: PageData, bounds: BBox, lexicon: set[str], warn: Warn) -> list[Block]:
    result: list[Block] = []
    remaining = list(lines)
    for block in sorted(specials, key=lambda b: b.sources[0].bbox[1]):
        top = block.sources[0].bbox[1]
        before = [line for line in remaining if line.bbox[1] < top]
        result.extend(_blocks_for_lines(before, page, bounds, lexicon, warn))
        before_ids = {id(line) for line in before}
        remaining = [line for line in remaining if id(line) not in before_ids]
        result.append(block)
    result.extend(_blocks_for_lines(remaining, page, bounds, lexicon, warn))
    return result


def layout_page(page: PageData, lexicon: set[str], warn: Warn) -> list[Block]:
    upright = [w for w in page.words if w.upright]
    rotated = [w for w in page.words if not w.upright]
    table_blocks = _table_blocks(page, lexicon, warn)
    table_boxes = [t.bbox for t in page.tables]
    words = [w for w in upright if not any(contains(box, w) for box in table_boxes)]
    frames = [box for box in page.frames if not any(contains(t, Word("", box, 0)) for t in table_boxes)]
    framed_blocks: list[Block] = []
    for frame in frames:
        inside = [w for w in words if contains(frame, w)]
        if not inside:
            continue
        words = [w for w in words if not contains(frame, w)]
        framed_blocks.extend(_blocks_for_lines(group_lines(inside, page.body_size), page, frame, lexicon, warn, forced="aside"))
    specials = table_blocks + framed_blocks
    # A sidebar may be the only text in one column; it still establishes
    # the gutter even though its words are processed in a separate container.
    gutter = _gutter([w for w in upright if not any(contains(box, w) for box in table_boxes)], page)
    bounds = union_bbox([w.bbox for w in words]) if words else (0, 0, page.width, page.height)
    if gutter is None:
        ordered = _ordered_region(group_lines(words, page.body_size, split_gaps=True), specials, page, bounds, lexicon, warn)
    else:
        split, g0, g1 = gutter
        spans: list[Line] = []
        ordinary: list[Word] = []
        for line in group_lines(words, page.body_size):
            cross = any(w.bbox[0] < split < w.bbox[2] for w in line.words)
            left = [w for w in line.words if w.bbox[2] <= split]
            right = [w for w in line.words if w.bbox[0] >= split]
            narrow_gap = left and right and min(w.bbox[0] for w in right) - max(w.bbox[2] for w in left) < max(page.body_size * 1.15, (g1-g0) * 0.65)
            if cross or narrow_gap:
                spans.append(line)
            else:
                ordinary.extend(line.words)
        wide = [b for b in specials if b.sources[0].bbox[0] < split - page.body_size and b.sources[0].bbox[2] > split + page.body_size]
        narrow = [b for b in specials if b not in wide]
        span_blocks = _blocks_for_lines(spans, page, bounds, lexicon, warn)
        for block in span_blocks:
            if block.kind == "paragraph":
                block.kind = "aside"
        wide.extend(span_blocks)
        ordered = []
        remaining_words = ordinary
        remaining_specials = narrow
        for boundary in sorted(wide, key=lambda b: b.sources[0].bbox[1]) + [None]:
            cutoff = boundary.sources[0].bbox[1] if boundary else math.inf
            band_words = [w for w in remaining_words if w.bbox[1] < cutoff]
            band_specials = [b for b in remaining_specials if b.sources[0].bbox[1] < cutoff]
            for is_left in (True, False):
                column = [w for w in band_words if ((w.bbox[0] + w.bbox[2]) / 2 < split) == is_left]
                column_specials = [b for b in band_specials if ((b.sources[0].bbox[0] + b.sources[0].bbox[2]) / 2 < split) == is_left]
                col_bounds = (bounds[0], 0, g0, page.height) if is_left else (g1, 0, bounds[2], page.height)
                ordered.extend(_ordered_region(group_lines(column, page.body_size), column_specials, page, col_bounds, lexicon, warn))
            word_ids = {id(w) for w in band_words}
            special_ids = {id(b) for b in band_specials}
            remaining_words = [w for w in remaining_words if id(w) not in word_ids]
            remaining_specials = [b for b in remaining_specials if id(b) not in special_ids]
            if boundary:
                ordered.append(boundary)
    # Bottom-of-page footnotes should not split the main flow between columns.
    ordered = [b for b in ordered if b.kind != "footnote"] + [b for b in ordered if b.kind == "footnote"]
    if rotated:
        warn(ConversionWarning("rotated_text", "Поворот текста не поддерживается: фрагмент сохранён отдельно для проверки.", (page.number,)))
        text = join_lines((line.text for line in group_lines(rotated, page.body_size)), lexicon, page.number, warn)
        ordered.append(Block("raw", text, (SourceRegion(page.number, union_bbox([w.bbox for w in rotated])),), role="rotated"))
    return ordered


def merge_continuations(blocks: list[Block], lexicon: set[str], warn: Warn) -> list[Block]:
    result: list[Block] = []
    for block in blocks:
        # Marginal art credits must not break an otherwise clear page continuation.
        prev = next((b for b in reversed(result) if b.role != "rotated" and b.kind != "footnote"), None)
        if prev and prev.kind == block.kind == "paragraph" and prev.lines and block.lines:
            a, b = prev.sources[-1], block.sources[0]
            next_page = b.page == a.page + 1
            next_column = a.page == b.page and b.bbox[0] > a.bbox[0] + prev.body_size * 4
            last = prev.lines[-1]
            first = block.lines[0]
            full_line = last.bbox[2] - a.bbox[0] >= prev.container_width * 0.75
            if ((next_page or next_column) and not _terminal(prev.text) and (full_line or prev.text.endswith("-"))
                    and (block.text[:1].islower() or prev.text.endswith("-"))
                    and abs(first.size - last.size) <= prev.body_size * 0.15):
                prev.text = join_lines([prev.text, block.text], lexicon, b.page, warn)
                prev.sources += block.sources
                prev.lines += block.lines
                continue
        result.append(block)
    return result
