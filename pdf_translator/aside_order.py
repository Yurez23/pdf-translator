"""Conservatively place independent insets at established section boundaries.

This pass only permutes already assembled blocks. It does not infer paragraph
continuations, change heading levels, or claim ownership of ambiguous insets.
"""

from collections import Counter
from collections.abc import Sequence

from .heading_context import hint_for
from .models import Block
from .page_data import PageData
from .paragraph_conventions import line_font


def _frame(block: Block) -> bool:
    return (block.kind == "aside" and not block.role and bool(block.children)
            and len(block.sources) == 1)


def _body(block: Block, page: int) -> bool:
    return (block.kind == "paragraph" and not block.role and not block.children
            and bool(block.lines) and all(s.page == page for s in block.sources))


def _section(block: Block) -> bool:
    return block.kind == "heading" and not block.role and block.outline_level > 0


def _left_of(left, right, size: float) -> bool:
    return left[2] + size <= right[0]


def _aligned(a, b, size: float) -> bool:
    return abs(a[0] - b[0]) <= size * .65


def _same_body(a: Block, b: Block) -> bool:
    first, second = a.lines[-1], b.lines[0]

    def color(line):
        return Counter(w.color for w in line.words).most_common(1)[0][0] if line.words else None

    font, shade = line_font(first), color(first)
    return (bool(first.words and second.words and font) and shade is not None
            and font == line_font(second) and shade == color(second)
            and abs(first.size - second.size) <= max(a.body_size, b.body_size) * .05)


def _preceding_section(blocks: list[Block], index: int, page: int) -> Block | None:
    # Only an uninterrupted run of ordinary paragraphs can establish scope.
    for block in reversed(blocks[:index]):
        if _body(block, page):
            continue
        return block if (_section(block) and len(block.sources) == 1
                         and block.sources[0].page == page) else None
    return None


def _body_until_heading(blocks: list[Block], start: int, page: int,
                        left: float, size: float, scope: Block) -> int | None:
    """Find an explicit next section in the same column; return its index."""
    end = start
    while end < len(blocks) and _body(blocks[end], page):
        block = blocks[end]
        if len(block.sources) != 1 or abs(block.sources[0].bbox[0] - left) > size * .65:
            return None
        end += 1
    if not start < end <= start + 8 or end >= len(blocks):
        return None
    heading = blocks[end]
    if (not _section(heading) or len(heading.sources) != 1
            or heading.sources[0].page != page
            or heading.outline_level > scope.outline_level
            or abs(heading.sources[0].bbox[0] - left) > size * .65):
        return None
    return end


def _across_page(blocks: list[Block], index: int, pages: dict[int, PageData]) -> int | None:
    aside = blocks[index]
    if not _frame(aside) or index == 0 or index + 1 >= len(blocks):
        return None
    previous, following = blocks[index - 1], blocks[index + 1]
    page = pages.get(aside.sources[0].page)
    if not page or not _body(previous, page.number) or len(previous.sources) != 1:
        return None
    next_page = pages.get(page.number + 1)
    if not next_page or not _body(following, next_page.number) or len(following.sources) != 1:
        return None
    section = _preceding_section(blocks, index - 1, page.number)
    if section is None:
        return None
    a, b, box, title = (previous.sources[0].bbox, following.sources[0].bbox,
                        aside.sources[0].bbox, section.sources[0].bbox)
    size = page.body_size
    if (not _left_of(a, box, size) or not _aligned(title, a, size)
            or not _aligned(a, b, size) or not _same_body(previous, following)
            or not (box[1] <= title[1] <= a[1] and box[3] >= a[3])
            or a[3] < page.height * .84 or b[1] > next_page.height * .14):
        return None
    return _body_until_heading(blocks, index + 1, next_page.number, b[0], size, section)


