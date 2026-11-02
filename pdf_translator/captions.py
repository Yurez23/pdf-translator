"""Exclude clearly isolated figure labels before paragraphs are assembled.

Image bounds alone are not a text mask: transparent artwork can overlap prose,
and a page-sized background is also an image in many illustrated books.
"""

from collections import Counter
import re
from statistics import median

from .geometry import contains, group_lines
from .page_data import PageData
from .models import BBox, Line, Word, union_bbox
from .paragraphs import bold, italic
from .text import is_list, terminal


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
    protected = ([table.bbox for table in page.tables] + page.frames
                 + [box for form in page.form_regions for box in (form.bbox, *form.attachments)]
                 + [hint.bbox for hint in page.heading_hints if hint.protect])
    if any(any(contains(box, word) for word in line.words) for box in protected):
        return True
    if any(0 <= box[1]-line.bbox[3] <= page.body_size*2.5
           and _overlap(line.bbox,box) >= (line.bbox[2]-line.bbox[0])*.8
           for box in page.diagram_boxes):
        return True
    # A title immediately above a table is not part of a neighbouring picture.
    for table in page.tables:
        if (0 <= table.bbox[1] - line.bbox[3] <= page.body_size * 2.5
                and _overlap(line.bbox, table.bbox) >= (line.bbox[2]-line.bbox[0]) * 0.8):
            return True
        # Notes below a table are content too, not labels for nearby artwork.
        if (0 <= line.bbox[1]-table.bbox[3] <= page.body_size * 3
                and _overlap(line.bbox,table.bbox) >= (line.bbox[2]-line.bbox[0])*.8
                and (line.text.startswith(('*','†','‡')) or line.size < page.body_size*.95
                     and any('italic' in w.font.lower() or 'oblique' in w.font.lower() for w in line.words))):
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
        inside = (image[0] <= line.bbox[0] and line.bbox[2] <= image[2]
                  and image[1] <= line.bbox[1] and line.bbox[3] <= image[3])
        if aligned and (above or below or inside):
            matches.add(index)
    return matches


def _belongs_to_prose(line: Line, prose: list[Line], size: float) -> bool:
    matches = 0
    for other in prose:
        gap = max(other.bbox[1]-line.bbox[3], line.bbox[1]-other.bbox[3], 0)
        aligned = abs(line.bbox[0]-other.bbox[0]) <= size * (0.5 if line.size > size * 1.8 else 2)
        if (gap <= size * 2.5 and aligned
                and _overlap(line.bbox, other.bbox) >= (line.bbox[2]-line.bbox[0]) * 0.6):
            matches += 1
            followed_by_prose = (0 <= other.bbox[1]-line.bbox[3] <= size * 3
                                 and aligned)
            if matches >= 2 or followed_by_prose:
                return True
    return False


def _continues_inline_prose(line: Line, lines: list[Line], prose: list[Line],
                            page: PageData, body_style: tuple | None) -> bool:
    """Keep an emphasis wrap after a mixed line already anchored in prose.

    Do not add inferred tails to the prose/style seeds: an isolated label must
    not bootstrap protection for its neighbours or elsewhere in the document.
    """
    if body_style is None or body_style[1] is None or is_list(line.text):
        return False
    size = page.body_size
    styles = {_style([word]) for word in line.words}
    for previous in lines:
        if (not 0 <= line.bbox[1]-previous.bbox[3] <= size*.8
                or abs(line.bbox[0]-previous.bbox[0]) > size*.35
                or terminal(previous.text) or is_list(previous.text)
                or _inside_protected(previous, page)):
            continue
        words = previous.words + line.words
        if any(abs(word.size-size) > size*.08 or word.color != body_style[1]
               for word in words):
            continue
        previous_styles = {_style([word]) for word in previous.words}
        boundary = _style([previous.words[-1]])
        if (body_style in previous_styles and boundary != body_style
                and boundary == _style([line.words[0]])
                and styles <= previous_styles
                and _belongs_to_prose(previous, prose, size)):
            return True
    return False


def _styled_runs(lines: list[Line], body: float) -> list[list[Line]]:
    """Group before eligibility checks, so an uncertain tail vetoes the run."""
    groups: list[list[Line]] = []
    for line in lines:
        for group in groups:
            previous = group[-1]
            centers = abs((line.bbox[0]+line.bbox[2]-previous.bbox[0]-previous.bbox[2])/2)
            if (_style(line.words) == _style(previous.words)
                    and abs(line.size-previous.size) <= .5
                    and -max(body*.3, line.size*.5) <= line.bbox[1]-previous.bbox[3] <= body
                    and (centers <= body*2 or abs(line.bbox[0]-previous.bbox[0]) <= body)):
                group.append(line)
                break
        else:
            groups.append([line])
    return groups


