"""Validate neutral cell geometry and normalize supported spans before export.

No PDF objects or prose matching: every slot needs exactly one physical owner,
and every source word needs exactly one cell. Failed evidence leaves raw data
untouched. Body colspans become separate rectangular sections with composite
headers, so a shared value is written once. Vertical labels are propagated only
over the rows actually covered by their physical cell.
"""

from collections import Counter
from dataclasses import dataclass, replace
import math
from statistics import median

from .geometry import contains, group_lines
from .models import BBox, Word
from .page_data import TableCell, TableData, TableStructure

_TOL = 1.25


@dataclass(frozen=True)
class CellPaint:
    bbox: BBox
    color: tuple[float, ...] | None


@dataclass(frozen=True)
class TableSection:
    bbox: BBox
    rows: tuple[tuple[str, ...], ...]
    group: str = ""
    group_bbox: BBox | None = None


def _edges(values):
    groups = []
    for value in sorted(set(values)):
        if groups and value-groups[-1][0] <= _TOL:
            groups[-1].append(value)
        else:
            groups.append([value])
    return tuple(median(group) for group in groups)


def _same(a, b):
    return all(abs(x-y) <= _TOL for x, y in zip(a, b))


def _fits(box, word):
    return (word.upright and word.angle % 360 == 0
            and box[0]-1 <= word.bbox[0] <= word.bbox[2] <= box[2]+1
            and box[1]-1 <= word.bbox[1] <= word.bbox[3] <= box[3]+1)


def _paint(box, paints):
    matches = [p.color for p in paints if _same(box, p.bbox)]
    return matches[0] if matches and all(c == matches[0] for c in matches) else None


def _text(words):
    if not words:
        return ""
    return "\n".join(line.text for line in group_lines(words, median(w.size for w in words)))


def structure_from_boxes(boxes, words, paints=(), *, header_rows=None):
    """Return a complete tiling or None, including real empty physical cells."""
    unique = []
    for box in boxes:
        if (len(box) != 4 or not all(math.isfinite(v) for v in box)
                or box[0] >= box[2] or box[1] >= box[3]):
            return None
        if not any(_same(box, other) for other in unique):
            unique.append(box)
    if len(unique) < 4:
        return None
    xs = _edges(x for b in unique for x in (b[0], b[2]))
    ys = _edges(y for b in unique for y in (b[1], b[3]))
    if len(xs) < 3 or len(ys) < 3:
        return None
    bbox = (xs[0], ys[0], xs[-1], ys[-1])
    slots = {}
    cells = []
    claimed = []
    for box in unique:
        c, end_c = (min(range(len(xs)), key=lambda i: abs(xs[i]-v)) for v in (box[0], box[2]))
        r, end_r = (min(range(len(ys)), key=lambda i: abs(ys[i]-v)) for v in (box[1], box[3]))
        if c == end_c or r == end_r:
            return None
        for row in range(r, end_r):
            for col in range(c, end_c):
                if (row, col) in slots:
                    return None  # overlapping detector cells are not spans
                slots[row, col] = len(cells)
        owned = [w for w in words if contains(box, w)]
        if any(not _fits(box, w) for w in owned):
            return None
        claimed.extend(owned)
        cells.append(TableCell(r, c, end_r-r, end_c-c, _text(owned), box))
    if (len(slots) != (len(xs)-1)*(len(ys)-1)
            or Counter(map(id, claimed)) != Counter(id(w) for w in words if contains(bbox, w))):
        return None
    top = [cell for cell in cells if cell.row == 0]
    depth = header_rows or max(cell.rowspan for cell in top)
    if depth >= len(ys)-1 or any(not cell.text for cell in top):
        return None
    # A partial-width horizontal header needs a lower header level. Its cells
    # must be corroborated by matching header paint, never inferred from prose.
    if header_rows is None and any(cell.colspan > 1 for cell in top):
        depth = max(depth, 2)
        if depth >= len(ys)-1:
            return None
        for cell in cells:
            if cell.row < depth and cell.row:
                parents = [p for p in top if p.column <= cell.column
                           and cell.column+cell.colspan <= p.column+p.colspan]
                if (len(parents) != 1 or not cell.text or
                        _paint(cell.bbox, paints) is None or
                        _paint(cell.bbox, paints) != _paint(parents[0].bbox, paints)):
                    return None
    groups = []
    header_colors = {_paint(cell.bbox, paints) for cell in top} - {None}
    for cell in cells:
        if cell.row >= depth and cell.colspan == len(xs)-1:
            if (cell.rowspan != 1 or not cell.text or
                    _paint(cell.bbox, paints) not in header_colors):
                return None
            groups.append(cell.row)
    if groups and (groups[-1] == len(ys)-2 or any(b == a+1 for a, b in zip(groups, groups[1:]))):
        return None  # no body row establishes the scope of an isolated label
    # A cell spanning both header and body is unresolved, not a blank header.
    if any(cell.row < depth < cell.row+cell.rowspan for cell in cells):
        return None
    if any(cell.row < depth and cell.colspan > 1 and cell.row+cell.rowspan == depth for cell in cells):
        return None  # deeper hierarchy has not been established
    if any(cell.row >= depth and cell.rowspan > 1
           and (cell.column != 0 or cell.colspan != 1 or len(cell.text.split()) > 4)
           for cell in cells):
        return None  # propagate only short leading labels, not shared body prose
    return TableStructure(tuple(cells), ys, xs, depth, tuple(sorted(groups)))


