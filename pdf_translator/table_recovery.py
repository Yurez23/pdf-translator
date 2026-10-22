"""Conservative, read-only recovery from painted cells and grouped stat grids.

``recover_painted_tables`` returns the COMPLETE replacement table list, not a
delta. Assign its result to ``PageData.tables`` after ruling detection, before
frame detection and layout. Inputs (including existing TableData objects) are
never modified. Existing tables win unless a recovered table strictly adds
source text, encloses every overlapping existing table, and preserves all of
their text tokens. Uncertain candidates leave the original tables untouched.

Two narrowly evidenced layouts are supported:
* At least two bands of at least three adjacent, individually painted cells
  with the same column boundaries. One intervening and one trailing unpainted
  row can be inferred at the painted body-row pitch, but only with complete,
  aligned cells and matching numerical evidence in at least half the columns.
* A painted group-header strip above equally populated, aligned label/number
  rows, corroborated by repeated stroked cell outlines in every group. Group
  spans retain explicit physical cells and two header levels for normalization.

This is not a general borderless-table detector. Missing/ambiguous cells,
crossing text, rotation, long gaps, prose, and unsupported spans are declined.
Every word the layout engine would consume inside a recovered bbox must have
one unambiguous cell assignment; words are neither re-extracted nor corrected.
"""

from collections import Counter
from dataclasses import dataclass
import math
import re
from statistics import median

from .geometry import contains, group_lines
from .models import BBox, Word, union_bbox
from .page_data import TableCell, TableData, TableStructure

_TOL = .75
_NUMBER = re.compile(r"[+\-\u2212]?\d+(?:[.,]\d+)*\Z")


@dataclass(frozen=True)
class _Band:
    cells: tuple[BBox, ...]

    @property
    def bbox(self) -> BBox:
        return union_bbox(list(self.cells))


def _shape_box(shape) -> BBox | None:
    try:
        box = tuple(float(shape[k]) for k in ("x0", "top", "x1", "bottom"))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(v) for v in box) or box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def _same_box(a: BBox, b: BBox) -> bool:
    return all(abs(x-y) <= _TOL for x, y in zip(a, b))


def _inside(box: BBox, words: list[Word]) -> list[Word]:
    # Use exactly the layout engine's centre/one-point tolerance, including
    # rotated text. Otherwise recovery could silently consume an unassigned word.
    return [word for word in words if contains(box, word)]


def _fits(box: BBox, word: Word) -> bool:
    return (word.upright and word.angle % 360 == 0
            and box[0]-1 <= word.bbox[0] <= word.bbox[2] <= box[2]+1
            and box[1]-1 <= word.bbox[1] <= word.bbox[3] <= box[3]+1)


def _cell_words(cells: tuple[BBox, ...], words: list[Word]) -> list[list[Word]] | None:
    result: list[list[Word]] = [[] for _ in cells]
    for word in _inside(union_bbox(list(cells)), words):
        matches = [i for i, cell in enumerate(cells) if contains(cell, word)]
        if len(matches) != 1 or not _fits(cells[matches[0]], word):
            return None
        result[matches[0]].append(word)
    return result if all(result) else None


def _text(words: list[Word]) -> str:
    return "\n".join(line.text for line in group_lines(words, median(w.size for w in words)))


