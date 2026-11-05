"""Conservative source-backed details within independently detected forms.

The routines here do not discover forms or infer values from surrounding prose.
They accept only repeated geometry and retain the contributing words/marks.
"""

from collections import Counter
from dataclasses import replace
import hashlib
import math
import re
from statistics import median

from .circular_labels import circle_outline
from .geometry import contains, group_lines
from .models import Block, Glyph, Line, SourceRegion, Word, union_bbox
from .symbol_geometry import _box
from .symbols import LABELS
from .text import normalize


# Exact, scale/translation-normalized paths independently identified from the
# two source vector marks. They are not generic star or arrow classifiers.
FORM_ICON_PATHS = {
    "a5c44d69a14b1094f228d9e44d77dea28f72c506d6557d38c771a1ccb884c343": "special_activation",
    "cd9d3d96bb71b42d8b05e845e2895c711982d33c65a6d797006872542a3a4e90": "reaction",
}


def _path_digest(path):
    if not path or not all(op[0] in ("m", "l", "c", "v", "y", "h") for op in path):
        return ""
    x, y, right, _ = _box(path)
    scale = right-x
    if scale <= 0:
        return ""
    signature = tuple((op[0], *(tuple(round((value-origin)/scale, 5)
                                    for value, origin in zip(point, (x, y)))
                               for point in op[1:])) for op in path)
    return hashlib.sha256(repr(signature).encode("ascii")).hexdigest()


def _small_caps(word):
    """Only explicit small-cap encoding, never a font family or inferred case."""
    letters = [g for g in word.glyphs if g.text.isalpha()]
    if (not letters or Counter(c.casefold() for c in word.text if c.isalpha()) !=
            Counter(g.text.casefold() for g in letters) or
            not all(len(g.text) == 1 and g.text.isascii() and
            (re.fullmatch(r"[a-z]\.sc", g.glyph_name) or
             re.fullmatch(r"[A-Z]", g.glyph_name)) and
            g.text.casefold() == g.glyph_name[0].casefold() for g in letters)
            or not any(g.glyph_name.endswith(".sc") for g in letters)):
        return word
    return replace(word, text=word.text.upper())


def _source_order_word(word):
    glyphs = word.glyphs
    if (len(glyphs) < 2 or any(g.source_order is None or g.text_run is None
                              or len(g.text) != 1 or not g.text.isalpha() for g in glyphs)
            or len({g.text_run for g in glyphs}) != 1):
        return word
    ordered = sorted(glyphs, key=lambda g: g.source_order)
    indices = [g.source_order for g in ordered]
    if indices != list(range(indices[0], indices[0]+len(indices))):
        return word
    inversions = [(a, b) for i, a in enumerate(ordered) for b in ordered[i+1:]
                  if a.bbox[0] > b.bbox[0]]
    if len(inversions) != 1:
        return word
    a, b = inversions[0]
    overlap = min(a.bbox[2], b.bbox[2])-max(a.bbox[0], b.bbox[0])
    if (a.bbox[0]-b.bbox[0] > word.size*.2 or b.bbox[2] < a.bbox[2]
            or overlap < min(a.bbox[2]-a.bbox[0], b.bbox[2]-b.bbox[0])*.5
            or abs(a.bbox[1]-b.bbox[1]) > word.size*.05
            or abs(a.bbox[3]-b.bbox[3]) > word.size*.05):
        return word
    text = normalize("".join(g.text for g in ordered))
    return replace(word, text=text, glyphs=tuple(ordered)) if Counter(text) == Counter(word.text) else word


def _closing_parenthesis(left, right):
    if not (left.text.startswith("(") and right.text == ")" and left.glyphs and right.glyphs
            and left.font == right.font and abs(left.size-right.size) <= .05
            and 0 <= right.bbox[0]-left.bbox[2] <= left.size*.4):
        return None
    a, b = left.glyphs[-1], right.glyphs[0]
    if (a.source_order is None or b.source_order != a.source_order+1 or a.text_run is None
            or a.text_run != b.text_run or abs(a.bbox[1]-b.bbox[1]) > left.size*.05):
        return None
    # Handwritten overlapping letters corroborate that a normal word-gap
    # threshold is unsuitable here. An ordinary parenthesis stays untouched.
    if not any(min(x.bbox[2], y.bbox[2])-max(x.bbox[0], y.bbox[0]) > left.size*.1
               for x, y in zip(left.glyphs, left.glyphs[1:])):
        return None
    return replace(left, text=left.text+right.text, bbox=union_bbox([left.bbox,right.bbox]),
                   glyphs=left.glyphs+right.glyphs)


