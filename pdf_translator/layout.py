"""Geometry-based ordering and block recovery within a page."""

import math
import re
from collections import Counter
from dataclasses import replace

from .geometry import contains, group_lines
from .continuations import merge_continuations
from .headings import graphic_label
from .paragraphs import (lead_in as _lead_in, hanging_entry, bold_continuation,
                         italic, returns_left, style_boundary, hanging_run)
from .page_data import PageData
from .forms import encloses, overlaps
from .models import BBox, Block, ConversionWarning, HyphenResolver, Line, SourceRegion, Word, union_bbox
from .text import is_list, join_lines, normalize, terminal as _terminal
from collections.abc import Callable, Sequence

type Warn = Callable[[ConversionWarning], None]


def _gutter(words: list[Word], page: PageData, *, upper: float = 1.15) -> tuple[float, float, float] | None:
    body = [w for w in words if page.body_size * 0.8 <= w.size <= page.body_size * upper and w.text.strip()]
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
        if min(len(left), len(right)) < max(8, len(body) * 0.025):
            continue
        # Both sides must occupy overlapping vertical ranges, not two successive paragraphs.
        overlap = min(max(w.bbox[3] for w in left), max(w.bbox[3] for w in right)) - max(min(w.bbox[1] for w in left), min(w.bbox[1] for w in right))
        if overlap >= page.body_size * 4:
            valid.append((len(run) - abs(split - page.width / 2) * 0.05, split, run[0], run[-1]))
    if not valid:
        # A secondary column can be a larger-type index, not just prose.
        # Include its labels too: counting only the smaller attributes could
        # put the inferred gutter through the final, longer index entries.
        return _gutter(words, page, upper=2.2) if upper == 1.15 else None
    _, split, x0, x1 = max(valid)
    return split, x0, x1


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
    if not normal:
        sizes = Counter()
        for row in lines:
            sizes[round(row.size,1)] += row.source_length
        local_size = sizes.most_common(1)[0][0]
        normal = [row for row in lines if abs(row.size-local_size)<=.5]
    anchor = min((line.bbox[0] for line in normal), default=bounds[0])
    right = max((line.bbox[2] for line in normal), default=bounds[2])
    field_lines = set()
    for i, row in enumerate(lines):
        if row.size <= body * 1.15 and row.text_without_symbols.endswith(":") and row.bold_start:
            field_lines.add(id(row))
            # A wrapped labelled field followed by an empty bold field is a
            # record, not a new bullet (and not a dialogue speaker exchange).
            for preceding in reversed(lines[max(0,i-4):i]):
                if _lead_in(preceding) and ":" in preceding.words[0].text:
                    field_lines.add(id(preceding))
                    break

    def flush() -> None:
        nonlocal pending
        if pending:
            bbox = union_bbox([line.bbox for line in pending])
            caps = [cap for cap in page.dropcaps if abs(cap[0] - bbox[0]) < body
                    and cap[1] <= pending[0].bbox[1] <= cap[3]]
            if caps:
                bbox = union_bbox([bbox, *caps])
            text = join_lines((line.text for line in pending), lexicon, page.number, warn, resolver)
            # Hanging indentation is useful for grouping, but cannot invent
            # a visible list marker absent from the source.
            labelled = kind == 'list_item' and not is_list(pending[0].text)
            result.append(Block(kind, text, (SourceRegion(page.number, bbox),),
                                lines=pending, body_size=body, container_width=right-anchor,
                                container_left=anchor,role='labelled_entry' if labelled else ''))
            pending = []

    for index, line in enumerate(lines):
        text = normalize(line.text)
        if not text:
            continue
        if not page.form_regions and graphic_label(line, body):
            flush()
            result.append(Block("raw", text, (SourceRegion(page.number, line.bbox),),
                                lines=[line], body_size=body, role="graphic_label"))
            warn(ConversionWarning("graphic_text", "Текст графического элемента сохранён отдельно; проверьте его смысл и структуру.", (page.number,)))
            continue
        is_heading = line.size >= body * 1.18 and line.source_length <= 180 and not italic(line)
        # Bold, short standalone labels are headings; bold lead-ins in prose are not.
        all_bold = all(any(s in w.font.lower() for s in ("bold", "demi", "black")) for w in line.words)
        if all_bold and line.source_length < 80 and not _terminal(text) and not is_list(text):
            is_heading = True
        if id(line) in field_lines:
            is_heading = False
        if (all_bold and line.size <= body * 1.15 and index+1 < len(lines)
                and bold_continuation(line, lines[index+1])
                and _lead_in(lines[index+1])
                and 0 <= lines[index+1].bbox[1]-line.bbox[3] <= body * .8):
            is_heading = False
        if is_heading:
            flush()
            # Final levels are assigned from all document styles after layout.
            level = 0
            prev_heading = result[-1] if result else None
            if (prev_heading and prev_heading.kind == "heading" and prev_heading.level == level
                    and prev_heading.lines and abs(prev_heading.lines[-1].size - line.size) < 0.5
                    and 0 <= line.bbox[1] - prev_heading.lines[-1].bbox[3] <= body * 0.45
                    and abs(line.text_left - prev_heading.lines[-1].text_left) < body):
                prev_heading.text += " " + text.replace("\u00ad", "")
                prev_heading.lines.append(line)
                prev_heading.sources = (SourceRegion(page.number, union_bbox([prev_heading.sources[0].bbox, line.bbox])),)
            else:
                result.append(Block("heading", text.replace("\u00ad", ""), (SourceRegion(page.number, line.bbox),),
                                    level=level, lines=[line], body_size=body))
            continue
        marker_note = bool(re.match(r"^[*†‡]\s*\w",text)) and (
            line.size < body * .91 or all(italic(Line([w],w.bbox,w.size)) for w in line.words[1:])
            and any(b.kind=='heading' and '*' in b.text for b in result[-4:]))
        footnote = marker_note or (line.size < body * 0.91 and line.bbox[1] > page.height * 0.72
                    and (bool(re.match(r"^(?:\d+[.)]?|[*†‡])\s*\w", text))
                         or any(0 <= line.bbox[1] - y < body * 5 for y in page.separators)))
        lead_in = _lead_in(line) and not (pending and bold_continuation(pending[-1], line))
        list_item = is_list(text) or lead_in and hanging_entry(lines, index, body) and id(line) not in field_lines
        new_kind = "footnote" if footnote else "list_item" if list_item else "paragraph"
        if pending:
            prev = pending[-1]
            gap = line.bbox[1] - prev.bbox[3]
            under_cap = any(cap[1] <= line.bbox[1] < cap[3] and abs(cap[0] - anchor) < body for cap in page.dropcaps)
            indent = (body * 0.65 < line.bbox[0] - anchor < body * 3
                      and line.bbox[0] > prev.bbox[0] + body * 0.5 and not under_cap)
            first_line_indent = returns_left(lines, index, anchor, body)
            colon_label = _lead_in(line) and any(w.text.endswith(":") for w in line.words if any(
                part in w.font.lower() for part in ("bold","demi","black")))
            if not _terminal(prev.text) and not list_item and not first_line_indent and not colon_label:
                lead_in = False
            indent = indent and (first_line_indent or _terminal(prev.text))
            # A completed first sentence may still have a hanging continuation.
            if kind == "list_item" and not first_line_indent:
                indent = False
            if len(pending) == 1 and hanging_run(lines, index-1, body):
                indent = False
            connector = bool(re.search(r"(?:[,;]|\b(?:and|or))$",prev.text))
            if connector and text[:1].islower() and not colon_label:
                indent = False
                lead_in = False
            short_end = prev.bbox[2] < right - body * 3 and _terminal(prev.text)
            content_left = prev.bbox[0]
            if kind == "list_item" and is_list(pending[0].text) and len(pending[0].words) > 1:
                content_left = max(content_left, pending[0].words[1].bbox[0])
            bold_entry = line.bold_start and _terminal(prev.text) and not prev.bold_start
            italic_field_end = (italic(pending[0]) and italic(prev) and not italic(line)
                                and bool(re.match(r"^[\w -]{1,40}:", pending[0].text))
                                and gap >= body * .2)
            disconnected = (abs(line.bbox[3] - prev.bbox[3]) < body * 0.3
                            and (line.bbox[0] - prev.bbox[2] > body * 3 or prev.bbox[0] - line.bbox[2] > body * 3))
            boundary = (gap > body * 0.65 or is_list(text) or lead_in or footnote and kind != "footnote"
                        or id(line) in field_lines
                        or id(prev) in field_lines and prev.text_without_symbols.endswith(":")
                        or kind == "list_item" and first_line_indent
                        and (_terminal(prev.text) or prev.bbox[2] < right-body*3) and not prev.text.endswith("-")
                        or indent and kind != "footnote" and (kind != "list_item" or short_end)
                        or kind == "list_item" and line.bbox[0] < content_left - body * 0.4
                        and (short_end or _terminal(prev.text))
                        or _terminal(prev.text) and prev.text.rstrip().endswith(('”','"'))
                        and text.startswith(('“','"')) and gap >= body*.5
                        or bold_entry or italic_field_end or disconnected or style_boundary(prev, line, body))
            if boundary:
                flush()
        if not pending:
            kind = new_kind
        pending.append(line)
    flush()
    # Display calculations are content, not outline levels. Require an
    # explicit arithmetic operator and an introducing colon; keep an adjacent
    # wrapped expression together without relying on book-specific wording.
    for i, block in enumerate(result):
        if (block.kind == "heading" and i and result[i-1].text.endswith(":")
                and re.search(r"\s[+×÷=]\s|\s\+\s*$", block.text)):
            block.kind = "raw"
            block.role = "formula"
        elif (block.kind == "heading" and i and result[i-1].role == "formula"
                and result[i-1].lines and block.lines):
            previous = result[i-1]
            a,b = previous.lines[-1],block.lines[0]
            if (abs(a.size-b.size) <= .5 and 0 <= b.bbox[1]-a.bbox[3] <= body * .8
                    and abs((a.bbox[0]+a.bbox[2]-b.bbox[0]-b.bbox[2])/2) <= body * 2):
                previous.text += " " + block.text
                previous.lines += block.lines
                previous.sources = (SourceRegion(page.number, union_bbox([s.bbox for s in (*previous.sources,*block.sources)])),)
                block.text = ""
                block.role = "formula"
                block.kind = "raw"
    result = [block for block in result if block.text]
    return result