def _painted_bands(rects) -> list[_Band]:
    boxes: list[BBox] = []
    for rect in rects:
        box = _shape_box(rect)
        if (rect.get("fill") and box and box[2]-box[0] >= 5 and box[3]-box[1] >= 4
                and not any(_same_box(box, other) for other in boxes)):
            boxes.append(box)
    rows: list[list[BBox]] = []
    for box in sorted(boxes, key=lambda b: (b[1], b[3], b[0])):
        row = next((r for r in rows if abs(r[0][1]-box[1]) <= _TOL
                    and abs(r[0][3]-box[3]) <= _TOL), None)
        if row is None:
            rows.append([box])
        else:
            row.append(box)
    bands = []
    for row in rows:
        # A full-width underpaint is not an additional cell.
        row = [b for b in row if sum(b != c and b[0] <= c[0] and c[2] <= b[2]
                                    for c in row) < 2]
        run: list[BBox] = []
        for box in sorted(row, key=lambda b: b[0]):
            if run and abs(box[0]-run[-1][2]) > _TOL:
                if len(run) >= 3:
                    bands.append(_Band(tuple(run)))
                run = []
            run.append(box)
        if len(run) >= 3:
            bands.append(_Band(tuple(run)))
    return bands


def _same_columns(a: _Band, b: _Band) -> bool:
    return len(a.cells) == len(b.cells) and all(
        abs(x[0]-y[0]) <= _TOL and abs(x[2]-y[2]) <= _TOL
        for x, y in zip(a.cells, b.cells))


def _inferred_matches(row: list[list[Word]], reference: list[list[Word]]) -> bool:
    numerical = 0
    for cell, sample in zip(row, reference):
        size = median(w.size for w in sample)
        if abs(median(w.size for w in cell)-size) > max(.5, size*.08):
            return False
        if len(group_lines(cell, size)) != 1 or len(group_lines(sample, size)) != 1:
            return False
        a, b = union_bbox([w.bbox for w in cell]), union_bbox([w.bbox for w in sample])
        # A column may be left, right or centre aligned; no names/units needed.
        if min(abs(a[0]-b[0]), abs(a[2]-b[2]), abs((a[0]+a[2]-b[0]-b[2])/2)) > max(2, size*.3):
            return False
        numerical += (any(any(ch.isdigit() for ch in w.text) for w in cell)
                      and any(any(ch.isdigit() for ch in w.text) for w in sample))
    return numerical >= max(1, math.ceil(len(row)/2))


def _recover_band_run(bands: list[_Band], words: list[Word]) -> TableData | None:
    if len(bands) < 2:
        return None
    pitch = min(b.bbox[3]-b.bbox[1] for b in bands)
    explicit = [_cell_words(b.cells, words) for b in bands]
    if any(row is None for row in explicit):
        return None
    reference = explicit[-1]
    rows: list[list[list[Word]]] = []
    claimed: list[Word] = []
    for i, band in enumerate(bands):
        if i:
            top, bottom = bands[i-1].bbox[3], band.bbox[1]
            if bottom-top > _TOL:
                if abs(bottom-top-pitch) > max(1, pitch*.08):
                    return None
                cells = tuple((b[0], top, b[2], bottom) for b in band.cells)
                row = _cell_words(cells, words)
                if row is None or not _inferred_matches(row, reference):
                    return None
                rows.append(row)
        rows.append(explicit[i])
    box = union_bbox([band.bbox for band in bands])
    # Extrapolate at most ONE row, with the same text/geometry evidence as a gap.
    tail_cells = tuple((b[0], box[3], b[2], box[3]+pitch) for b in bands[-1].cells)
    tail = _cell_words(tail_cells, words)
    if tail is not None and _inferred_matches(tail, reference):
        rows.append(tail)
        box = (box[0], box[1], box[2], box[3]+pitch)
    for row in rows:
        claimed.extend(w for cell in row for w in cell)
    if Counter(map(id, claimed)) != Counter(map(id, _inside(box, words))):
        return None
    return TableData(box, [[_text(cell) for cell in row] for row in rows])