def _circle_marks(shapes, forms):
    outlines = [(circle_outline(shape), shape) for shape in shapes]
    outlines = [(box, shape) for box, shape in outlines if box and
                any(contains(form.bbox, Word("", box, 0)) for form in forms)]
    marks = []
    for box, outline in outlines:
        if any(all(abs(a-b) < .1 for a,b in zip(box, existing)) for existing,_ in marks):
            continue
        nearby = [shape for shape in shapes if shape.get("fill") and
                  shape.get("width", 0) <= (box[2]-box[0])*1.2 and
                  shape.get("height", 0) <= (box[3]-box[1])*1.2 and
                  min(box[2],shape["x1"])-max(box[0],shape["x0"]) > .1 and
                  min(box[3],shape["bottom"])-max(box[1],shape["top"]) > .1]
        filled = []
        for shape in nearby:
            candidate = circle_outline({**shape, "stroke": True})
            if (candidate and all(abs(a-b) < .1 for a,b in zip(box,candidate)) and
                    shape.get("non_stroking_color") == outline.get("stroking_color")):
                filled.append(shape)
        state = bool(filled) if len(filled) == len(nearby) and len(filled) <= 1 else None
        marks.append((box, state))
    return marks


def prepare_form_details(page, shapes, rules):
    """Repair only form-owned text; collect source marks without adding text."""
    if not page.form_regions:
        return
    words = []
    for original in page.words:
        if any(contains(form.bbox, original) for form in page.form_regions):
            original = _source_order_word(_small_caps(original))
        words.append(original)
    # Only immediately adjacent source glyphs from one form and one run join.
    merged = []
    for word in words:
        if merged and any(contains(form.bbox, word) and contains(form.bbox, merged[-1])
                          for form in page.form_regions):
            combined = _closing_parenthesis(merged[-1], word)
            if combined:
                merged[-1] = combined
                continue
        merged.append(word)
    page.words = merged
    for shape in shapes:
        if (not shape.get("fill") or shape.get("stroke")
                or len(shape.get("path", ())) not in (47, 89)):
            continue
        symbol = FORM_ICON_PATHS.get(_path_digest(shape.get("path", ())))
        if not symbol:
            continue
        box = tuple(shape[k] for k in ("x0", "top", "x1", "bottom"))
        owners = [field for form in page.form_regions for field in form.fields
                  if contains(field, Word("",box,0))]
        if len(owners) != 1:
            continue
        candidates = [w for w in page.words if contains(owners[0],w)
                      and 0 <= w.bbox[0]-box[2] <= w.size*1.25
                      and w.bbox[1] <= (box[1]+box[3])/2 <= w.bbox[3]
                      and not any(g.symbol for g in w.glyphs)]
        if not candidates:
            continue
        candidates.sort(key=lambda w:w.bbox[0])
        if len(candidates)>1 and candidates[1].bbox[0]-candidates[0].bbox[0] <= page.body_size:
            continue
        anchor = candidates[0]
        glyph = Glyph("", "", box, symbol=symbol, origin="vector")
        index = page.words.index(anchor)
        # The mark's own bbox remains in Glyph provenance. Do not move the
        # text anchor: that would falsely introduce a hanging paragraph.
        page.words[index] = replace(anchor, text=LABELS[symbol]+" "+anchor.text,
                                    glyphs=(glyph,*anchor.glyphs))
    page.form_circles = _circle_marks(shapes, page.form_regions)
    page.form_dividers = [tuple(rule[k] for k in ("x0","top","x1","bottom")) for rule in rules
                          if rule.get("stroke") and rule["height"] > page.body_size
                          and abs(rule["x1"]-rule["x0"]) < .1]
    for shape in (*shapes,*rules):
        colors = ([shape.get("non_stroking_color")] if shape.get("fill") else [])
        colors += ([shape.get("stroking_color")] if shape.get("stroke") else [])
        def dark(color):
            channels = (color,) if isinstance(color,(float,int)) else color
            return (not isinstance(channels,(tuple,list)) or
                    any(not isinstance(c,(int,float)) or c < .5 for c in channels))
        if colors and any(dark(color) for color in colors):
            box = tuple(shape[k] for k in ("x0","top","x1","bottom"))
            # A stroked panel outline (including its top/bottom rules) is
            # structure, not ink in an otherwise blank writable cell.
            def border(field):
                width, height = field[2]-field[0], field[3]-field[1]
                w, h = box[2]-box[0], box[3]-box[1]
                tolerance = page.body_size*.3
                if not all(field[i]-tolerance <= box[i] <= field[i]+tolerance
                           for i in (0,1,2,3)):
                    # Standalone horizontal/vertical rules need only match
                    # the corresponding field edges and full span.
                    horizontal = (w >= width*.9 and h <= .1 and
                                  abs(box[0]-field[0]) <= tolerance and
                                  abs(box[2]-field[2]) <= tolerance and
                                  min(abs(box[1]-field[1]),abs(box[1]-field[3])) <= tolerance)
                    vertical = (h >= height*.8 and w <= .1 and
                                abs(box[1]-field[1]) <= tolerance and
                                abs(box[3]-field[3]) <= tolerance and
                                min(abs(box[0]-field[0]),abs(box[0]-field[2])) <= tolerance)
                    return horizontal or vertical
                if w < width*.9 or h < height*.8:
                    return False
                if shape.get("object_type") == "rect":
                    return True
                points = shape.get("pts", ())
                if len(points) < 5 or math.dist(points[0],points[-1]) > page.body_size*.2:
                    return False
                edge_tolerance = page.body_size*.2
                def edges(point):
                    return {i for i, distance in enumerate((abs(point[0]-box[0]),
                            abs(point[0]-box[2]),abs(point[1]-box[1]),abs(point[1]-box[3])))
                            if distance <= edge_tolerance}
                return all(edges(a) and edges(b) and
                           (edges(a) & edges(b) or math.dist(a,b) <= page.body_size)
                           for a,b in zip(points,points[1:]))
            if (shape.get("stroke") and not shape.get("fill") and
                    any(border(field) for form in page.form_regions for field in form.fields)):
                continue
            if any(contains(form.bbox,Word("",box,0)) for form in page.form_regions):
                page.form_ink.append(box)