def _table_blocks(page: PageData, lexicon: set[str], warn: Warn, resolver: HyphenResolver | None = None) -> list[Block]:
    from .table_structure import normalized_sections
    result = []
    for table in page.tables:
        if table.structure:
            header_box = (table.structure.column_edges[0], table.structure.row_edges[0],
                          table.structure.column_edges[-1], table.structure.row_edges[table.structure.header_rows])
            for index, section in enumerate(normalized_sections(table.structure)):
                if section.group_bbox is not None:
                    result.append(Block("heading", join_lines(section.group.splitlines(), lexicon, page.number, warn, resolver),
                                        (SourceRegion(page.number, section.group_bbox), SourceRegion(page.number, header_box)), role="table_group"))
                rows = [[join_lines(cell.splitlines(), lexicon, page.number, warn, resolver)
                         for cell in row] for row in section.rows]
                box = union_bbox([header_box, section.bbox]) if index == 0 and not section.group else section.bbox
                sources = (SourceRegion(page.number, box),)
                if box == section.bbox:
                    sources += (SourceRegion(page.number, header_box),)
                result.append(Block("table", "", sources, rows=rows))
            continue
        complex_table = table.repaired_gaps or any(cell is None for row in table.rows for cell in row)
        available = [word for word in page.words if contains(table.bbox, word)] if complex_table and table.cell_boxes else []
        if available:
            warn(ConversionWarning("complex_table", "Структура таблицы не подтверждена: доступный текст сохранён отдельным блоком; проверьте строки, столбцы и объединения.", (page.number,)))
            # Conflicting cells can omit or repeat text. Preserve the available
            # source words once, without asserting a grid or resolving line-end
            # hyphens against an uncertain cell assignment.
            text = " / ".join(line.text for line in group_lines(available, page.body_size))
            result.append(Block("raw", text, (SourceRegion(page.number, table.bbox),), role="complex_table"))
            continue
        rows = [[join_lines((cell or "").splitlines(), lexicon, page.number, warn, resolver) for cell in row] for row in table.rows]
        # Ruled book tables sometimes have an extra, entirely empty decorative column.
        keep = [i for i in range(max(map(len, rows), default=0)) if any(i < len(row) and row[i] for row in rows)]
        rows = [[row[i] if i < len(row) else "" for i in keep] for row in rows]
        sources = (SourceRegion(page.number, table.bbox),)
        if complex_table:
            warn(ConversionWarning("complex_table", "Структура таблицы не подтверждена: доступный текст сохранён отдельным блоком; проверьте строки, столбцы и объединения.", (page.number,)))
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