def _painted_tables(rects, words: list[Word]) -> list[TableData]:
    groups: list[list[_Band]] = []
    for band in sorted(_painted_bands(rects), key=lambda b: b.bbox[1]):
        group = next((g for g in groups if _same_columns(g[0], band)), None)
        if group is None:
            groups.append([band])
        else:
            group.append(band)
    result = []
    for group in groups:
        run: list[_Band] = []
        for band in group:
            if run:
                pitch = min(b.bbox[3]-b.bbox[1] for b in [*run, band])
                gap = band.bbox[1]-run[-1].bbox[3]
                if gap < -_TOL or gap > pitch+max(1, pitch*.08):
                    table = _recover_band_run(run, words)
                    if table:
                        result.append(table)
                    run = []
            run.append(band)
        table = _recover_band_run(run, words)
        if table:
            result.append(table)
    return result


def _outline_groups(shapes, labels: list[Word], values: list[Word], group_count: int,
                    header_bottom: float, size: float) -> list[BBox] | None:
    """Require the same isolated stroked cell in each equal-sized group."""
    centres = [(w.bbox[0]+w.bbox[2])/2 for w in labels]
    spacing = median(b-a for a, b in zip(centres, centres[1:]))
    per_group = len(labels)//group_count
    candidates: list[tuple[int, BBox]] = []
    for shape in shapes:
        box = _shape_box(shape)
        if (not box or not shape.get("stroke") or shape.get("fill")
                or not spacing*.5 <= box[2]-box[0] <= spacing*1.4
                or not header_bottom-2 <= box[1] <= min(w.bbox[3] for w in labels)
                or not max(w.bbox[3] for w in values)-1 <= box[3]
                <= max(w.bbox[3] for w in values)+size*.8):
            continue
        matches = [i for i, (label, value) in enumerate(zip(labels, values))
                   if _fits(box, label) and _fits(box, value)
                   and abs((box[0]+box[2])/2-centres[i]) <= max(1, size*.3)]
        if len(matches) == 1:
            candidates.append((matches[0], box))
    for relative in range(per_group):
        chosen = []
        for group in range(group_count):
            matches = [b for i, b in candidates if i == group*per_group+relative]
            if not matches:
                break
            chosen.append(matches[0])
        if len(chosen) == group_count and all(
            abs(b[1]-chosen[0][1]) <= _TOL and abs(b[3]-chosen[0][3]) <= _TOL
            and abs((b[2]-b[0])-(chosen[0][2]-chosen[0][0])) <= _TOL for b in chosen):
            return chosen
    return None


