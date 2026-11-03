"""Keep fragmented lower-arc labels out of the ordinary reading flow."""

import math
import re

from .forms import overlaps
from .models import BBox, Block, ConversionWarning, SourceRegion, Word, union_bbox
from .page_data import PageData


def circle_outline(shape) -> BBox | None:
    """Recognize a stroked circle drawn as four cubic quarter arcs."""
    path = shape.get("path", ())
    if not shape.get("stroke") or tuple(op[0] for op in path) not in (
        ("m", "c", "c", "c", "c"), ("m", "c", "c", "c", "c", "h"),
    ):
        return None
    x0, y0, x1, y1 = box = (shape["x0"], shape["top"], shape["x1"], shape["bottom"])
    radius = (x1 - x0) / 2
    if radius <= 0 or abs(y1 - y0 - 2 * radius) > radius * .04:
        return None
    x, y = (x0 + x1) / 2, (y0 + y1) / 2
    cardinals = ((x0, y), (x, y0), (x1, y), (x, y1))
    ends = [op[-1] for op in path[:5]]
    indices = [min(range(4), key=lambda i: math.dist(point, cardinals[i])) for point in ends]
    if (len(set(indices[:4])) != 4 or indices[0] != indices[-1]
            or any(math.dist(point, cardinals[i]) > radius * .04 for point, i in zip(ends, indices))):
        return None
    for start, (_, control1, control2, end) in zip(ends, path[1:5]):
        if abs(math.dist(start, end) / radius - math.sqrt(2)) > .06:
            return None
        # A quarter-circle cubic's handles point along the other endpoint's radius.
        for point, other, handle in ((start, end, control1), (end, start, control2)):
            expected = (point[0] + .55 * (other[0] - x), point[1] + .55 * (other[1] - y))
            if math.dist(handle, expected) > radius * .05:
                return None
    return box


def _center(box: BBox) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _arc_words(box: BBox, words: list[Word]) -> list[Word]:
    x, y = _center(box)
    radius = (box[2] - box[0]) / 2
    candidates = []
    for word in words:
        if not word.text.isalpha() or not word.glyphs or any(g.symbol for g in word.glyphs):
            continue
        gx, gy = _center(word.glyphs[0].bbox)
        if gy <= y or not .62 <= math.hypot(gx-x, gy-y) / radius <= 1.02:
            continue
        angle = (word.angle + 180) % 360 - 180
        if abs(angle - math.degrees(math.atan2(gx-x, gy-y))) <= 16:
            candidates.append((_center(word.bbox)[0], angle, word))
    candidates.sort(key=lambda item: item[0])
    if len(candidates) < 2 or candidates[0][1] > -15 or candidates[-1][1] < 15:
        return []
    run = [word for _, _, word in candidates]
    if len({(w.font, w.color) for w in run}) != 1:
        return []
    if any(bx <= ax or ba <= aa or b.bbox[0] - a.bbox[2] > min(a.size, b.size) * .2
           for (ax, aa, a), (bx, ba, b) in zip(candidates, candidates[1:])):
        return []
    return run


def circular_label_blocks(page: PageData, tables: list[Block]) -> tuple[list[Block], set[int], bool]:
    """Join only source fragments corroborated by an intact token below."""
    protected = [t.bbox for t in page.tables] + page.frames + page.diagram_boxes
    protected += [box for form in page.form_regions for box in (form.bbox, *form.attachments)]
    circles = [box for box in page.circle_outlines if not any(overlaps(box, other) for other in protected)]
    runs = [_arc_words(box, page.words) for box in circles]
    blocks, claimed, fallback = [], set(), False
    for box, run in zip(circles, runs):
        ids = {id(w) for w in run}
        if not ids or any(ids.intersection(map(id, other)) for other in runs if other is not run):
            continue
        token = "".join(w.text for w in run)
        x, _ = _center(box)
        confirmed = any(
            table.kind == "table" and table.sources[0].bbox[1] >= box[3]
            and table.sources[0].bbox[0] <= x <= table.sources[0].bbox[2]
            and any(token.casefold() in re.findall(r"[^\W\d_]+", cell.casefold())
                    for row in table.rows for cell in row)
            for table in tables
        )
        fallback |= not confirmed
        bounds = union_bbox([w.bbox for w in run])
        text = token if confirmed else " / ".join(w.text for w in run)
        blocks.append(Block("raw", text, (SourceRegion(page.number, bounds),), role="graphic_label"))
        claimed.update(ids)
    return blocks, claimed, fallback