def _layout_page(page: PageData, lexicon: set[str], warn: Warn, resolver: HyphenResolver | None = None,
                 special_blocks: Sequence[Block] = ()) -> list[Block]:
    upright = [w for w in page.words if w.upright]
    rotated = [w for w in page.words if not w.upright]
    table_blocks = _table_blocks(page, lexicon, warn, resolver)
    table_boxes = [t.bbox for t in page.tables]
    words = [w for w in upright if not any(contains(box, w) for box in table_boxes)]
    def encloses(outer, inner):
        return (outer[0]-1 <= inner[0] and outer[1]-1 <= inner[1]
                and inner[2] <= outer[2]+1 and inner[3] <= outer[3]+1)

    frames = [box for box in page.frames if not any(encloses(t,box) for t in table_boxes)]
    framed_blocks: list[Block] = []
    owned_tables = set()
    for frame in frames:
        inside = [w for w in words if contains(frame, w)]
        if not inside:
            continue
        words = [w for w in words if not contains(frame, w)]
        inner_tables = [b for b in table_blocks if encloses(frame,b.sources[0].bbox)]
        owned_tables.update(id(b) for b in inner_tables)
        inner_gutter = _gutter(inside,page) if frame[2]-frame[0] >= page.width*.6 else None
        if inner_gutter:
            split,_,_ = inner_gutter
            children = []
            for left in (True,False):
                column = [w for w in inside if ((w.bbox[0]+w.bbox[2])/2 < split) == left]
                tables = [b for b in inner_tables if ((b.sources[0].bbox[0]+b.sources[0].bbox[2])/2 < split) == left]
                children.extend(_ordered_region(group_lines(column,page.body_size),tables,page,frame,lexicon,warn,resolver))
        else:
            children = _ordered_region(group_lines(inside,page.body_size),inner_tables,page,frame,lexicon,warn,resolver)
        children = merge_continuations(children,lexicon,warn,resolver)
        for child in children:
            if child.kind == "heading":
                child.role = "aside_title"
        framed_blocks.append(Block("aside", " ".join(child.text for child in children),
                                   (SourceRegion(page.number, frame),), children=children,
                                   lines=[line for child in children for line in child.lines],
                                   body_size=page.body_size, container_width=frame[2] - frame[0],
                                   container_left=frame[0], role="diagram_node" if frame in page.diagram_boxes else ""))
    specials = [b for b in table_blocks if id(b) not in owned_tables] + framed_blocks + list(special_blocks)
    if page.diagram_boxes:
        region = union_bbox(page.diagram_boxes)
        diagram_parts = [b for b in specials if encloses(region,b.sources[0].bbox)]
        labels = [w for w in words if contains(region,w)]
        label_ids = {id(w) for w in labels}
        words = [w for w in words if id(w) not in label_ids]
        for row in group_lines(labels,page.body_size,split_gaps=True):
            diagram_parts.append(Block('raw',row.text,(SourceRegion(page.number,row.bbox),),
                                       lines=[row],body_size=page.body_size,role='graphic_label'))
        part_ids = {id(b) for b in diagram_parts}
        specials = [b for b in specials if id(b) not in part_ids]
        diagram_parts.sort(key=lambda b:(b.sources[0].bbox[1],b.sources[0].bbox[0]))
        specials.append(Block('aside',' '.join(b.text for b in diagram_parts),
                              (SourceRegion(page.number,region),),children=diagram_parts,
                              body_size=page.body_size,role='diagram'))
    # A sidebar may be the only text in one column; it still establishes
    # the gutter even though its words are processed in a separate container.
    flow_words = [w for w in upright if not any(contains(box,w) for box in table_boxes+frames+(
        [union_bbox(page.diagram_boxes)] if page.diagram_boxes else []))]
    gutter = _gutter(flow_words,page) or _gutter(
        [w for w in upright if not any(contains(box,w) for box in table_boxes)],page) or _gutter(upright,page)
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
        # A short title above a spanning table belongs to the spanning band,
        # even when its text itself only occupies the left column.
        title_lines = []
        for line in group_lines(ordinary, page.body_size, split_gaps=True):
            styled = line.size >= page.body_size * 1.18 or all(
                any(tag in w.font.lower() for tag in ("bold", "demi", "black")) for w in line.words)
            if (styled and len(line.words) <= 18 and not _terminal(line.text)
                    and any(b.kind in ("table", "raw")
                            and 0 <= b.sources[0].bbox[1]-line.bbox[3] <= page.body_size*1.5
                            and b.sources[0].bbox[0]-1 <= line.bbox[0] < line.bbox[2] <= b.sources[0].bbox[2]+1
                            for b in wide)):
                title_lines.append(line)
        title_ids = {id(w) for line in title_lines for w in line.words}
        ordinary = [w for w in ordinary if id(w) not in title_ids]
        for line in title_lines:
            wide.extend(_blocks_for_lines([line], page, bounds, lexicon, warn, resolver))
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
    for block in ordered:
        if (block.kind in ("paragraph", "aside") and not block.children and block.lines
                and all(italic(line) and line.size >= page.body_size * 1.15 for line in block.lines)):
            block.kind = "aside"
            block.role = "quotation"
    quotations = []
    for block in ordered:
        if block.role == "quotation":
            if quotations and quotations[-1].role == "quotation":
                previous = quotations[-1]
                last = previous.children[-1] if previous.children else previous
                a,b = last.sources[-1].bbox,block.sources[0].bbox
                same_face = last.lines[0].words[0].font == block.lines[0].words[0].font
                if (same_face and -page.body_size*.3 <= b[1]-a[3] <= page.body_size*1.5
                        and min(a[2],b[2]) > max(a[0],b[0])):
                    if not previous.children:
                        previous.children = [Block('paragraph',previous.text,previous.sources,lines=list(previous.lines))]
                    previous.children.append(Block('paragraph',block.text,block.sources,lines=list(block.lines)))
                    previous.text += ' ' + block.text
                    previous.lines += block.lines
                    previous.sources = (SourceRegion(page.number,union_bbox([a,b])),)
                    continue
        quotations.append(block)
    ordered = quotations
    if rotated:
        warn(ConversionWarning("rotated_text", "Поворот текста не поддерживается: фрагмент сохранён отдельно для проверки.", (page.number,)))
        text = join_lines((line.text for line in group_lines(rotated, page.body_size)), lexicon, page.number, warn, resolver)
        ordered.append(Block("raw", text, (SourceRegion(page.number, union_bbox([w.bbox for w in rotated])),), role="rotated"))
    return ordered


