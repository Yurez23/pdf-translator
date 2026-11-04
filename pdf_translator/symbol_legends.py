"""Recover aligned symbol legends from source words, never from prose alone."""

from .geometry import contains, group_lines
from .models import union_bbox
from .page_data import TableData
from .symbols import LABELS


def recover_symbol_legends(page):
    """Return separate two-column legends with a verified glyph on every row.

    Header cells, regularly spaced rows, independent glyphs, aligned meanings,
    and a clear enclosing area are all required. Cell text and symbol labels
    come from the observed words; neither the meanings nor rows are supplied by
    the recognizer. Unsupported or incomplete candidates remain ordinary text.
    """
    size = page.body_size
    words = [w for w in page.words if w.upright and w.angle % 360 == 0]
    results = []
    for icon in (w for w in words if w.text.casefold() == "icon"):
        meanings = [w for w in words if w.text.casefold() == "meaning"
                    and size*.4 <= w.bbox[0]-icon.bbox[2] <= size*3
                    and abs(w.bbox[3]-icon.bbox[3]) <= size*.25]
        if len(meanings) != 1:
            continue
        meaning = meanings[0]
        column_x = meaning.bbox[0]
        # Start from the meaning column, so a missing/unknown icon cannot end
        # an otherwise partial table silently. Stop only at a genuine row gap.
        candidates = [w for w in words if column_x-size*.4 <= w.bbox[0] <= column_x+size*12
                      and icon.bbox[3] < w.bbox[1] <= icon.bbox[3]+size*16]
        lines = [line for line in group_lines(candidates, size, split_gaps=True)
                 if abs(line.bbox[0]-column_x) <= size*.4]
        rows = []
        previous_bottom = max(icon.bbox[3], meaning.bbox[3])
        for line in lines:
            gap = line.bbox[1]-previous_bottom
            if gap < -size*.25:
                rows = []  # conflicting source baselines, do not guess
                break
            if gap > size*.8:
                break
            symbols = [w for w in words
                       if abs((w.bbox[0]+w.bbox[2]-icon.bbox[0]-icon.bbox[2])/2) <= size*.8
                       and abs(w.bbox[3]-line.bbox[3]) <= size*.3]
            if len(symbols) != 1:
                rows = []
                break
            symbol = symbols[0]
            if (len(symbol.glyphs) != 1 or not symbol.glyphs[0].symbol
                    or symbol.glyphs[0].symbol == "unknown"
                    or symbol.text != LABELS.get(symbol.glyphs[0].symbol)
                    or symbol.bbox[2] >= line.bbox[0]):
                rows = []
                break
            rows.append((symbol, line))
            previous_bottom = line.bbox[3]
        if len(rows) < 3 or len({s.glyphs[0].symbol for s, _ in rows}) < 3:
            continue
        # A displaced next cell is evidence of an incomplete/ambiguous key,
        # not the end of a shorter table. Leave the whole candidate in flow.
        if any(previous_bottom <= w.bbox[1] <= previous_bottom+size*.8
               and (abs(w.bbox[0]-column_x) <= size*2
                    or abs((w.bbox[0]+w.bbox[2]-icon.bbox[0]-icon.bbox[2])/2) <= size*.8)
               for w in words):
            continue
        spacings = [b[1].bbox[1]-a[1].bbox[1] for a, b in zip(rows, rows[1:])]
        if max(spacings)-min(spacings) > size*.3:
            continue
        cells = [[icon, meaning]] + [[symbol, *line.words] for symbol, line in rows]
        box = union_bbox([w.bbox for row in cells for w in row])
        claimed = {id(w) for row in cells for w in row}
        if (any(contains(box, w) and id(w) not in claimed for w in page.words)
                or any(_overlap(box, b) for b in page.diagram_boxes)
                or any(_overlap(box, t.bbox) for t in (*page.tables, *results))):
            continue
        boxes = [[icon.bbox, meaning.bbox]] + [
            [symbol.bbox, union_bbox([w.bbox for w in line.words])] for symbol, line in rows]
        texts = [[icon.text, meaning.text]] + [[symbol.text, line.text] for symbol, line in rows]
        results.append(TableData(box, texts, cell_boxes=boxes, role="symbol_legend"))
    return results


def _overlap(a, b):
    return min(a[2], b[2]) > max(a[0], b[0])+1 and min(a[3], b[3]) > max(a[1], b[1])+1
