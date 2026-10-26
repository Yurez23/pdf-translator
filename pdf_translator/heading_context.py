"""Document evidence for heading roles, before destructive caption filtering.

Only neutral words, regions and recurring arrangements are used here. A font,
colour, image rectangle or a heading's wording alone cannot establish its role.
This module neither opens PDFs nor renders, joins or deletes source text.
"""

import re
from collections import Counter

from .geometry import contains, group_lines
from .models import BBox, Line, union_bbox
from .page_data import HeadingHint, PageData
from .paragraphs import bold, italic
from .text import is_list, terminal


def face(line: Line) -> str:
    counts = Counter()
    for word in line.words:
        counts[re.sub(r"^[A-Z]{6}\+", "", word.font)] += word.source_length
    return counts.most_common(1)[0][0] if counts else ""


def overlap(a: BBox, b: BBox) -> float:
    return max(0., min(a[2], b[2]) - max(a[0], b[0]))


def encloses(a: BBox, b: BBox, tolerance: float = 1.) -> bool:
    return (a[0]-tolerance <= b[0] and a[1]-tolerance <= b[1]
            and b[2] <= a[2]+tolerance and b[3] <= a[3]+tolerance)


def centered(a: BBox, b: BBox, tolerance: float) -> bool:
    return abs((a[0]+a[2]-b[0]-b[2])/2) <= tolerance


def styled(line: Line, body: float) -> bool:
    return (not italic(line) and not is_list(line.text) and not terminal(line.text)
            and line.source_length <= 180
            and (line.size >= body*1.18 or all(bold(w) for w in line.words)))


def _prose(lines: list[Line], body: float) -> list[Line]:
    return [line for line in lines if body*.8 <= line.size <= body*1.15
            and len(line.words) >= 4 and not all(bold(w) for w in line.words)]


def _banner_pairs(page: PageData, lines: list[Line]):
    prose = _prose(lines, page.body_size)
    titles = [line for line in lines if styled(line,page.body_size)]
    for a,b in ((a,b) for a in titles for b in titles):
        if not (styled(a, page.body_size) and styled(b, page.body_size)
                and a.size > b.size*1.25 and a.size >= page.body_size*1.6
                and len(a.words) <= 8 and len(b.words) <= 8
                and -page.body_size*.3 <= b.bbox[1]-a.bbox[3] <= page.body_size
                and centered(a.bbox, b.bbox, page.body_size)):
            continue
        box = union_bbox([a.bbox,b.bbox])
        images = [im for im in page.images if encloses(im,box)
                  and im[2]-im[0] < page.width*.85]
        following = [p for p in prose if 0 <= p.bbox[1]-box[3] <= page.body_size*8
                     and overlap(p.bbox,box) >= (box[2]-box[0])*.8
                     and any(0 <= p.bbox[1]-im[3] <= page.body_size*3 for im in images)
                     and len([q for q in prose if face(q)==face(p)
                              and abs(q.bbox[0]-p.bbox[0]) <= page.body_size*2
                              and 0 <= q.bbox[1]-p.bbox[1] <= page.body_size*4]) >= 3]
        if images and following and not any(encloses(t.bbox,box) for t in page.tables):
            yield a,b