def _flat_text(block: Block) -> str:
    if block.children:
        return " ".join(_flat_text(child) for child in block.children)
    if block.kind == "table":
        return " ".join(cell for row in block.rows for cell in row)
    return block.text


def _form_headings(blocks: list[Block]) -> None:
    for block in blocks:
        if block.kind == "heading":
            block.level = 0
            if block.role not in ("table_title", "table_group"):
                # Large handwritten values/numbers are field content. Printed
                # bold panel labels may remain auxiliary internal titles.
                if block.lines and all(any(tag in w.font.lower() for tag in ("bold","demi","black"))
                                       for line in block.lines for w in line.words) and any(c.isalpha() for c in block.text):
                    block.role = "aside_title"
                else:
                    block.kind, block.role = "paragraph", ""
        _form_headings(block.children)


def _loose_form_blocks(page: PageData, lexicon: set[str], warn: Warn,
                       resolver: HyphenResolver | None) -> list[Block]:
    """Keep loose aligned labels with unboxed numeric values when unambiguous."""
    lines = group_lines(page.words,page.body_size,split_gaps=True)
    labels = [line for line in lines if len(line.words) <= 6 and line.source_length <= 60
              and any(c.isalpha() for c in line.text)]
    values = [line for line in lines if line.text.isdecimal()]
    def distance(label, value):
        aligned = abs((label.bbox[0]+label.bbox[2]-value.bbox[0]-value.bbox[2])/2) <= page.body_size*.6
        gap = max(label.bbox[1]-value.bbox[3],value.bbox[1]-label.bbox[3],0)
        return gap if aligned and gap <= page.body_size*3 else float("inf")
    paired = set()
    blocks = []
    for value in values:
        ranked = sorted((distance(label,value),i,label) for i,label in enumerate(labels))
        if not ranked or ranked[0][0] == float("inf") or len(ranked)>1 and ranked[0][0]==ranked[1][0]:
            continue
        _,_,label = ranked[0]
        if id(label) in paired or any(distance(label,other) <= distance(label,value)
                                     for other in values if other is not value):
            continue
        paired.update((id(label),id(value)))
        box = union_bbox([label.bbox,value.bbox])
        blocks.append(Block("paragraph",join_lines([label.text,value.text],lexicon,page.number,warn,resolver),
                            (SourceRegion(page.number,box),),lines=[label,value],body_size=page.body_size))
    for line in lines:
        if id(line) not in paired:
            blocks.extend(_blocks_for_lines([line],page,line.bbox,lexicon,warn,resolver))
    return blocks


