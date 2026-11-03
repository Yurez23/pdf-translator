"""Recognize separated text-card grids without interpreting graphical edges."""

from .geometry import contains
from .models import BBox, Block, SourceRegion, union_bbox
from .page_data import PageData
from .paragraphs import bold, italic
from .text import terminal


def join_card_title(children: list[Block], panel: BBox) -> None:
    """Join a split, symbol-led title at the top of a confirmed card."""
    if len(children) < 3:
        return
    first, second, following = children[:3]
    if (first.kind not in ("heading", "paragraph") or first.role
            or second.kind != "heading" or second.role
            or len(first.lines) != 1 or len(second.lines) != 1
            or not following.lines):
        return
    start, end, detail = first.lines[0], second.lines[0], following.lines[0]
    symbol, *lettering = start.words
    if (not symbol.glyphs or not all(g.symbol for g in symbol.glyphs) or not lettering
            or any(g.symbol for word in (*lettering, *end.words) for g in word.glyphs)
            or terminal(start.text_without_symbols) or not italic(detail)):
        return
    face = lettering[0]
    if not all(bold(word) and (word.font, word.color) == (face.font, face.color)
               and abs(word.size - face.size) <= .5 for word in (*lettering, *end.words)):
        return
    size = face.size
    if (not 0 <= start.bbox[1] - panel[1] <= size
            or not -size * .1 <= end.bbox[1] - start.bbox[3] <= size * .25
            or not 0 <= detail.bbox[1] - end.bbox[3] <= size * .45
            or abs(detail.text_left - start.bbox[0]) > size * .25
            or min(abs(end.text_left - x) for x in (detail.text_left, start.text_left)) > size * .25):
        return
    # The glyph and both physical lines remain available in source order.
    second.text = first.text + " " + second.text
    second.lines = [start, end]
    second.sources = (SourceRegion(first.sources[0].page, union_bbox([start.bbox, end.bbox])),)
    children.pop(0)


def card_panels(page: PageData, shapes) -> list[BBox]:
    """Require repeated white panels with styled titles and real gutters.

    A normal table's regular cells are not cards. A lone framed sidebar is not
    a grid. This conservative detector recovers text containers only; it does
    not assign meaning to connector lines, symbols, or prerequisite relations.
    """
    candidates = []
    for shape in shapes:
        color = shape.get('non_stroking_color')
        if not (shape.get('fill') and isinstance(color,(tuple,list))
                and len(color)==3 and min(color)>=.98):
            continue
        box = (shape['x0'],shape['top'],shape['x1'],shape['bottom'])
        width,height = box[2]-box[0],box[3]-box[1]
        if not (70 <= width <= page.width*.4 and page.body_size*3 <= height <= page.height*.5):
            continue
        inside = [w for w in page.words if w.upright and contains(box,w)]
        if (sum(len(w.text) for w in inside) >= 20 and any(
                any(tag in w.font.lower() for tag in ('bold','demi','black')) for w in inside)
                and box not in candidates):
            candidates.append(box)
    groups = []
    for box in sorted(candidates):
        for group in groups:
            if abs((box[2]-box[0])-(group[0][2]-group[0][0])) <= 2:
                group.append(box)
                break
        else:
            groups.append([box])
    panels = []
    for group in groups:
        columns = {round(box[0]/3) for box in group}
        rows = {round(box[1]/3) for box in group}
        gap = max(1,page.body_size*.2)
        separated = all(min(a[2],b[2])-max(a[0],b[0]) <= -gap
                        or min(a[3],b[3])-max(a[1],b[1]) <= -gap
                        for i,a in enumerate(group) for b in group[i+1:])
        if len(group) >= 4 and len(columns) >= 2 and len(rows) >= 2 and separated:
            panels.extend(group)
    if panels:
        left,right = min(b[0] for b in panels),max(b[2] for b in panels)
        top = min(b[1] for b in panels)
        panels.extend(box for box in candidates if box not in panels
                      and left-page.body_size*2 <= box[0] and box[2] <= right+page.body_size*2
                      and 0 <= top-box[3] <= page.body_size*12)
    return panels
