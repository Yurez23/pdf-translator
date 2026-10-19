"""Geometry-based ordering and block recovery within a page."""

import math
import re

from .geometry import contains, group_lines
from .page_data import PageData
from .models import BBox, Block, ConversionWarning, HyphenResolver, Line, SourceRegion, Word, union_bbox
from .text import is_list, join_lines, normalize, terminal as _terminal
from collections.abc import Callable

type Warn = Callable[[ConversionWarning], None]


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


def _lead_in(line: Line) -> bool:
    bold = []
    for word in line.words:
        if not any(s in word.font.lower() for s in ("bold", "demi", "black")):
            break
        bold.append(word.text)
    return bool(bold and len(bold) < len(line.words) and bold[-1].endswith((".", ":")))


def _hanging_lead_in(line: Line, following: Line | None, body: float) -> bool:
    """Bold prose is a list only with evidence of a hanging continuation.

    A first-line paragraph indent goes in the opposite direction. A completed
    one-line paragraph must not borrow the indent of the next paragraph.
    """
    return bool(following and _lead_in(line) and not _terminal(line.text)
                and not _lead_in(following) and not is_list(following.text)
                and 0 <= following.bbox[1] - line.bbox[3] <= body * 0.8
                and body * 0.5 < following.bbox[0] - line.bbox[0] < body * 3
                and abs(following.size - line.size) <= body * 0.15)


def _blocks_for_lines(
    lines: list[Line], page: PageData, bounds: BBox, lexicon: set[str], warn: Warn,
    resolver: HyphenResolver | None = None,
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
            text = join_lines((line.text for line in pending), lexicon, page.number, warn, resolver)
            result.append(Block(kind, text, (SourceRegion(page.number, bbox),),
                                lines=pending, body_size=body, container_width=right-anchor,
                                container_left=anchor))
            pending = []

    for index, line in enumerate(lines):
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
            # Final levels are assigned from all document styles after layout.
            level = 0
            prev_heading = result[-1] if result else None
            if (prev_heading and prev_heading.kind == "heading" and prev_heading.level == level
                    and prev_heading.lines and abs(prev_heading.lines[-1].size - line.size) < 0.5
                    and 0 <= line.bbox[1] - prev_heading.lines[-1].bbox[3] <= body * 0.45
                    and abs(line.bbox[0] - prev_heading.lines[-1].bbox[0]) < body):
                prev_heading.text += " " + text.replace("\u00ad", "")
                prev_heading.lines.append(line)
                prev_heading.sources = (SourceRegion(page.number, union_bbox([prev_heading.sources[0].bbox, line.bbox])),)
            else:
                result.append(Block("heading", text.replace("\u00ad", ""), (SourceRegion(page.number, line.bbox),),
                                    level=level, lines=[line], body_size=body))
            continue
        footnote = (line.size < body * 0.91 and line.bbox[1] > page.height * 0.72
                    and (bool(re.match(r"^(?:\d+[.)]?|[*†‡])\s*\w", text))
                         or any(0 <= line.bbox[1] - y < body * 5 for y in page.separators)))
        lead_in = _lead_in(line)
        following = lines[index + 1] if index + 1 < len(lines) else None
        list_item = is_list(text) or lead_in and _hanging_lead_in(line, following, body)
        new_kind = "footnote" if footnote else "list_item" if list_item else "paragraph"
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


def _table_blocks(page: PageData, lexicon: set[str], warn: Warn, resolver: HyphenResolver | None = None) -> list[Block]:
    result = []
    for table in page.tables:
        rows = [[join_lines((cell or "").splitlines(), lexicon, page.number, warn, resolver) for cell in row] for row in table.rows]
        # Ruled book tables sometimes have an extra, entirely empty decorative column.
        keep = [i for i in range(max(map(len, rows), default=0)) if any(i < len(row) and row[i] for row in rows)]
        rows = [[row[i] if i < len(row) else "" for i in keep] for row in rows]
        complex_table = any(cell is None for row in table.rows for cell in row)
        sources = (SourceRegion(page.number, table.bbox),)
        if complex_table:
            warn(ConversionWarning("complex_table", "Таблица с объединёнными ячейками: текст сохранён отдельным блоком; проверьте структуру.", (page.number,)))
            result.append(Block("raw", " / ".join(" | ".join(row) for row in rows), sources, role="complex_table"))
        else:
            result.append(Block("table", "", sources, rows=rows))
    return result


def _ordered_region(lines: list[Line], specials: list[Block], page: PageData, bounds: BBox, lexicon: set[str], warn: Warn, resolver: HyphenResolver | None = None) -> list[Block]:
    result: list[Block] = []
    remaining = list(lines)
    for block in sorted(specials, key=lambda b: b.sources[0].bbox[1]):
        top = block.sources[0].bbox[1]
        before = [line for line in remaining if line.bbox[1] < top]
        result.extend(_blocks_for_lines(before, page, bounds, lexicon, warn, resolver))
        before_ids = {id(line) for line in before}
        remaining = [line for line in remaining if id(line) not in before_ids]
        result.append(block)
    result.extend(_blocks_for_lines(remaining, page, bounds, lexicon, warn, resolver))
    return result


def layout_page(page: PageData, lexicon: set[str], warn: Warn, resolver: HyphenResolver | None = None) -> list[Block]:
    upright = [w for w in page.words if w.upright]
    rotated = [w for w in page.words if not w.upright]
    table_blocks = _table_blocks(page, lexicon, warn, resolver)
    table_boxes = [t.bbox for t in page.tables]
    words = [w for w in upright if not any(contains(box, w) for box in table_boxes)]
    frames = [box for box in page.frames if not any(contains(t, Word("", box, 0)) for t in table_boxes)]
    framed_blocks: list[Block] = []
    for frame in frames:
        inside = [w for w in words if contains(frame, w)]
        if not inside:
            continue
        words = [w for w in words if not contains(frame, w)]
        children = _blocks_for_lines(group_lines(inside, page.body_size), page, frame, lexicon, warn, resolver)
        for child in children:
            if child.kind == "heading":
                child.role = "aside_title"
        framed_blocks.append(Block("aside", " ".join(child.text for child in children),
                                   (SourceRegion(page.number, frame),), children=children,
                                   lines=[line for child in children for line in child.lines],
                                   body_size=page.body_size, container_width=frame[2] - frame[0],
                                   container_left=frame[0]))
    specials = table_blocks + framed_blocks
    # A sidebar may be the only text in one column; it still establishes
    # the gutter even though its words are processed in a separate container.
    gutter = _gutter([w for w in upright if not any(contains(box, w) for box in table_boxes)], page)
    bounds = union_bbox([w.bbox for w in words]) if words else (0, 0, page.width, page.height)
    if gutter is None:
        ordered = _ordered_region(group_lines(words, page.body_size, split_gaps=True), specials, page, bounds, lexicon, warn, resolver)
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
        span_blocks = _blocks_for_lines(spans, page, bounds, lexicon, warn, resolver)
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
                ordered.extend(_ordered_region(group_lines(column, page.body_size), column_specials, page, col_bounds, lexicon, warn, resolver))
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
        text = join_lines((line.text for line in group_lines(rotated, page.body_size)), lexicon, page.number, warn, resolver)
        ordered.append(Block("raw", text, (SourceRegion(page.number, union_bbox([w.bbox for w in rotated])),), role="rotated"))
    return ordered
