"""Conservative continuation across columns and physical pages."""

import re
from collections import Counter
from collections.abc import Callable
from .models import Block, ConversionWarning, HyphenResolver, Line
from .text import join_lines, terminal as _terminal

type Warn = Callable[[ConversionWarning], None]


def _line_font(line: Line) -> str:
    fonts: Counter[str] = Counter()
    for word in line.words:
        # PDF subset prefixes can differ between pages of the same document.
        fonts[re.sub(r"^[A-Z]{6}\+", "", word.font)] += len(word.text)
    return fonts.most_common(1)[0][0] if fonts else ""


def _closes_open_bracket(previous: str, following: str) -> bool:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for char in previous:
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
    depth = len(stack)
    if not depth:
        return False
    for char in following:
        if char in ".!?…":
            break
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
            if len(stack) < depth:
                return True
    return False


def _continuation_start(previous: str, following: str) -> bool:
    # A quotation mark is not a sentence boundary. Capitalized continuations
    # still need an extra signal: closing a bracket left open on the prior page.
    start = following.lstrip("\"'‘“«")
    return (start[:1].islower() or previous.endswith("-")
            or _closes_open_bracket(previous, following))


def _continuation_alignment(previous: Block, following: Block) -> bool:
    if previous.container_left is None or following.container_left is None:
        return False
    tolerance = previous.body_size * 0.45
    first = following.lines[0]
    # Returning to the left after the first line identifies a NEW paragraph,
    # including a paragraph following a list with a similar hanging indent.
    if any(line.bbox[0] < first.bbox[0] - tolerance for line in following.lines[1:]):
        return False
    offset = first.bbox[0] - following.container_left
    if abs(offset) <= tolerance:
        return True
    if previous.kind == "list_item" and len(previous.lines) > 1:
        hanging = previous.lines[-1].bbox[0] - previous.container_left
        return hanging > previous.body_size * 0.5 and abs(offset - hanging) <= tolerance
    return False


def merge_continuations(blocks: list[Block], lexicon: set[str], warn: Warn, resolver: HyphenResolver | None = None) -> list[Block]:
    result: list[Block] = []
    for block in blocks:
        # Marginal art credits must not break an otherwise clear page continuation.
        prev = next((b for b in reversed(result) if b.role != "rotated" and b.kind != "footnote"), None)
        if (prev and prev.kind in ("paragraph", "list_item") and block.kind == "paragraph"
                and prev.lines and block.lines):
            a, b = prev.sources[-1], block.sources[0]
            next_page = b.page == a.page + 1
            next_column = (a.page == b.page and prev.container_left is not None and block.container_left is not None
                           and block.container_left > prev.container_left + prev.body_size * 4)
            last = prev.lines[-1]
            first = block.lines[0]
            full_line = last.bbox[2] - a.bbox[0] >= prev.container_width * 0.75
            if ((next_page or next_column) and not _terminal(prev.text) and (full_line or prev.text.endswith("-"))
                    and _continuation_start(prev.text, block.text) and _continuation_alignment(prev, block)
                    and abs(first.size - last.size) <= prev.body_size * 0.15
                    and _line_font(first) == _line_font(last)):
                prev.text = join_lines([prev.text, block.text], lexicon, b.page, warn, resolver)
                prev.sources += block.sources
                prev.lines += block.lines
                prev.container_left = block.container_left
                prev.container_width = block.container_width
                continue
        result.append(block)
    return result