def _across_column(blocks: list[Block], index: int, pages: dict[int, PageData]) -> int | None:
    aside = blocks[index]
    if not (_frame(aside) or aside.kind == "aside" and aside.role == "quotation"
            and len(aside.sources) == 1) or index == 0 or index + 1 >= len(blocks):
        return None
    previous, following = blocks[index - 1], blocks[index + 1]
    page = pages.get(aside.sources[0].page)
    if not page or not _body(previous, page.number) or not _body(following, page.number):
        return None
    if len(following.sources) != 1:
        return None
    section = _preceding_section(blocks, index - 1, page.number)
    if section is None:
        return None
    left, right, box, title = (previous.sources[0].bbox, following.sources[0].bbox,
                               aside.sources[0].bbox, section.sources[0].bbox)
    size = page.body_size
    if (not _left_of(left, right, size) or not _left_of(box, right, size)
            or not _aligned(title, left, size) or not _same_body(previous, following)
            or box[0] < left[0] - size or box[2] > left[2] + size
            or not 0 <= box[1] - left[3] <= size * 6):
        return None
    if len(previous.sources) == 1:
        # The next column begins at the level of the current section heading.
        if not (_frame(aside) and title[1] <= page.height * .14
                and right[1] <= title[1] + size * 2):
            return None
    elif len(previous.sources) == 2 and aside.role == "quotation":
        # The existing paragraph has already wrapped to the next column.
        tail = previous.sources[-1].bbox
        if (not _aligned(tail, right, size)
                or not 0 <= right[1] - tail[3] <= size * 2):
            return None
    else:
        return None
    return _body_until_heading(blocks, index + 1, page.number, right[0], size, section)


def _page_opener(blocks: list[Block], index: int, pages: dict[int, PageData]) -> int | None:
    aside = blocks[index]
    if not _frame(aside) or index + 3 >= len(blocks):
        return None
    page = pages.get(aside.sources[0].page)
    if not page or any(s.page == page.number for b in blocks[:index] for s in b.sources):
        return None
    heading, label, first = blocks[index + 1:index + 4]
    if (not _section(heading) or len(heading.sources) != 1 or heading.sources[0].page != page.number
            or label.kind != "heading" or label.role != "title_label" or len(label.sources) != 1
            or label.sources[0].page != page.number or not _body(first, page.number)):
        return None
    hint = hint_for(page, heading.sources[0].bbox)
    if not hint or hint.scope != "section":
        return None
    box, title, sub, body = (aside.sources[0].bbox, heading.sources[0].bbox,
                             label.sources[0].bbox, first.sources[0].bbox)
    size = page.body_size
    if (not _left_of(title, box, size) or not _left_of(body, box, size)
            or box[1] > page.height * .14 or not box[1] <= title[1] <= box[3] + size
            or abs((title[0] + title[2]) - (sub[0] + sub[2])) > size * 2
            or not 0 <= body[1] - sub[3] <= size * 8):
        return None
    end = index + 3
    while end < len(blocks) and _body(blocks[end], page.number):
        current = blocks[end]
        if len(current.sources) != 1 or not _left_of(current.sources[0].bbox, box, size):
            return None
        end += 1
    if end < len(blocks):
        following = blocks[end]
        if len(following.sources) != 1:
            return None
        source = following.sources[0]
        if source.page == page.number:
            # Do not put a frame between a lead-in and a following list/table
            # in the same column. A section heading or the other column does
            # establish an independent boundary.
            other_column = box[0] - size <= source.bbox[0] and source.bbox[2] <= box[2] + size
            if not (_section(following) or other_column):
                return None
        elif source.page == page.number + 1 and not _section(following):
            return None
    return end


def order_asides(blocks: list[Block], pages: Sequence[PageData], *,
                 page_seams: list[tuple[Block, Block]] | None = None) -> list[Block]:
    """Move insets and optionally record body pairs on confirmed page routes."""
    result = list(blocks)
    page_map = {page.number: page for page in pages}
    for aside in blocks:
        if aside.kind != "aside":
            continue
        index = next(i for i, block in enumerate(result) if block is aside)
        end = _page_opener(result, index, page_map) or _across_column(result, index, page_map)
        if end is None:
            end = _across_page(result, index, page_map)
            if end is not None and page_seams is not None:
                page_seams.append((result[index - 1], result[index + 1]))
        if end is not None:
            result.insert(end - 1, result.pop(index))
    return result