def form_table_rows(table, words):
    """Rebuild simple form cells only when each cell preserves source content."""
    if (table.structure or table.repaired_gaps or len(table.cell_boxes) != len(table.rows)
            or any(len(boxes) != len(row) or any(box is None for box in boxes)
                   for boxes,row in zip(table.cell_boxes,table.rows))):
        return table
    rows = []
    for boxes, old in zip(table.cell_boxes,table.rows):
        result = []
        for box, expected in zip(boxes,old):
            inside = [w for w in words if contains(box,w)]
            lines = group_lines(inside, median((w.size for w in inside)) if inside else 10)
            text = "\n".join(line.text for line in lines)
            if "".join(text.split()).casefold() != "".join((expected or "").split()).casefold():
                return table
            result.append(text)
        rows.append(result)
    return replace(table, rows=rows)


def _line(words):
    return Line(words, union_bbox([w.bbox for w in words]), median(w.size for w in words))


def _block(page, text, words, boxes=()):
    box = union_bbox([*(w.bbox for w in words), *boxes])
    return Block("paragraph", text, (SourceRegion(page.number, box),),
                 lines=[_line(words)], body_size=page.body_size)


def _field_parts(field, words):
    owned = [w for w in words if contains(field,w)]
    values = [w for w in owned if w.text.isdecimal()]
    labels = [w for w in owned if w not in values and any(c.isalpha() for c in w.text)]
    if len(values) != 1 or not labels or len(values)+len(labels) != len(owned):
        return None
    return sorted(labels, key=lambda w:(w.bbox[1],w.bbox[0])), values[0]


