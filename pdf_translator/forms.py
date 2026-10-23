"""Conservative static-form geometry, independent of PDF engines and I/O."""

import re
from collections import Counter
from collections.abc import Sequence

from .geometry import contains, group_lines
from .models import BBox, Word, union_bbox
from .page_data import FormRegion, PageData


def encloses(outer: BBox, inner: BBox, tolerance: float = 1.0) -> bool:
    return (outer[0]-tolerance <= inner[0] and outer[1]-tolerance <= inner[1]
            and inner[2] <= outer[2]+tolerance and inner[3] <= outer[3]+tolerance)


def overlaps(a: BBox, b: BBox, tolerance: float = 1.0) -> bool:
    return (min(a[2], b[2])-max(a[0], b[0]) > tolerance
            and min(a[3], b[3])-max(a[1], b[1]) > tolerance)


def protected(word: Word, page: PageData) -> bool:
    return any(contains(box, word) for form in page.form_regions
               for box in (form.bbox, *form.attachments))


def _area(box: BBox) -> float:
    return (box[2]-box[0]) * (box[3]-box[1])


def _style(words: list[Word]) -> tuple:
    styles = Counter()
    for word in words:
        styles[(re.sub(r"^[A-Z]{6}\+", "", word.font), word.color)] += word.source_length
    return styles.most_common(1)[0][0] if styles else ()


def _groups(values: list[float], tolerance: float) -> int:
    anchors = []
    for value in sorted(values):
        if not anchors or value-anchors[-1] > tolerance:
            anchors.append(value)
    return len(anchors)


def field_label(box: BBox, words: list[Word], size: float) -> tuple | None:
    inside = [w for w in words if w.upright and contains(box, w)]
    lines = group_lines(inside, size, split_gaps=True)
    if not lines:
        return None
    first = lines[0]
    # Labels occupy the top of a writable panel, not its centred prose.
    if (len(first.words) > 6 or first.source_length > 60
            or not any(c.isalpha() for c in first.text)
            or first.bbox[1]-box[1] > size*1.5
            or first.bbox[3] > box[1]+(box[3]-box[1])*.55):
        return None
    rest = [w for line in lines[1:] for w in line.words]
    blank = not rest and box[3]-first.bbox[3] >= size*1.5
    value = (rest and len(lines) <= 3 and sum(w.source_length for w in rest) <= 100
             and (_style(rest) != _style(first.words)
                  or any(abs(w.size-first.size) > size*.15 for w in rest)))
    return _style(first.words) if blank or value else None


def detect_form_regions(page: PageData, outlines: list[BBox],
                        diagram_boxes: Sequence[BBox] = ()) -> list[FormRegion]:
    """Require an enclosing panel and repeated writable fields, not just boxes.

    Only disjoint field panels count. A touching cell grid, diagram cards,
    crossing panels or prose outside the fields prevents promotion.
    """
    size = page.body_size
    tolerance = max(1.25, size*.25)
    boxes: list[BBox] = []
    for box in sorted(outlines, key=_area):
        if (box[2]-box[0] < size*2 or box[3]-box[1] < size*1.8
                or _area(box) > page.width*page.height*.95):
            continue
        peer = next((i for i, other in enumerate(boxes)
                     if all(abs(a-b) <= tolerance for a,b in zip(box, other))), None)
        if peer is None:
            boxes.append(box)
        else:
            boxes[peer] = union_bbox([box, boxes[peer]])
    confirmed = []
    for outer in sorted(boxes, key=_area):
        if any(overlaps(outer, diagram) for diagram in diagram_boxes):
            continue
        nested = [box for box in boxes if box != outer and encloses(outer, box)
                  and _area(box) < _area(outer)*.65]
        # The outermost independent panels own any smaller decorations/cells.
        fields = [box for box in nested if not any(other != box and _area(other) > _area(box)
                                                    and encloses(other, box, tolerance)
                                                    for other in nested)]
        if len(fields) < 6 or _groups([b[1] for b in fields], size) < 3:
            continue
        if _groups([b[0] for b in fields], size) < 2:
            continue
        # Real gutters distinguish a composition from the cells of one grid.
        if any(overlaps(a, b, tolerance) for i,a in enumerate(fields) for b in fields[i+1:]):
            continue
        independent = [a for a in fields if not any(a != b and overlaps(a,b,-size*.15) for b in fields)]
        if len(independent) < 4:
            continue
        labels = [(box, field_label(box, page.words, size)) for box in fields]
        labels = [(box, style) for box,style in labels if style is not None]
        styles = Counter(style for _,style in labels)
        if not styles or styles.most_common(1)[0][1] < 4:
            continue
        repeated = [box for box,style in labels if style == styles.most_common(1)[0][0]]
        if _groups([b[1] for b in repeated], size) < 2:
            continue
        # Consistent body paragraphs outside panels mean this is an enclosing
        # layout/background, not a form. Short unboxed labels remain content.
        outside = [w for w in page.words if contains(outer,w)
                   and not any(contains(box,w) for box in fields)]
        rows = group_lines(outside, size, split_gaps=True)
        if any(len(row.words) > 14 or row.source_length > 120 for row in rows):
            continue
        if sum(0 <= b.bbox[1]-a.bbox[3] <= size*.8
               and abs(a.bbox[0]-b.bbox[0]) <= size for a,b in zip(rows, rows[1:])) >= 3:
            continue
        if any(overlaps(outer, old.bbox) for old in confirmed):
            continue
        confirmed.append(FormRegion(outer, tuple(sorted(fields, key=lambda b:(round(b[1]/size), b[0])))))
    return sorted(confirmed, key=lambda form:(form.bbox[1], form.bbox[0]))


def attach_form_labels(page: PageData) -> None:
    """Attach only nearby short display labels, never ordinary body paragraphs."""
    size = page.body_size
    outside = [w for w in page.words if w.upright and not protected(w,page)]
    for form in page.form_regions:
        rows = group_lines(outside, size, split_gaps=True)
        chosen = []
        for row in rows:
            width = row.bbox[2]-row.bbox[0]
            gap = min(abs(row.bbox[1]-form.bbox[3]), abs(form.bbox[1]-row.bbox[3]))
            display = (size*1.18 <= row.size <= size*1.8 and 4 <= len(row.words) <= 18
                       and row.source_length <= 180 and row.text.endswith((".","!","?")))
            centered = abs((row.bbox[0]+row.bbox[2]-form.bbox[0]-form.bbox[2])/2) <= size*2
            if (display and centered and gap <= size*2 and width >= (form.bbox[2]-form.bbox[0])*.6
                    and (row.bbox[1] >= form.bbox[3] or row.bbox[3] <= form.bbox[1])):
                chosen.append(row.bbox)
        # Multiple labels on one edge are ambiguous; retain ordinary layout.
        for above in (True, False):
            matches = [b for b in chosen if (b[3] <= form.bbox[1]) == above]
            if len(matches) == 1:
                form.attachments += tuple(matches)
    # A display sentence between two forms can satisfy both geometric tests.
    # Its ownership is then uncertain: leave it in the main page layout once.
    owners = Counter(box for form in page.form_regions for box in form.attachments)
    for form in page.form_regions:
        form.attachments = tuple(box for box in form.attachments if owners[box] == 1)
