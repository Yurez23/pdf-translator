"""Recognize narrowly evidenced quotation and graphic text before filtering."""

from collections import Counter, defaultdict

from .geometry import contains, group_lines
from .heading_context import centered, encloses, face, styled
from .models import union_bbox
from .page_data import HeadingHint, PageData
from .text import is_list, terminal


def _style(line):
    return face(line), Counter(w.color for w in line.words).most_common(1)[0][0]


def _owned_regions(page):
    return ([t.bbox for t in page.tables] + page.frames + page.diagram_boxes
            + [box for f in page.form_regions for box in (f.bbox, *f.attachments)]
            + [n.source.bbox for d in page.diagram_mappings for n in d.nodes])


def _quotation_regions(page, lines):
    """A quoted passage followed by narration in one centered display run."""
    body = page.body_size
    groups = []
    for line in lines:
        if not (body * 1.15 <= line.size <= body * 3 and not is_list(line.text)):
            continue
        for group in groups:
            previous = group[-1]
            if (_style(line) == _style(previous) and abs(line.size-previous.size) <= .5
                    and centered(line.bbox, previous.bbox, body * 2)
                    and -line.size * .5 <= line.bbox[1]-previous.bbox[3] <= body):
                group.append(line)
                break
        else:
            groups.append([line])
    for group in groups:
        if not 2 <= len(group) <= 5:
            continue
        text = " ".join(line.text for line in group).strip()
        if text.startswith("“"):
            end = text.rfind("”")
        elif text.startswith('"'):
            end = text.rfind('"')
        else:
            continue
        narration = text[end+1:].strip() if end > 0 else ""
        if (len(text) >= 50 and len(narration) >= 12 and terminal(narration)
                and sum(terminal(line.text) for line in group) >= 2
                and not any(contains(box, word) for line in group for word in line.words
                            for box in _owned_regions(page))):
            yield union_bbox([line.bbox for line in group])


def _graphic_label_groups(page, lines):
    """Repeated detached labelled panels over the same localized illustration.

    A duplicate and a different label distinguish a repeated graphic key from
    a single ordinary caption. Do not infer what any mark or position means.
    """
    body = page.body_size
    candidates = defaultdict(list)
    for line in lines:
        if not (body * 1.15 <= line.size <= body * 2.5
                and 1 <= len(line.words) <= 10 and len(line.text) <= 80
                and not terminal(line.text) and not is_list(line.text)
                and len({(w.font, w.color) for w in line.words}) == 1
                and not any(contains(box, w) for box in _owned_regions(page) for w in line.words)):
            continue
        panels = [p for p in page.heading_panels if encloses(p, line.bbox, 0)
                  and p[2]-p[0] <= line.bbox[2]-line.bbox[0]+body*3
                  and p[3]-p[1] <= line.size*1.8]
        if len(panels) != 1:
            continue
        for index, image in enumerate(page.images):
            area = (image[2]-image[0]) * (image[3]-image[1])
            if (.15*page.width*page.height <= area < .85*page.width*page.height
                    and encloses(image, panels[0], 0)):
                candidates[index, _style(line), round(line.size, 1)].append((line, panels[0]))
    claimed = set()
    for group in sorted(candidates.values(), key=len, reverse=True):
        group = [(line, panel) for line, panel in group if id(line) not in claimed]
        counts = Counter(line.text.casefold() for line, _ in group)
        if len(group) < 3 or len(counts) < 2 or max(counts.values(), default=0) < 2:
            continue
        if len(set(panel for _, panel in group)) != len(group):
            continue
        group.sort(key=lambda pair: (pair[0].bbox[1], pair[0].bbox[0]))
        claimed.update(id(line) for line, _ in group)
        yield tuple(line.bbox for line, _ in group)


def _mapping_titles(page, lines):
    for mapping in page.diagram_mappings:
        region = union_bbox([node.source.bbox for node in mapping.nodes])
        candidates = [line for line in lines if styled(line, page.body_size)
                      and len(line.words) <= 8 and line.bbox[2]-line.bbox[0] <= (region[2]-region[0])*.7
                      and region[0] <= line.bbox[0] < line.bbox[2] <= region[2]
                      and 0 <= region[1]-line.bbox[3] <= page.body_size*1.5
                      and centered(line.bbox, region, page.body_size)]
        if len(candidates) == 1:
            yield HeadingHint(candidates[0].bbox, "aside_title", "cell_diagram", True)


def recognize_special_text(page: PageData) -> None:
    lines = group_lines([w for w in page.words if w.upright], page.body_size, split_gaps=True)
    page.quotation_regions = list(_quotation_regions(page, lines))
    page.graphic_label_groups = list(_graphic_label_groups(page, lines))
    page.heading_hints.extend(_mapping_titles(page, lines))
