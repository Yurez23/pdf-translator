"""Strict vector-constellation matching, independent of PDF extraction/export."""

from .models import union_bbox
from .symbol_signatures import PATH_SIGNATURES

TOLERANCE = .002  # Relative to full icon extent; accommodates PDF decimal rounding.


def _points(path):
    return [p for command in path for p in command[1:]]


def _box(path):
    points = _points(path)
    return min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)


def _matches(path, template, x, y, scale):
    if len(path) != len(template):
        return False
    for a, b in zip(path, template):
        if a[0] != b[0] or len(a) != len(b):
            return False
        if any(abs((p[0]-x)/scale-q[0]) > TOLERANCE
               or abs((p[1]-y)/scale-q[1]) > TOLERANCE for p, q in zip(a[1:], b[1:])):
            return False
    return True


def vector_symbols(shapes):
    """Yield complete exact signatures, largest constellations first.

    Partial known multipart outlines are positively detected but unresolved;
    never decode the outer free-action triangle as a one-action icon.
    Nearby extra paths inside a candidate also make it unresolved. No generic
    shape, raster, arrow or star classifier is used.
    """
    candidates = [s for s in shapes if s.get("fill") and not s.get("stroke")
                  and s.get("path") and 0 < s["width"] <= 80 and 0 < s["height"] <= 80]
    used = set()
    templates = sorted(PATH_SIGNATURES.items(), key=lambda p: -len(p[1]))
    for name, parts in templates:
        tb = _box(parts[0])
        for index, shape in enumerate(candidates):
            if index in used:
                continue
            # Control-point extents (not curve bbox) preserve Bezier geometry.
            sb = _box(shape["path"])
            scale = (sb[2]-sb[0])/(tb[2]-tb[0])
            x, y = sb[0]-tb[0]*scale, sb[1]-tb[1]*scale
            if not _matches(shape["path"], parts[0], x, y, scale):
                continue
            members = [index]
            for part in parts[1:]:
                matches = [i for i, s in enumerate(candidates) if i not in used and i not in members
                           and _matches(s["path"], part, x, y, scale)
                           and s.get("non_stroking_color") == shape.get("non_stroking_color")]
                if len(matches) != 1:
                    break
                members.append(matches[0])
            fullbox = union_bbox([_box(p) for p in parts])
            box = (x+fullbox[0]*scale, y+fullbox[1]*scale,
                   x+fullbox[2]*scale, y+fullbox[3]*scale)
            inside = [i for i, s in enumerate(candidates) if i not in used
                      and box[0]-.02 <= s["x0"] and s["x1"] <= box[2]+.02
                      and box[1]-.02 <= s["top"] and s["bottom"] <= box[3]+.02]
            if name == "free_action" and len(members) == 1 and len(inside) == 1:
                continue  # A plain triangle is the supported one-action sign.
            if name.startswith("complication") and len(members) < 6:
                continue  # One isolated ray is not evidence of a game symbol.
            # A multipart first path is distinctive enough for an unknown
            # diagnostic; a lone triangle/star outside inline context is not.
            complete = len(members) == len(parts) and set(inside) == set(members)
            used.update(inside or members)
            yield name.split(":")[0] if complete else "unknown", box