def _defense_groups(page, form, words):
    size = page.body_size
    for first in form.fields:
        width, height = first[2]-first[0], first[3]-first[1]
        fields = sorted([box for box in form.fields
                         if abs(box[1]-first[1]) <= size*.1
                         and abs((box[2]-box[0])-width) <= size*.15
                         and abs((box[3]-box[1])-height) <= size*.15], key=lambda b:b[0])
        if len(fields) != 6 or width < size*3 or height < size*3:
            continue
        groups = []
        for left,right in zip(fields[::2],fields[1::2]):
            if right[0] <= left[2] or right[0]-left[2] > width*1.1:
                break
            parts = [_field_parts(field,words) for field in (left,right)]
            if any(part is None for part in parts):
                break
            center = [w for w in words if left[2] <= (w.bbox[0]+w.bbox[2])/2 <= right[0]
                      and left[1]-size*2 <= (w.bbox[1]+w.bbox[3])/2 <= left[3]]
            labels = sorted([w for w in center if w.text.isalpha()], key=lambda w:w.bbox[1])
            values = [w for w in center if w.text.isdecimal()]
            if len(labels) != 2 or len(values) != 1 or len(center) != 3:
                break
            if any(abs((w.bbox[0]+w.bbox[2])/2-(left[2]+right[0])/2) > size*1.5
                   for w in center):
                break
            groups.append((left,right,labels,values[0],parts))
        if len(groups) != 3 or len({group[2][1].text.casefold() for group in groups}) != 1:
            continue
        output = []
        for left,right,labels,value,parts in groups:
            ordered = [*labels,value,*parts[0][0],parts[0][1],*parts[1][0],parts[1][1]]
            text = " ".join(w.text for w in (*labels,value))
            text += ", " + " ".join(w.text for w in (*parts[0][0],parts[0][1]))
            text += ", " + " ".join(w.text for w in (*parts[1][0],parts[1][1]))
            block = _block(page,text,ordered,(left,right))
            output.append((block.sources[0].bbox,[block],{id(w) for w in ordered},{left,right}))
        return output
    return []


def _resource_groups(page, form, words):
    candidates = []
    for box in form.fields:
        dividers = [rule for rule in page.form_dividers
                    if box[0]+page.body_size < rule[0] < box[2]-page.body_size
                    and rule[1] <= box[1]+page.body_size*.3
                    and rule[3] >= box[3]-page.body_size*.3]
        if len(dividers) != 1:
            continue
        x = dividers[0][0]
        owned = [w for w in words if contains(box,w)]
        if any(w.bbox[0] < x < w.bbox[2] for w in owned):
            continue
        left = [w for w in owned if w.bbox[2] <= x]
        right = [w for w in owned if w.bbox[0] >= x]
        ll = sorted([w for w in left if w.text.isalpha()], key=lambda w:w.bbox[1])
        rl = [w for w in right if w.text.isalpha()]
        lv,rv = ([w for w in side if w.text.isdecimal()] for side in (left,right))
        if (len(ll) != 2 or len(rl) != 1 or len(lv)>1 or len(rv)>1
                or len(left) != len(ll)+len(lv) or len(right) != len(rl)+len(rv)
                or ll[1].bbox[1] <= ll[0].bbox[1]):
            continue
        # An empty text cell with independent vector ink is not proven blank.
        def marked(left_edge,right_edge,top):
            return any(ink[2]-ink[0] <= (box[2]-box[0])*1.5
                       and ink[3]-ink[1] <= (box[3]-box[1])*1.5
                       and min(right_edge-.5,ink[2]+.2)-max(left_edge+.5,ink[0]-.2) > 0
                       and min(box[3]-.5,ink[3]+.2)-max(top,ink[1]-.2) > 0
                       for ink in page.form_ink)
        if ((not lv and marked(box[0],x,ll[1].bbox[3]))
                or (not rv and marked(x,box[2],rl[0].bbox[3]))):
            continue
        candidates.append((box,ll,rl,lv,rv,owned))
    for candidate in candidates:
        box,ll,rl,_,_,_ = candidate
        peers = [other for other in candidates if abs(other[0][1]-box[1]) <= page.body_size*.2
                 and abs((other[0][2]-other[0][0])-(box[2]-box[0])) <= page.body_size*.2
                 and other[1][1].text.casefold() == ll[1].text.casefold()
                 and other[2][0].text.casefold() == rl[0].text.casefold()]
        if len(peers) < 2:
            continue
        output = []
        for box,ll,rl,lv,rv,owned in sorted(peers,key=lambda c:c[0][0]):
            text = f"{ll[0].text} {ll[1].text} {lv[0].text if lv else '[blank]'}, "
            text += f"{rl[0].text} {rv[0].text if rv else '[blank]'}"
            block = _block(page,text,owned,(box,))
            output.append((box,[block],{id(w) for w in owned},{box}))
        return output
    return []