def learn_heading_context(pages: list[PageData]) -> None:
    """Freeze positive evidence before captions can erase a section's title.

    Repetition confirms the banner arrangement, not its words or an absolute
    location. Tall sidebar headers are classified without changing containers.
    """
    rows = {p.number: group_lines([w for w in p.words if w.upright],p.body_size,
                                 split_gaps=True) for p in pages}
    annotation_styles = []
    candidates = [(p,a,b) for p in pages for a,b in _banner_pairs(p,rows[p.number])]
    for page,a,b in candidates:
        peers = [(p,x,y) for p,x,y in candidates if face(x)==face(a) and face(y)==face(b)
                 and abs(x.size/p.body_size-a.size/page.body_size) <= .15
                 and abs(y.size/p.body_size-b.size/page.body_size) <= .15]
        if len({p.number for p,_,_ in peers}) >= 2:
            page.heading_hints.extend([HeadingHint(a.bbox,scope="section",protect=True),
                                       HeadingHint(b.bbox,"title_label",protect=True)])
    for page in pages:
        lines = rows[page.number]
        prose = _prose(lines,page.body_size)
        for line in lines:
            if not styled(line,page.body_size):
                continue
            # An independently enclosed header strip and text-bearing panel
            # establish a sidebar even when its height exceeds frame heuristics.
            bands = [b for b in page.heading_panels if encloses(b,line.bbox)
                     and page.body_size*.7 <= b[3]-b[1] <= page.body_size*3]
            if any(encloses(panel,band) and panel[3]-band[3] >= page.body_size*3
                   and panel[2]-panel[0] <= page.width*.75
                   and len([p for p in prose if encloses(panel,p.bbox)]) >= 2
                   for band in bands for panel in page.heading_panels):
                page.heading_hints.append(HeadingHint(line.bbox,"aside_title"))
        if page.diagram_boxes:
            region = union_bbox(page.diagram_boxes)
            above = [l for l in lines if styled(l,page.body_size)
                     and 0 <= region[1]-l.bbox[3] <= page.body_size*14
                     and region[0]-page.body_size <= l.bbox[0]
                     and l.bbox[2] <= region[2]+page.body_size]
            for line in above:
                if not any(l.bbox[1] >= line.bbox[3] and l.bbox[3] <= region[1]
                           and overlap(l.bbox,line.bbox) > 0 for l in prose):
                    page.heading_hints.append(HeadingHint(line.bbox,"aside_title"))
            for line in lines:
                if not styled(line,page.body_size) or line.bbox[1] < region[3]:
                    continue
                symbols = [w for w in page.words if any(g.symbol for g in w.glyphs)
                           and 0 <= w.bbox[1]-line.bbox[3] <= page.body_size*12
                           and line.bbox[0]-page.body_size <= w.bbox[0] <= line.bbox[2]+page.body_size]
                if len(symbols) >= 3:
                    page.heading_hints.append(HeadingHint(line.bbox,"aside_title"))
        # Curved labels have several distinct non-horizontal baselines. Retain
        # them verbatim; do not implement rotated-content removal or graph edges.
        angled = [w for w in page.words if w.upright and min(w.angle%360,360-w.angle%360)>3]
        families = {re.sub(r"^[A-Z]{6}\+","",w.font) for w in angled
                    if len({round(v.angle/5) for v in angled
                            if re.sub(r"^[A-Z]{6}\+","",v.font)==re.sub(r"^[A-Z]{6}\+","",w.font)}) >= 4}
        envelopes = {font: union_bbox([w.bbox for w in angled
                     if re.sub(r"^[A-Z]{6}\+","",w.font)==font]) for font in families}
        for line in lines:
            if (styled(line,page.body_size) and face(line) in families
                    and (all(min(w.angle%360,360-w.angle%360)>3 for w in line.words)
                         or encloses(envelopes[face(line)],line.bbox,page.body_size))):
                # Tangents near the top/bottom of a curved label may be nearly
                # horizontal. The observed envelope, rather than the face on
                # the whole page, links those fragments to the same graphic.
                page.heading_hints.append(HeadingHint(line.bbox,"graphic_label"))
        # Short centred annotations around an illustration are auxiliary text,
        # even when the caption filter conservatively keeps them. Classifying
        # the complete run here never joins or deletes its individual blocks.
        candidates = [l for l in lines if styled(l,page.body_size)
                      and l.source_length <= 100 and len(l.words) <= 14
                      and not any(encloses(t.bbox,l.bbox) for t in page.tables)
                      and not any(encloses(f,l.bbox) for f in page.frames)
                      and hint_for(page,l.bbox) is None]
        runs = []
        for line in candidates:
            for run in runs:
                previous = run[-1]
                if (face(line) == face(previous) and abs(line.size-previous.size) <= .5
                        and line.words[0].color == previous.words[0].color
                        and centered(line.bbox,previous.bbox,page.body_size)
                        and -page.body_size*.3 <= line.bbox[1]-previous.bbox[3] <= page.body_size*.6):
                    run.append(line)
                    break
            else:
                runs.append([line])
        for run in runs:
            box = union_bbox([l.bbox for l in run])
            if not 2 <= len(run) <= 4 or box[2]-box[0] > page.width*.45:
                continue
            adjacent = any(overlap(box,im) > 0
                           and im[2]-im[0] < page.width*.9
                           and max(im[1]-box[3],box[1]-im[3],0) <= page.body_size*5
                           for im in page.images)
            attached = any(overlap(box,p.bbox) >= (box[2]-box[0])*.5
                           and max(p.bbox[1]-box[3],box[1]-p.bbox[3],0) <= page.body_size*2.5
                           for p in prose)
            if adjacent and not attached:
                page.heading_hints.extend(HeadingHint(l.bbox,"graphic_label") for l in run)
                annotation_styles.append((face(run[0]),run[0].words[0].color,
                                          run[0].size/page.body_size))
    # A known multi-line illustration annotation can corroborate a detached
    # single label elsewhere. It still needs its own image and negative prose,
    # table and panel checks; the style alone never establishes the role.
    for page in pages:
        prose = _prose(rows[page.number],page.body_size)
        for line in rows[page.number]:
            if (not styled(line,page.body_size) or hint_for(page,line.bbox)
                    or len(line.words)>6 or line.source_length>60
                    or line.bbox[2]-line.bbox[0]>page.width*.35
                    or any(encloses(t.bbox,line.bbox) for t in page.tables)
                    or any(encloses(f,line.bbox) for f in page.frames)
                    or not any(face(line)==font and line.words[0].color==color
                               and abs(line.size/page.body_size-ratio)<=.2
                               for font,color,ratio in annotation_styles)):
                continue
            adjacent = any(overlap(line.bbox,im)>=(line.bbox[2]-line.bbox[0])*.5
                           and im[2]-im[0]<page.width*.9
                           and max(im[1]-line.bbox[3],line.bbox[1]-im[3],0)<=page.body_size*2.5
                           for im in page.images)
            attached = any(overlap(line.bbox,p.bbox)>=(line.bbox[2]-line.bbox[0])*.5
                           and max(p.bbox[1]-line.bbox[3],line.bbox[1]-p.bbox[3],0)<=page.body_size*2.5
                           for p in prose)
            if adjacent and not attached:
                page.heading_hints.append(HeadingHint(line.bbox,"graphic_label"))


def hint_for(page: PageData, box: BBox) -> HeadingHint | None:
    matches = [hint for hint in page.heading_hints if encloses(hint.bbox,box)]
    return next((h for h in matches if h.protect),matches[0] if matches else None)
