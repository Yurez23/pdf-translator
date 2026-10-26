"""Infer a conservative outline from recurring document structure.

Typography proposes comparable groups; it does not allocate one depth per size.
Only supported parent/child constraints add depth. Role decisions precede them.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from statistics import median

from .models import Block, ConversionWarning
from .paragraphs import italic
from .heading_context import centered, face, hint_for, overlap
from .page_data import PageData


def graphic_label(line, body: float) -> bool:
    """Retain symbolic glyphs/spaced numeric diagram rows outside the outline."""
    if line.words and all((len(w.text) == 1 or w.glyphs and all(g.symbol for g in w.glyphs)) and any(tag in w.font.lower() for tag in ("dingbat", "symbol", "icon"))
                          for w in line.words):
        return True
    return (len(line.words) >= 2 and all(w.text.isdecimal() or w.glyphs and all(
                g.symbol or g.text.isdecimal() for g in w.glyphs) for w in line.words)
            and line.size > body * 1.15
            and any(b.bbox[0] - a.bbox[2] > body * .8 for a, b in zip(line.words, line.words[1:])))


@dataclass(frozen=True)
class HeadingStyle:
    size: float
    font: str
    color: tuple[float, ...] | None


def _style(block: Block) -> HeadingStyle:
    line = block.lines[0]
    fonts: Counter[str] = Counter()
    colors: Counter[tuple[float, ...] | None] = Counter()
    for word in line.words:
        fonts[re.sub(r"^[A-Z]{6}\+", "", word.font)] += word.source_length
        colors[word.color] += word.source_length
    return HeadingStyle(line.size, fonts.most_common(1)[0][0] if fonts else "",
                        colors.most_common(1)[0][0] if colors else None)


def _close_size(a: float, b: float) -> bool:
    # Tolerate PDF numeric noise, not actual steps in the book's typography.
    return abs(a - b) <= max(0.2, max(a, b) * 0.02)


def _same_style(a: HeadingStyle, b: HeadingStyle) -> bool:
    return _close_size(a.size, b.size) and a.font == b.font and a.color == b.color


def _auxiliary_heading(block: Block, following: Block) -> str:
    # A normalized table can begin with a scoped group label. Its secondary
    # source is the original header; the label must not hide that table from
    # title classification or introduce a new document-wide heading style.
    a = block.sources[-1]
    b = following.sources[-1] if following.role == "table_group" else following.sources[0]
    if a.page != b.page:
        return ""
    gap = b.bbox[1] - a.bbox[3]
    overlap = min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0])
    width = min(a.bbox[2] - a.bbox[0], b.bbox[2] - b.bbox[0])
    if following.role == 'diagram' and 0 <= gap <= block.body_size*2.5 and overlap >= width*.75:
        return 'aside_title'
    if following.kind == "aside" and not following.children and following.lines and all(italic(line) for line in following.lines):
        centered = abs((a.bbox[0] + a.bbox[2] - b.bbox[0] - b.bbox[2]) / 2) <= block.body_size * 2
        if centered and 0 <= gap <= block.body_size * 1.5:
            return "aside_title"
    if following.kind == "table" or following.role in ("complex_table", "table_group"):
        if 0 <= gap <= block.body_size * 1.5 and overlap >= width * 0.75:
            return "table_title"
    # A short centered kicker directly above a larger title is a label, not a
    # preceding subsection. No chapter/volume words or book names are assumed.
    if following.kind == "heading" and following.lines:
        center_shift = abs((a.bbox[0] + a.bbox[2] - b.bbox[0] - b.bbox[2]) / 2)
        if (0 <= gap <= block.body_size * 2.5 and center_shift <= block.body_size
                and following.lines[0].size > block.lines[0].size * 1.1
                and len(block.text.split()) <= 6):
            return "title_label"
    return ""


def assign_heading_levels(
    blocks: list[Block], selected_pages: set[int], warn: Callable[[ConversionWarning], None],
    *, pages: Sequence[PageData] = (),
) -> None:
    """Assign levels in place, leaving text, reading order and geometry intact.

    Auxiliary titles do not create outline levels. If a table-adjacent heading's
    style is also used for ordinary sections, keep it a section: proximity alone
    must not demote that shared style. Different fonts/colors at the same size
    do not by themselves prove deeper nesting.
    """
    headings = [block for block in blocks if block.kind == "heading" and block.lines]
    page_map = {p.number:p for p in pages}
    sections: set[int] = set()
    for block in headings:
        page = page_map.get(block.sources[0].page)
        hint = hint_for(page,block.sources[0].bbox) if page else None
        if hint:
            if hint.role:
                block.role = hint.role
            if hint.scope == "section":
                sections.add(id(block))
        elif page:
            # Layout may group two distant fragments on one baseline, while
            # context deliberately split their gap. Require evidence for every
            # word; do not classify the intervening area or an uncovered word.
            word_hints = [hint_for(page,w.bbox) for line in block.lines for w in line.words]
            if (word_hints and word_hints[0] and word_hints[0].role
                    and all(h and h.role == word_hints[0].role for h in word_hints)):
                block.role = word_hints[0].role
    _compact_labels(blocks)
    styles = {id(block): _style(block) for block in headings}
    candidates = {}
    for index, block in enumerate(blocks[:-1]):
        if id(block) in styles and not block.role:
            role = _auxiliary_heading(block, blocks[index + 1])
            if role:
                candidates[id(block)] = role
    ordinary_styles = [styles[id(block)] for block in headings
                       if id(block) not in candidates and not block.role]
    for block in headings:
        candidate = candidates.get(id(block))
        if candidate and not any(_same_style(styles[id(block)], other) for other in ordinary_styles):
            block.role = candidate
            block.level = 0

    roots = {id(b) for a,b in zip(blocks,blocks[1:]) if a.role == "title_label"
             and b.kind == "heading" and not b.role and id(b) not in sections
             and a.sources[0].page == b.sources[0].page and a.lines and b.lines
             and _auxiliary_heading(a,b) == "title_label"}
    for block in headings:
        if block.role:
            block.level = block.outline_level = 0
    main = [block for block in headings if not block.role and id(block) not in roots|sections]
    bands: list[list[float]] = []
    for size in sorted((styles[id(block)].size for block in main), reverse=True):
        if bands and _close_size(size, median(bands[-1])):
            bands[-1].append(size)
        else:
            bands.append([size])
    centers = [median(band) for band in bands]
    group = {id(b):min(range(len(centers)),key=lambda i:abs(styles[id(b)].size-centers[i]))
             for b in main}
    sequence = [b for b in headings if not b.role and id(b) not in roots]
    if centers:
        # A recurring banner is a section boundary, alongside the first body
        # section under a corroborated document title. Its larger display face
        # does not introduce a further level between that section and its peers.
        primary = group[id(main[0])] if roots else 0
        group.update((ident,primary) for ident in sections)
        edges = _recurring_edges(sequence,group,centers,page_map)
    else:
        edges = set()
    levels, uncertain_groups = _group_depths(centers,edges,bool(roots))
    # Numbering must be corroborated by another heading's ancestor/sibling path.
    # Isolated decimal-looking titles do not establish a numbering convention.
    numbered = _numbered_levels([b for b in headings if not b.role])
    learned = defaultdict(set)
    for b in main:
        if id(b) in numbered:
            learned[group[id(b)]].add(numbered[id(b)])
    uncertain: set[int] = set()
    overflow_pages: set[int] = set()
    context: list[tuple[int,int]] = []
    for block in headings:
        if block.role:
            continue
        confident = True
        if id(block) in numbered:
            level = numbered[id(block)]
        elif id(block) in roots:
            level = 1
        elif id(block) in sections:
            level = 2 if roots else 1
        else:
            g = group[id(block)]
            level = next(iter(learned[g])) if len(learned[g]) == 1 else levels[g]
            if g in uncertain_groups and len(learned[g]) != 1:
                confident = False
                uncertain.update(s.page for s in block.sources if s.page in selected_pages)
        rank = level
        while context and context[-1][0] >= rank:
            context.pop()
        # A style's document-wide path can have optional intermediate roles.
        # Do not invent an absent parent when a visible, established ancestor
        # is open. Explicit numeric paths retain their absolute depth; an
        # initial fragment without an open ancestor retains document calibration.
        if context and id(block) not in numbered and id(block) not in roots|sections:
            level = min(rank,context[-1][1]+1)
        if confident:
            context.append((rank,level))
        block.outline_level = level
        block.level = min(level,6)
        if level > 6:
            overflow_pages.update(s.page for s in block.sources if s.page in selected_pages)
    if uncertain:
        warn(ConversionWarning("heading_hierarchy", "Вложенность части заголовков не подтверждена повторяющейся структурой; сохранён ближайший установленный уровень без добавления глубины. Проверьте оглавление.",
                               tuple(sorted(uncertain))))
    if overflow_pages:
        warn(ConversionWarning("heading_depth", "Установленная глубина заголовков превышает шесть: точный уровень сохранён в Block.outline_level, Markdown ограничен H6 и теряет различия глубоких уровней.",
                               tuple(sorted(overflow_pages))))


def _compact_labels(blocks: list[Block]) -> None:
    """Learn tightly attached title/subtitle pairs without demoting a style.

    Repeated alternating pairs without intervening prose form a reference list,
    not ten main sections. A single title followed by prose remains a section.
    """
    pairs = []
    for i,(a,b) in enumerate(zip(blocks,blocks[1:])):
        if (a.kind == b.kind == "heading" and not a.role and not b.role
                and a.lines and b.lines and a.sources[0].page == b.sources[0].page
                and a.lines[0].size > b.lines[0].size*1.25
                and len(b.text.split()) <= 6
                and -a.body_size*.3 <= b.sources[0].bbox[1]-a.sources[0].bbox[3] <= a.body_size*.25
                and (centered(a.sources[0].bbox,b.sources[0].bbox,a.body_size)
                     or abs(a.sources[0].bbox[0]-b.sources[0].bbox[0]) <= a.body_size*.5)):
            pairs.append((i,a,b))
    for i,a,b in pairs:
        similar = [(j,x,y) for j,x,y in pairs
                   if face(x.lines[0]) == face(a.lines[0]) and face(y.lines[0]) == face(b.lines[0])
                   and _close_size(x.lines[0].size,a.lines[0].size)
                   and _close_size(y.lines[0].size,b.lines[0].size)]
        if len(similar) >= 2:
            b.role = "title_label"
        run = [(j,x,y) for j,x,y in similar if x.sources[0].page == a.sources[0].page
               and abs(x.sources[0].bbox[0]-a.sources[0].bbox[0]) <= a.body_size
               and all(z.kind == "heading" for z in blocks[min(i,j):max(i,j)+2])]
        if len(run) >= 3:
            a.role,b.role = "aside_title","title_label"
            first = min(j for j,_,_ in run)
            if first and blocks[first-1].kind == "heading":
                title = blocks[first-1]
                if (title.sources[0].page == a.sources[0].page
                        and overlap(title.sources[0].bbox,a.sources[0].bbox) > 0):
                    title.role = "aside_title"


def _ruled(block: Block, pages: dict[int,PageData]) -> bool:
    page = pages.get(block.sources[0].page)
    a = block.sources[0].bbox
    return bool(page and any(abs(rule[1]-a[3]) <= block.body_size*.6
                and overlap(a,rule) >= (a[2]-a[0])*.8 for rule in page.heading_rules))


def _recurring_edges(main, group, centers, pages):
    positions = defaultdict(list)
    for i,b in enumerate(main):
        positions[group[id(b)]].append(i)
    edges = set()
    structural = set()
    normal_support = {}
    reverse_support = {}
    for parent,indices in positions.items():
        votes = Counter()
        reverse_votes = Counter()
        reverse_children = Counter()
        for left,right in zip(indices,indices[1:]):
            inner = main[left+1:right]
            if not inner:
                continue
            # Returning to an established ancestor closes this parent's final
            # child run before its next occurrence. Do not let later sibling
            # sections erase the evidence inside that bounded run.
            boundary = next((j for j,b in enumerate(inner)
                             if centers[group[id(b)]] > centers[parent]),len(inner))
            bounded = inner[:boundary]
            gs = {group[id(b)] for b in bounded}
            if gs and all(centers[g] < centers[parent] for g in gs):
                child = min(gs,key=lambda g:-centers[g])
                votes[child] += sum(group[id(b)] == child for b in bounded)
            elif (len({group[id(b)] for b in inner}) == 1 and len(inner) >= 2 and _ruled(main[left],pages)
                  and _ruled(main[right],pages) and all(not _ruled(b,pages) for b in inner)):
                reverse_votes[group[id(inner[0])]] += 1
                reverse_children[group[id(inner[0])]] += len(inner)
        normal_support.update(((parent,child),count) for child,count in votes.items())
        reverse_support.update(((parent,child),(count,reverse_children[child]))
                               for child,count in reverse_votes.items())
    edges = {edge for edge,count in normal_support.items() if count >= 2}
    structural = {edge for edge,(intervals,children) in reverse_support.items()
                  if intervals >= 2 and children >= normal_support.get((edge[1],edge[0]),0)}
    # Repeated ruled boundaries are independent evidence and outrank a size
    # proposal in the opposite direction, but not a more widely corroborated
    # ordinary relationship. Two local intervals can otherwise reverse the
    # dominant main-section/subsection pattern of an entire document.
    edges = (edges-{(b,a) for a,b in structural})|structural
    # Conflicting cycles carry no defensible ordering. Reject their edges rather
    # than choose a winner from the font sizes or occurrence order.
    def reaches(start,target,seen):
        if start == target:
            return True
        if start in seen:
            return False
        return any(reaches(b,target,seen|{start}) for a,b in edges if a == start)
    tail_edges = set()
    supported = {g for edge in edges for g in edge}
    for parent,indices in positions.items():
        if len(indices) < 2:
            continue
        tail = main[indices[-1]+1:]
        boundary = next((i for i,b in enumerate(tail)
                         if centers[group[id(b)]] > centers[parent]),None)
        if boundary is None:
            continue
        inner = tail[:boundary]
        children = {group[id(b)] for b in inner}
        # The final occurrence has no next peer to close its interval. A
        # previously established ancestor can close it instead, but only for
        # a recurring parent and repeated children of one still unknown group.
        # Do not grow depth from an open tail, a unique cascade or this new
        # evidence itself: reachability uses only the earlier document edges.
        if (len(inner) >= 2 and len(children) == 1
                and all(centers[g] < centers[parent] and g not in supported for g in children)
                and reaches(group[id(tail[boundary])],parent,set())):
            tail_edges.add((parent,next(iter(children))))
    edges |= tail_edges
    return {(a,b) for a,b in edges if not reaches(b,a,set())}


def _group_depths(centers, edges, rooted):
    count = len(centers)
    if not count:
        return {},set()
    incoming = {b for _,b in edges}
    primary = min(set(range(count))-incoming)
    base = 2 if rooted else 1
    levels = {g:base if g == primary else base if rooted else base+1 for g in range(count)}
    for _ in range(count):
        for a,b in sorted(edges):
            levels[b] = max(levels[b],levels[a]+1)
    supported = {primary}|{g for edge in edges for g in edge}
    if edges:
        for g in set(range(count))-supported:
            closest = min(supported,key=lambda other:(abs(centers[other]-centers[g]),other))
            levels[g] = levels[closest]
    return levels,set(range(count))-supported


def _numbered_levels(headings):
    paths = {}
    for b in headings:
        match = re.match(r"^(\d+(?:\.\d+)*)(?:[.)])?\s+\S",b.text)
        if match:
            path = tuple(int(n) for n in match[1].split('.'))
            if all(path):
                paths[id(b)] = path
    result = {}
    for ident,path in paths.items():
        if any(other != path and (other[:len(path)] == path or path[:len(other)] == other
                   or len(other) == len(path) and other[:-1] == path[:-1]) for other in paths.values()):
            result[ident] = len(path)
    return result