def _grouped_grid(strip: BBox, words: list[Word], shapes) -> TableData | None:
    headers = _inside(strip, words)
    if not 2 <= len(headers) <= 8 or not all(_fits(strip, w) for w in headers):
        return None
    size = median(w.size for w in headers)
    if len(group_lines(headers, size)) != 1 or strip[3]-strip[1] > size*1.8:
        return None
    headers.sort(key=lambda w: w.bbox[0])
    below = _inside((strip[0], strip[3]+1.01, strip[2], strip[3]+size*3), words)
    lines = group_lines(below, size)
    if len(lines) < 2:
        return None
    labels, values = lines[0].words, lines[1].words
    count = len(labels)
    if (count != len(values) or count < len(headers)*2 or count % len(headers)
            or count > len(headers)*6
            or not all(any(c.isalpha() for c in w.text) and len(w.text) <= 24
                       and not any(c.isdigit() for c in w.text) for w in labels)
            or not all(_NUMBER.fullmatch(w.text) for w in values)):
        return None
    header_bottom = max(w.bbox[3] for w in headers)
    if (not -.5 <= lines[0].bbox[1]-strip[3] <= size*.5
            or not 0 <= lines[1].bbox[1]-lines[0].bbox[3] <= size*.5
            or not all(w.upright and w.angle % 360 == 0 and abs(w.size-size) <= size*.15
                       for w in [*labels, *values])):
        return None
    centres = [(w.bbox[0]+w.bbox[2])/2 for w in labels]
    boundaries = [strip[0], *((a+b)/2 for a, b in zip(centres, centres[1:])), strip[2]]
    for i, (label, value) in enumerate(zip(labels, values)):
        cell = (boundaries[i], lines[0].bbox[1], boundaries[i+1], lines[1].bbox[3])
        if (not _fits(cell, label) or not _fits(cell, value)
                or abs((value.bbox[0]+value.bbox[2])/2-centres[i]) > max(1, size*.25)):
            return None
    per_group = count//len(headers)
    for i, header in enumerate(headers):
        left, right = boundaries[i*per_group], boundaries[(i+1)*per_group]
        if not (left <= header.bbox[0] <= header.bbox[2] <= right
                and abs((header.bbox[0]+header.bbox[2]-left-right)/2) <= (right-left)*.15):
            return None
    outlines = _outline_groups(shapes, labels, values, len(headers), header_bottom, size)
    if outlines is None:
        return None
    box = (strip[0], strip[1], strip[2], max(b[3] for b in outlines))
    assigned = [*headers, *labels, *values]
    if (Counter(map(id, _inside(box, words))) != Counter(map(id, assigned))
            or not all(_fits(box, w) for w in assigned)):
        return None
    spans: list[str | None] = [None]*count
    for i, header in enumerate(headers):
        spans[i*per_group] = header.text
    middle = (lines[0].bbox[3]+lines[1].bbox[1])/2
    edges = (strip[1], strip[3], middle, box[3])
    cells = [TableCell(0, i*per_group, 1, per_group, header.text,
                       (boundaries[i*per_group], strip[1], boundaries[(i+1)*per_group], strip[3]))
             for i, header in enumerate(headers)]
    cells.extend(TableCell(r, i, 1, 1, word.text,
                           (boundaries[i], edges[r], boundaries[i+1], edges[r+1]))
                 for r, row in ((1, labels), (2, values)) for i, word in enumerate(row))
    return TableData(box, [spans, [w.text for w in labels], [w.text for w in values]],
                     structure=TableStructure(tuple(cells), edges, tuple(boundaries), 2))


def _tokens(table: TableData) -> Counter:
    return Counter(token for row in table.rows for cell in row if cell for token in cell.split())


def _overlaps(a: BBox, b: BBox) -> bool:
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _encloses(a: BBox, b: BBox) -> bool:
    return a[0]-1 <= b[0] and a[1]-1 <= b[1] and b[2] <= a[2]+1 and b[3] <= a[3]+1


def recover_painted_tables(page, words: list[Word], existing_tables: list[TableData]) -> list[TableData]:
    """Return a complete, non-mutating replacement table list (see module contract).

    The page is read only for ``rects`` and ``curves``; no extraction, cropping,
    rendering, network or PDF mutations occur. A rejected candidate leaves words
    available to ordinary layout. Preserve the caller's diagram-region exclusion
    and conservative warning policy for unresolved structures when integrating.
    """
    rects = list(page.rects)
    shapes = [*rects, *getattr(page, "curves", ())]
    candidates = _painted_tables(rects, words)
    for rect in rects:
        box = _shape_box(rect)
        if rect.get("fill") and box and box[2]-box[0] >= (box[3]-box[1])*6:
            table = _grouped_grid(box, words, shapes)
            if table:
                candidates.append(table)
    result = list(existing_tables)
    for candidate in candidates:
        overlaps = [table for table in result if _overlaps(candidate.bbox, table.bbox)]
        tokens = _tokens(candidate)
        if any(not _encloses(candidate.bbox, table.bbox) or _tokens(table)-tokens for table in overlaps):
            continue
        if overlaps and not tokens-sum((_tokens(t) for t in overlaps), Counter()):
            continue
        # All overlapping evidence is retained, including token multiplicity.
        # Do not merge two independently represented copies of the same text.
        if sum((_tokens(t) for t in overlaps), Counter())-tokens:
            continue
        overlap_ids = {id(table) for table in overlaps}
        result = [table for table in result if id(table) not in overlap_ids]
        result.append(candidate)
    return sorted(result, key=lambda table: (table.bbox[1], table.bbox[0]))
