"""Exclude clearly isolated figure labels before paragraphs are assembled.

Image bounds alone are not a text mask: transparent artwork can overlap prose,
and a page-sized background is also an image in many illustrated books.
"""

from collections import Counter

from .layout import PageData, contains, group_lines
from .models import BBox, Line, Word


def _overlap(a: BBox, b: BBox) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def _font(name: str) -> str:
    return name.split("+")[-1].casefold()


def _style(words: list[Word]) -> tuple[str, tuple[float, ...] | None]:
    weights = Counter()
    for word in words:
        weights[(_font(word.font), word.color)] += len(word.text)
    return weights.most_common(1)[0][0]


def _inside_protected(line: Line, page: PageData) -> bool:
    protected = [table.bbox for table in page.tables] + page.frames
    if any(any(contains(box, word) for word in line.words) for box in protected):
        return True
    # A title immediately above a table is not part of a neighbouring picture.
    for table in page.tables:
        if (0 <= table.bbox[1] - line.bbox[3] <= page.body_size * 2.5
                and _overlap(line.bbox, table.bbox) >= (line.bbox[2]-line.bbox[0]) * 0.8):
            return True
    return False


def _related_images(line: Line, images: list[BBox], size: float) -> set[int]:
    matches = set()
    width = line.bbox[2]-line.bbox[0]
    center = (line.bbox[0]+line.bbox[2]) / 2
    for index, image in enumerate(images):
        image_width = image[2]-image[0]
        aligned = (_overlap(line.bbox, image) >= width * 0.9
                   and abs(center-(image[0]+image[2])/2) <= image_width * 0.4)
        above = -size * 2 <= image[1]-line.bbox[3] <= size * 4
        below = -size * 2 <= line.bbox[1]-image[3] <= size * 4
        if aligned and (above or below):
            matches.add(index)
    return matches


def _belongs_to_prose(line: Line, prose: list[Line], size: float) -> bool:
    matches = 0
    for other in prose:
        gap = max(other.bbox[1]-line.bbox[3], line.bbox[1]-other.bbox[3], 0)
        if gap <= size * 8 and _overlap(line.bbox, other.bbox) >= (line.bbox[2]-line.bbox[0]) * 0.6:
            matches += 1
            followed_by_prose = (0 <= other.bbox[1]-line.bbox[3] <= size * 3
                                 and abs(line.bbox[0]-other.bbox[0]) <= size * 2)
            if matches >= 2 or followed_by_prose:
                return True
    return False


def remove_figure_captions(page: PageData) -> None:
    """Remove short, distinctively styled figure labels outside prose flow.

    Uncertain fragments are retained. Tables, table titles and framed sidebars
    are protected even if a decorative image lies behind them.
    """
    area = page.width * page.height
    images = [box for box in page.images
              if box[2]-box[0] >= 60 and box[3]-box[1] >= 60
              and area * 0.01 <= (box[2]-box[0]) * (box[3]-box[1]) < area * 0.85
              and not (box[2]-box[0] >= page.width * 0.9 and box[3]-box[1] >= page.height * 0.9)]
    if not images:
        return
    lines = group_lines([w for w in page.words if w.upright], page.body_size, split_gaps=True)
    prose = [line for line in lines if abs(line.size-page.body_size) <= page.body_size * 0.08
             and not _inside_protected(line, page)]
    if not prose:
        return
    body_style = _style([w for line in prose for w in line.words])
    # A caption can use the body font size but a different colour. It must not
    # protect its own neighbouring lines by masquerading as a prose column.
    prose = [line for line in prose if _style(line.words) == body_style]
    candidates: list[tuple[Line, set[int]]] = []
    for line in lines:
        if not (1 <= len(line.words) <= 14 and len(line.text) <= 120 and line.size <= page.body_size * 1.8):
            continue
        if _inside_protected(line, page) or _style(line.words) == body_style:
            continue
        # Distinct styling and proximity are insufficient if the line belongs
        # to a text column (e.g. a section heading over a decorated background).
        if _belongs_to_prose(line, prose, page.body_size):
            continue
        related = _related_images(line, images, page.body_size)
        if related:
            candidates.append((line, related))
    groups: list[list[tuple[Line, set[int]]]] = []
    for line, related in candidates:
        for group in groups:
            previous, previous_images = group[-1]
            center_shift = abs((line.bbox[0]+line.bbox[2]-previous.bbox[0]-previous.bbox[2])/2)
            if (related & previous_images and _style(line.words) == _style(previous.words)
                    and abs(line.size-previous.size) <= 0.5
                    and -page.body_size * 0.3 <= line.bbox[1]-previous.bbox[3] <= page.body_size
                    and (center_shift <= page.body_size * 2 or abs(line.bbox[0]-previous.bbox[0]) <= page.body_size)):
                group.append((line, related))
                break
        else:
            groups.append([(line, related)])
    ignored = set()
    for group in groups:
        if len(group) <= 3 and sum(len(line.text) for line, _ in group) <= 160:
            ignored.update(id(w) for line, _ in group for w in line.words)
    page.words = [w for w in page.words if id(w) not in ignored]