def _body_prose(page: PageData, lines: list[Line]) -> list[Line]:
    """Require a paragraph run, rather than trusting the page's modal size alone."""
    ordinary = [line for line in lines
                if page.body_size*.75 <= line.size <= page.body_size*1.15
                and not _inside_protected(line, page)]
    result = {}
    for first in ordinary:
        run = [first]
        for following in ordinary:
            previous = run[-1]
            if (0 <= following.bbox[1]-previous.bbox[3] <= page.body_size
                    and abs(following.bbox[0]-first.bbox[0]) <= page.body_size*2
                    and abs(following.size-first.size) <= .5
                    and _style(following.words) == _style(first.words)):
                run.append(following)
        if len(run) >= 3 and sum(line.source_length for line in run) >= 100:
            result.update((id(line), line) for line in run)
    return list(result.values())


def remove_background_captions(pages: list[PageData]) -> None:
    """Conservative document pass for compact labels on composited artwork.

    A background only enables this branch; it does not locate a figure. A new
    style therefore needs stronger paragraph, typography and group evidence
    than an ordinary image label. Freeze content vetoes and strong seeds before
    reuse, so weak matches cannot bootstrap a cascade of further deletions.
    """
    lines = {p.number: group_lines([w for w in p.words if w.upright], p.body_size,
                                  split_gaps=True) for p in pages}
    prose = {p.number: _body_prose(p, lines[p.number]) for p in pages}
    sizes = [line.size for p in pages for line in prose[p.number]]
    if not sizes:
        return
    document_body = median(sizes)
    content_styles = set()
    for page in pages:
        body = median(line.size for line in prose[page.number]) if prose[page.number] else document_body
        context_lines = [line for line in lines[page.number]
                         if body*.75 <= line.size <= body*1.15]
        prose_ids = {id(line) for line in prose[page.number]}
        for line in lines[page.number]:
            if (_inside_protected(line, page) or italic(line) or is_list(line.text)
                    or id(line) in prose_ids
                    or _belongs_to_prose(line, context_lines, body)):
                content_styles.add(_style(line.words))

    candidates = []
    seeds = set().union(*(p.caption_styles for p in pages)) - content_styles
    for page in pages:
        backgrounds = [box for box in page.images
                       if box[2]-box[0] >= page.width*.9 and box[3]-box[1] >= page.height*.9]
        if not backgrounds:
            continue
        local_prose = prose[page.number]
        body = median(line.size for line in local_prose) if local_prose else document_body
        body_style = _style([w for line in local_prose for w in line.words]) if local_prose else None
        for group in _styled_runs(lines[page.number], body):
            style = _style(group[0].words)
            box = union_bbox([line.bbox for line in group])
            centers = [(line.bbox[0]+line.bbox[2])/2 for line in group]
            if not (2 <= len(group) <= 4 and sum(len(line.text) for line in group) <= 160
                    and re.search(r'[.!?…]$', group[-1].text)
                    and all(1 <= len(line.words) <= 14 and len(line.text) <= 120
                            and body*1.8 < line.size <= body*3 for line in group)
                    and max(centers)-min(centers) <= body
                    and box[2]-box[0] <= page.width*.6
                    and any(b[0] <= box[0] and b[1] <= box[1]
                            and box[2] <= b[2] and box[3] <= b[3] for b in backgrounds)):
                continue
            if (style in content_styles or any(_inside_protected(line, page)
                    or italic(line) or all(bold(w) for w in line.words) or is_list(line.text)
                    or line.text.startswith(('“', '‘', '"', '«', '—'))
                    or any(g.symbol for w in line.words for g in w.glyphs) for line in group)):
                continue
            # A nearby display line with changed size/face may be the tail of
            # this same label. Do not delete only the uniform prefix of a run.
            members = {id(line) for line in group}
            if any(id(other) not in members and abs(other.size-group[0].size) <= body*.3
                   and max(other.bbox[1]-box[3], box[1]-other.bbox[3], 0) <= body
                   and (abs((other.bbox[0]+other.bbox[2]-box[0]-box[2])/2) <= body*2
                        or _overlap(box, other.bbox) >= min(box[2]-box[0], other.bbox[2]-other.bbox[0])*.8)
                   for other in lines[page.number]):
                continue
            # Separation from every overlapping prose line is stricter than
            # left-edge alignment: a centred heading still owns nearby prose.
            if any(_overlap(box, other.bbox) >= min(box[2]-box[0], other.bbox[2]-other.bbox[0])*.5
                   and max(other.bbox[1]-box[3], box[1]-other.bbox[3], 0) <= body*2.5
                   for other in local_prose):
                continue
            strong = bool(body_style and style[0] != body_style[0]
                          and style[1] is not None and body_style[1] is not None
                          and style[1] != body_style[1])
            candidates.append((page, group, style, strong))
            if strong:
                seeds.add(style)
    for page, group, style, strong in candidates:
        if strong or style in seeds:
            ignored = {id(w) for line in group for w in line.words}
            page.words = [w for w in page.words if id(w) not in ignored]
            # Background matches never broaden the ordinary-image classifier.
            # Its historical font/colour style key has no size or context guard.


