"""Learn heading size bands from a document, not fixed point-size thresholds."""

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from statistics import median

from .models import Block, ConversionWarning
from .paragraphs import italic


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
) -> None:
    """Assign levels in place, leaving text, reading order and geometry intact.

    Auxiliary titles do not create outline levels. If a table-adjacent heading's
    style is also used for ordinary sections, keep it a section: proximity alone
    must not demote that shared style. Different fonts/colors at the same size
    do not by themselves prove deeper nesting.
    """
    headings = [block for block in blocks if block.kind == "heading" and block.lines]
    styles = {id(block): _style(block) for block in headings}
    candidates = {}
    for index, block in enumerate(blocks[:-1]):
        if id(block) in styles:
            role = _auxiliary_heading(block, blocks[index + 1])
            if role:
                candidates[id(block)] = role
    ordinary_styles = [styles[id(block)] for block in headings if id(block) not in candidates]
    for block in headings:
        candidate = candidates.get(id(block))
        if candidate and not any(_same_style(styles[id(block)], other) for other in ordinary_styles):
            block.role = candidate
            block.level = 0

    main = [block for block in headings if not block.role]
    bands: list[list[float]] = []
    for size in sorted((styles[id(block)].size for block in main), reverse=True):
        if bands and _close_size(size, median(bands[-1])):
            bands[-1].append(size)
        else:
            bands.append([size])
    centers = [median(band) for band in bands]
    overflow_pages: set[int] = set()
    for block in main:
        level = min(range(len(centers)), key=lambda i: abs(styles[id(block)].size - centers[i])) + 1
        block.level = min(level, 6)
        if level > 6:
            overflow_pages.update(s.page for s in block.sources if s.page in selected_pages)
    if overflow_pages:
        warn(ConversionWarning("heading_depth", "В документе больше шести уровней заголовков: глубокие уровни сохранены как H6; проверьте вложенность.",
                               tuple(sorted(overflow_pages))))