def _painted_rectangle(table, words, paints):
    """Repair conflicting ruling only with full repeated painted column bands.

    Existing detector cells must corroborate every body row. Header paint can
    replace a clipped/missing header cell; it cannot extrapolate body content.
    A title protruding above the new bbox remains in ordinary page layout.
    """
    inside = [p for p in paints if table.bbox[0]-_TOL <= p.bbox[0]
              and p.bbox[2] <= table.bbox[2]+_TOL
              and table.bbox[1]-_TOL <= p.bbox[1] < p.bbox[3] <= table.bbox[3]+_TOL]
    bands = {}
    for paint in inside:
        bands.setdefault((round(paint.bbox[1], 2), round(paint.bbox[3], 2)), []).append(paint.bbox)
    raw = [box for row in table.cell_boxes for box in row if box]
    for key, band in sorted(bands.items()):
        band = sorted(set(band))
        if len(band) < 3 or not all(abs(a[2]-b[0]) <= _TOL for a, b in zip(band, band[1:])):
            continue
        if abs(band[0][0]-table.bbox[0]) > _TOL or abs(band[-1][2]-table.bbox[2]) > _TOL:
            continue
        xs = [band[0][0], *(b[2] for b in band)]
        repeated = [other for k, other in bands.items() if k > key and len(other) == len(band)
                    and all(abs(a[0]-b[0]) <= _TOL and abs(a[2]-b[2]) <= _TOL
                            for a, b in zip(band, sorted(other)))]
        if not repeated:
            continue
        rows = {}
        for box in raw:
            if box[1] < band[0][3]-_TOL:
                continue
            if any(abs(box[0]-left) <= _TOL and abs(box[2]-right) <= _TOL
                   for left, right in zip(xs, xs[1:])):
                rows.setdefault((box[1], box[3]), []).append(box)
        boxes = list(band)
        bottom = band[0][3]
        for (top, end), row in sorted(rows.items()):
            row = sorted(set(row))
            if abs(top-bottom) > _TOL or len(row) != len(band):
                break
            if not all(abs(b[0]-left) <= _TOL and abs(b[2]-right) <= _TOL
                       for b, left, right in zip(row, xs, xs[1:])):
                break
            boxes.extend(row)
            bottom = end
        if abs(bottom-table.bbox[3]) > _TOL:
            continue
        structure = structure_from_boxes(boxes, words, paints, header_rows=1)
        if structure is None:
            continue
        bbox = (xs[0], band[0][1], xs[-1], bottom)
        # Never discard any extracted text, including text outside the repaired
        # rectangle. Outside words remain available to the ordinary layout.
        available = Counter(t for w in words if contains(table.bbox, w) for t in w.text.split())
        original = Counter(t for row in table.rows for cell in row if cell for t in cell.split())
        if original-available:
            continue
        return replace(table, bbox=bbox, structure=structure)
    return None


def recover_table_structure(table, words, paints):
    """Preserve simple/unsupported tables; annotate only confidently tiled grids."""
    if table.structure or not table.cell_boxes:
        return table
    if not table.repaired_gaps and not any(c is None for row in table.rows for c in row):
        return table
    structure = structure_from_boxes([b for row in table.cell_boxes for b in row if b], words, paints)
    if structure is not None:
        return replace(table, structure=structure)
    return _painted_rectangle(table, words, paints) or table


def normalized_sections(structure):
    """Establish headers and groups without making the exporter infer semantics."""
    cells = structure.cells
    columns = len(structure.column_edges)-1
    owners = {(r, c): cell for cell in cells
              for r in range(cell.row, cell.row+cell.rowspan)
              for c in range(cell.column, cell.column+cell.colspan)}
    headers = []
    for col in range(columns):
        path = []
        seen = set()
        for row in range(structure.header_rows):
            cell = owners[row, col]
            identity = (cell.row, cell.column)
            if identity not in seen:
                path.append(cell.text)
                seen.add(identity)
        headers.append(" / ".join(path))
    sections = []
    group = ""
    group_bbox = None
    start = structure.header_rows
    while start < len(structure.row_edges)-1:
        if start in structure.group_rows:
            cell = owners[start, 0]
            group, group_bbox = cell.text, cell.bbox
            start += 1
            continue
        row_cells = [owners[start, c] for c in range(columns)]
        pattern = tuple((cell.column, cell.colspan) for c, cell in enumerate(row_cells) if cell.column == c)
        rows = [tuple(" / ".join(dict.fromkeys(headers[c:c+span])) for c, span in pattern)]
        end = start
        while end < len(structure.row_edges)-1 and end not in structure.group_rows:
            current = tuple((owners[end, c].column, owners[end, c].colspan)
                            for c in range(columns) if owners[end, c].column == c)
            if current != pattern:
                break
            rows.append(tuple(owners[end, c].text for c, _ in pattern))
            end += 1
        bbox = (structure.column_edges[0], structure.row_edges[start],
                structure.column_edges[-1], structure.row_edges[end])
        sections.append(TableSection(bbox, tuple(rows), group, group_bbox))
        group_bbox = None  # emit the group label once, even across span patterns
        start = end
    return sections