def _rank_groups(page, form, words):
    """Require a complete repeated box/label/circle matrix before counting."""
    size = page.body_size
    candidates = []
    for field in form.fields:
        if not (size <= field[2]-field[0] <= size*4 and size <= field[3]-field[1] <= size*3):
            continue
        next_x = min((other[0] for other in form.fields if other[0] > field[2]
                      and min(other[3],field[3]) > max(other[1],field[1])), default=form.bbox[2])
        circles = sorted([(box,state) for box,state in page.form_circles
                          if field[2] < box[0] and box[2] < next_x
                          and field[1] <= (box[1]+box[3])/2 <= field[3]], key=lambda c:c[0][0])
        if len(circles) < 4 or any(state is None for _,state in circles):
            continue
        spacing = [b[0][0]-a[0][0] for a,b in zip(circles,circles[1:])]
        if max(spacing)-min(spacing) > size*.1:
            continue
        labels = sorted([w for w in words if field[2] <= w.bbox[0] < circles[0][0][0]
                         and field[1] <= (w.bbox[1]+w.bbox[3])/2 <= field[3]], key=lambda w:w.bbox[0])
        value = [w for w in words if contains(field,w)]
        if not labels or len(value)>1 or any(not w.text.isdecimal() for w in value):
            continue
        named = any(c.isalpha() for w in labels for c in w.text)
        if named != bool(value):
            continue
        candidates.append((field,labels,value,circles,named))
    columns = []
    for candidate in sorted(candidates,key=lambda c:(c[0][0],c[0][1])):
        column = next((c for c in columns if abs(c[0][0][0]-candidate[0][0]) <= size*.2),None)
        if column is None:
            columns.append([candidate])
        else:
            column.append(candidate)
    columns = [sorted(column,key=lambda c:c[0][1]) for column in columns if len(column)>=3]
    if len(columns)<2 or len({len(column) for column in columns})!=1:
        return []
    count = len(columns[0][0][3])
    if any(len(row[3]) != count or abs(row[0][1]-columns[0][i][0][1]) > size*.2
           for column in columns for i,row in enumerate(column)):
        return []
    for column in columns:
        first = column[0][3]
        if any(any(abs(box[0]-reference[0]) > size*.15
                       or abs((box[2]-box[0])-(reference[2]-reference[0])) > size*.1
                       or abs((box[3]-box[1])-(reference[3]-reference[1])) > size*.1
                       for (box,_),(reference,_) in zip(row[3],first)) for row in column):
            return []
    all_words = [w for column in columns for _,labels,values,_,_ in column for w in (*values,*labels)]
    if len({id(w) for w in all_words}) != len(all_words):
        return []
    output = []
    for column in columns:
        children, boxes, claimed, fields = [], [], set(), set()
        for field,labels,values,circles,named in column:
            row_words = [*values,*labels]
            text = " ".join(w.text for w in row_words)
            glyphs = tuple(Glyph("", "", box, symbol="rank_filled" if state else "rank_empty",
                                 origin="vector") for box,state in circles)
            marker = Word("",union_bbox([box for box,_ in circles]),size,glyphs=glyphs)
            if named:
                text += f" [ranks: {sum(state for _,state in circles)}]"
            block = _block(page,text,[*row_words,marker],(field,))
            children.append(block)
            boxes.append(block.sources[0].bbox)
            claimed.update(id(w) for w in row_words)
            fields.add(field)
        output.append((union_bbox(boxes),children,claimed,fields))
    return output


def form_detail_groups(page, form, words):
    """Return non-overlapping evidenced groups and their exact word owners."""
    result = []
    claimed_words, claimed_fields = set(), set()
    for detector in (_defense_groups, _resource_groups, _rank_groups):
        found = detector(page,form,[w for w in words if id(w) not in claimed_words])
        for group in found:
            if group[2] & claimed_words or group[3] & claimed_fields:
                continue
            result.append(group)
            claimed_words.update(group[2])
            claimed_fields.update(group[3])
    return result


def include_form_icon_sources(blocks, page_number):
    for block in blocks:
        boxes = [g.bbox for line in block.lines for word in line.words for g in word.glyphs
                 if g.origin == "vector" and g.symbol in ("special_activation", "reaction")]
        if boxes and block.sources:
            first = block.sources[0]
            block.sources = (SourceRegion(page_number,union_bbox([first.bbox,*boxes])),*block.sources[1:])
        include_form_icon_sources(block.children,page_number)
