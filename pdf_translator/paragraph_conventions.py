"""Document-specific paragraph starts, inferred from ordinary internal boundaries.

No PDF SDK, rendering, callbacks or I/O. Missing or contradictory evidence never
authorizes continuation after a complete sentence.
"""

import re
from collections import Counter
from dataclasses import dataclass
from statistics import median

from .models import Block, Line
from .paragraphs import italic, lead_in
from .text import terminal


def line_font(line: Line) -> str:
    fonts: Counter[str] = Counter()
    for word in line.words:
        fonts[re.sub(r"^[A-Z]{6}\+", "", word.font)] += len(word.text)
    return fonts.most_common(1)[0][0] if fonts else ""


def _color(line: Line):
    return Counter(w.color for w in line.words).most_common(1)[0][0] if line.words else None


def _ordinary(block: Block) -> bool:
    return (block.kind == "paragraph" and not block.role and not block.children
            and len(block.lines) >= 2 and block.container_left is not None
            and block.container_width > block.body_size * 8
            and not block.text.startswith(tuple('"\'‘“«'))
            and not any(italic(line) for line in block.lines)
            and not lead_in(block.lines[0]))


@dataclass(frozen=True)
class ParagraphStart:
    font: str
    color: tuple[float, ...] | None
    size: float
    width: float
    indent: float
    gap: float
    page: int
    left: float

    def matches(self, block: Block) -> bool:
        first = block.lines[0]
        return (self.font == line_font(first) and self.color == _color(first)
                and abs(self.size - first.size) <= first.size * .05
                and abs(self.width - block.container_width) <= self.width * .15)


@dataclass(frozen=True)
class ParagraphConventions:
    starts: tuple[ParagraphStart, ...] = ()

    def continues(self, previous: Block, following: Block) -> bool:
        """A flush fragment contradicts a well-supported first-line-indent rule."""
        if not _ordinary(previous) or not _ordinary(following):
            return False
        witnesses = [s for s in self.starts if s.matches(previous) and s.matches(following)]
        # One local run is insufficient; require repeated internal boundaries
        # in at least two physical page/column containers. Any flush or spaced
        # peer is a counterexample to an indent-only paragraph convention.
        if (len(witnesses) < 3 or len({(s.page, round(s.left / s.size)) for s in witnesses}) < 2
                or any(not .7 <= s.indent <= 3 or not -.1 <= s.gap <= .65 for s in witnesses)):
            return False
        typical = median(s.indent for s in witnesses)
        if any(abs(s.indent - typical) > .2 for s in witnesses):
            return False
        last, first = previous.lines[-1], following.lines[0]
        size = first.size
        if (line_font(last) != line_font(first) or _color(last) != _color(first)
                or abs(last.size - size) > size * .05
                or abs(previous.container_width - following.container_width) > previous.container_width * .15
                or last.bbox[2] - last.bbox[0] < previous.container_width * .85
                or abs(last.bbox[0] - previous.container_left) > size * .2
                or abs(first.bbox[0] - following.container_left) > size * .2):
            return False
        # This fragment starts at a reading boundary, not at the author's first
        # line. Subsequent lines must confirm the same body alignment/style.
        return all(abs(line.bbox[0] - following.container_left) <= size * .2
                   and line_font(line) == line_font(first) and _color(line) == _color(first)
                   and abs(line.size - size) <= size * .05
                   for line in following.lines[1:])


def infer_paragraph_conventions(blocks: list[Block]) -> ParagraphConventions:
    starts = []
    for previous, following in zip(blocks, blocks[1:]):
        if not (_ordinary(previous) and _ordinary(following) and terminal(previous.text)):
            continue
        a, b = previous.sources[-1], following.sources[0]
        size = following.lines[0].size
        if (a.page != b.page or abs(previous.container_left - following.container_left) > size * .2
                or abs(previous.container_width - following.container_width) > previous.container_width * .15
                or line_font(previous.lines[-1]) != line_font(following.lines[0])
                or abs(previous.lines[-1].size - size) > size * .05
                or _color(previous.lines[-1]) != _color(following.lines[0])):
            continue
        gap = (following.lines[0].bbox[1] - previous.lines[-1].bbox[3]) / size
        if not -.1 <= gap <= 3:
            continue
        # Returning lines establish the body edge independently of first-line
        # punctuation/capitalization. Do not learn from hanging entries or wraps.
        if not all(abs(line.bbox[0] - following.container_left) <= size * .2
                   for line in following.lines[1:]):
            continue
        starts.append(ParagraphStart(line_font(following.lines[0]), _color(following.lines[0]),
                                     size, following.container_width,
                                     (following.lines[0].bbox[0] - following.container_left) / size,
                                     gap, b.page, following.container_left))
    return ParagraphConventions(tuple(starts))