def layout_page(page: PageData, lexicon: set[str], warn: Warn, resolver: HyphenResolver | None = None) -> list[Block]:
    if not page.form_regions:
        return _layout_page(page, lexicon, warn, resolver)
    forms = []
    claimed_words: set[int] = set()
    claimed_tables: set[int] = set()
    claimed_frames: set[BBox] = set()
    for form in page.form_regions:
        # A table crossing the region boundary cannot be split or swallowed.
        if any(overlaps(t.bbox,form.bbox) and not encloses(form.bbox,t.bbox) for t in page.tables):
            continue
        boxes = (form.bbox, *form.attachments)
        inside = [w for w in page.words if any(contains(box,w) for box in boxes)]
        tables = [t for t in page.tables if encloses(form.bbox,t.bbox)]
        frames = [box for box in page.frames if encloses(form.bbox,box)]
        inner = replace(page, words=inside, tables=tables, frames=frames, form_regions=[form], diagram_boxes=[])
        # Tables are built once and remain atomic. Their words never enter a
        # field or leftover prose, even when several field outlines overlap it.
        table_blocks = _table_blocks(inner,lexicon,warn,resolver)
        remaining = [w for w in inside if not any(contains(t.bbox,w) for t in tables)]
        groups = [(b.sources[0].bbox,[b]) for b in table_blocks]
        used_frames = set()
        for field in form.fields:
            if any(overlaps(field,t.bbox) for t in tables):
                continue
            words = [w for w in remaining if contains(field,w)]
            # A small value box can have its printed label immediately to its
            # right. Only claim the same-height short run before the next box.
            if field[2]-field[0] <= page.body_size*4 and field[3]-field[1] <= page.body_size*4:
                right = min((b[0] for b in form.fields if b[0] > field[2]
                             and min(b[3],field[3]) > max(b[1],field[1])), default=form.bbox[2])
                adjacent = [w for w in remaining if field[2] <= w.bbox[0] < right
                            and field[1] <= (w.bbox[1]+w.bbox[3])/2 <= field[3]
                            and not any(contains(b,w) for b in form.fields)]
                if (adjacent and min(w.bbox[0] for w in adjacent)-field[2] <= page.body_size
                        and len(adjacent) <= 6 and sum(w.source_length for w in adjacent) <= 60):
                    words += adjacent
            if not words:
                continue
            local_frames = [b for b in frames if encloses(field,b,max(1.25,page.body_size*.25))]
            part = replace(inner, words=words, tables=[], frames=local_frames, images=[])
            children = _layout_page(part,lexicon,warn,resolver)
            groups.append((field,children))
            used = {id(w) for w in words}
            remaining = [w for w in remaining if id(w) not in used]
            used_frames.update(local_frames)
        rest = replace(inner, words=remaining, tables=[], frames=[b for b in frames if b not in used_frames], images=[])
        # Remaining loose labels must not establish a fictitious two-column
        # prose flow across a three-column field grid.
        leftovers = (_layout_page(rest,lexicon,warn,resolver) if rest.frames else
                     _loose_form_blocks(rest,lexicon,warn,resolver))
        for child in leftovers:
            groups.append((child.sources[0].bbox,[child]))
        groups.sort(key=lambda item:(round(item[0][1]/page.body_size),item[0][0]))
        children = [child for _,group in groups for child in group]
        _form_headings(children)
        forms.append(Block("aside", " ".join(_flat_text(child) for child in children),
                           tuple(SourceRegion(page.number,box) for box in boxes), children=children,
                           body_size=page.body_size, role="form_insert"))
        claimed_words.update(id(w) for w in inside)
        claimed_tables.update(id(t) for t in tables)
        claimed_frames.update(frames)
    outside = replace(page, words=[w for w in page.words if id(w) not in claimed_words],
                      tables=[t for t in page.tables if id(t) not in claimed_tables],
                      frames=[b for b in page.frames if b not in claimed_frames], form_regions=[])
    return _layout_page(outside,lexicon,warn,resolver,forms)