def remove_figure_captions(page: PageData, *, known_styles: set[tuple] | None = None) -> None:
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
    known_styles = known_styles or set()
    prose = [line for line in lines if abs(line.size-page.body_size) <= page.body_size * 0.08
             and _style(line.words) not in known_styles
             and not _inside_protected(line, page)]
    if not prose and not known_styles:
        return
    body_style = _style([w for line in prose for w in line.words]) if prose else None
    # A caption can use the body font size but a different colour. It must not
    # protect its own neighbouring lines by masquerading as a prose column.
    prose = [line for line in prose if _style(line.words) == body_style]
    # Literary pull quotes are text, unlike handwritten figure labels. A
    # repeated moderately enlarged italic style provides independent evidence.
    display = [line for line in lines if italic(line)
               and page.body_size*1.15 <= line.size <= page.body_size*1.8]
    for line in display:
        if any(other is not line and abs(other.size-line.size)<=.5
               and abs(other.bbox[0]-line.bbox[0])<=page.body_size*2
               and max(other.bbox[1]-line.bbox[3],line.bbox[1]-other.bbox[3],0)<=page.body_size*1.5
               for other in display):
            prose.append(line)
    # A secondary column may use a different body face (e.g. an ability's
    # sans-serif rules). Consistent paragraph runs are prose, not captions.
    for first in lines:
        if not (page.body_size * .75 <= first.size <= page.body_size * 1.15
                and len(first.words) >= 3):
            continue
        run = [first]
        for following in lines:
            previous = run[-1]
            if (0 <= following.bbox[1]-previous.bbox[3] <= page.body_size*.8
                    and abs(following.bbox[0]-first.bbox[0]) <= page.body_size*2
                    and abs(following.size-first.size) <= .5
                    and _style(following.words) == _style(first.words)):
                run.append(following)
        if len(run) >= 3 and sum(len(line.text) for line in run) >= 100:
            known = {id(line) for line in prose}
            prose.extend(line for line in run if id(line) not in known)
    candidates: list[tuple[Line, set[int]]] = []
    for line in lines:
        if not (1 <= len(line.words) <= 14 and len(line.text) <= 120
                and (line.size <= page.body_size*3 or _style(line.words) in known_styles
                     and line.size <= page.body_size*4)):
            continue
        if _inside_protected(line, page) or _style(line.words) == body_style:
            continue
        # Distinct styling and proximity are insufficient if the line belongs
        # to a text column (e.g. a section heading over a decorated background).
        if _belongs_to_prose(line, prose, page.body_size):
            continue
        if _continues_inline_prose(line, lines, prose, page, body_style):
            continue
        related = _related_images(line, images, page.body_size)
        # Group the whole styled run, including lines extending beyond the
        # image. Otherwise a long uncertain label could lose only its first
        # (narrower) line while the remaining lines survive.
        candidates.append((line, related))
    groups: list[list[tuple[Line, set[int]]]] = []
    for line, related in candidates:
        for group in groups:
            previous, previous_images = group[-1]
            center_shift = abs((line.bbox[0]+line.bbox[2]-previous.bbox[0]-previous.bbox[2])/2)
            if (_style(line.words) == _style(previous.words)
                    and abs(line.size-previous.size) <= 0.5
                    and -max(page.body_size * .3, line.size * .5) <= line.bbox[1]-previous.bbox[3] <= page.body_size
                    and (center_shift <= page.body_size * 2 or abs(line.bbox[0]-previous.bbox[0]) <= page.body_size)):
                group.append((line, related))
                break
        else:
            groups.append([(line, related)])
    ignored = set()
    for group in groups:
        complete_four = len(group)==4 and bool(re.search(r'[.!?…][”\'\"]*$',group[-1][0].text))
        if ((len(group) <= 3 or complete_four) and all(related for _, related in group)
                and sum(len(line.text) for line, _ in group) <= 160):
            # Caption classification/style learning stays unchanged. Protect
            # ONLY lines containing positively identified game glyphs (also
            # explicit unknowns), including their adjacent ordinary words.
            ignored.update(id(w) for line, _ in group
                           if not any(g.symbol for w in line.words for g in w.glyphs)
                           for w in line.words)
            page.caption_styles.update(_style(line.words) for line,_ in group)
    page.words = [w for w in page.words if id(w) not in ignored]
