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
    return (start[:1].islower() or previous.endswith(("-", ",", ";"))
            or start.startswith("(")
            or bool(re.search(r"\b(?:the|a|an|their|your|our|his|her|its|my|this|these|those)$",previous,re.I))
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
    if (previous.kind == "list_item" or previous.lines[0].bold_start) and len(previous.lines) > 1:
        hanging = previous.lines[-1].bbox[0] - previous.container_left
        return hanging > previous.body_size * 0.5 and abs(offset - hanging) <= tolerance
    return False


def merge_continuations(blocks: list[Block], lexicon: set[str], warn: Warn, resolver: HyphenResolver | None = None,
                        *, empty_pages: set[int] | None = None,
                        selected_pages: set[int] | None = None,
                        insert_pages: set[int] | None = None) -> list[Block]:
    result: list[Block] = []
    diagram_titles = {id(a) for a,b in zip(blocks,blocks[1:])
                      if a.role == 'aside_title' and b.role == 'diagram'}
    for block in blocks:
        # Marginal art credits must not break an otherwise clear page continuation.
        prev = next((b for b in reversed(result) if b.role != "rotated" and b.kind != "footnote"
                     and id(b) not in diagram_titles
                     and not (b.kind == "aside" and (b.children or b.role == "quotation"))), None)
        if (prev and prev.kind in ("paragraph", "list_item") and block.kind == "paragraph"
                and prev.lines and block.lines):
            between = result[next(i for i in range(len(result)-1,-1,-1) if result[i] is prev)+1:]
            # Preserved forms stay after the mutable merged paragraph in the
            # logical list. A form behind its CURRENT physical tail has already
            # been crossed; it must not block a later ordinary column/page wrap.
            tail = prev.sources[-1]
            between = [item for item in between if not (item.role == "form_insert" and all(
                s.page < tail.page or s.page == tail.page and s.bbox[3] <= prev.lines[-1].bbox[1]
                for s in item.sources))]
            forms = [item for item in between if item.role == "form_insert"]
            if forms:
                if _merge_across_forms(prev,block,between,forms,selected_pages,insert_pages or set(),
                                       empty_pages or set(),lexicon,warn,resolver):
                    continue
                result.append(block)
                continue
            a, b = prev.sources[-1], block.sources[0]
            next_page = (b.page > a.page and all(p in (empty_pages or set())
                         for p in range(a.page+1,b.page)))
            next_column = (a.page == b.page and prev.container_left is not None and block.container_left is not None
                           and block.container_left > prev.container_left + prev.body_size * 4)
            last = prev.lines[-1]
            first = block.lines[0]
            full_line = last.bbox[2] - a.bbox[0] >= prev.container_width * 0.75
            # Bottom-column lines can be short because the next physical
            # column begins under artwork. A visible lowercase start, matching
            # face/alignment and an unfinished multi-line paragraph are needed
            # before accepting this weaker fullness signal.
            short_wrap = (next_column and len(prev.lines)>=2 and block.text[:1].islower()
                          and last.bbox[2]-last.bbox[0] >= prev.container_width*.6)
            if ((next_page or next_column) and not _terminal(prev.text) and (full_line or short_wrap or prev.text.endswith("-"))
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


def _merge_across_forms(previous: Block, following: Block, between: list[Block], forms: list[Block],
                        selected: set[int] | None, inserts: set[int], empty: set[int],
                        lexicon: set[str], warn: Warn, resolver: HyphenResolver | None) -> bool:
    """A form is transparent only along an independently proven body route."""
    a,b = previous.sources[-1],following.sources[0]
    if (selected is None or previous.kind != "paragraph" or previous.role or following.role
            or any(item.role != "form_insert" for item in between)
            or not all(p in selected for p in range(a.page,b.page+1))
            or not all(p in inserts | empty for p in range(a.page+1,b.page))):
        return False
    last,first = previous.lines[-1],following.lines[0]
    start = following.text.lstrip("\"'‘“«")
    strong_start = (start[:1].islower() or previous.text.endswith("-")
                    or _closes_open_bracket(previous.text,following.text)
                    or bool(re.search(r"\b(?:the|a|an|their|your|our|his|her|its|my|this|these|those)$",previous.text,re.I)))
    def color(line: Line):
        return Counter(w.color for w in line.words).most_common(1)[0][0] if line.words else None
    if (_terminal(previous.text) or not strong_start
            or not (last.bbox[2]-a.bbox[0] >= previous.container_width*.75 or previous.text.endswith("-"))
            or not _continuation_alignment(previous,following)
            or abs(first.size-last.size) > previous.body_size*.15
            or _line_font(first) != _line_font(last) or color(first) != color(last)):
        return False
    widths_match = (min(previous.container_width,following.container_width) > 0
                    and abs(previous.container_width-following.container_width)
                    <= max(previous.container_width,following.container_width)*.15)
    route = b.page > a.page
    if a.page == b.page:
        tolerance = previous.body_size*.45
        route = (previous.container_left is not None and following.container_left is not None
                 and abs(previous.container_left-following.container_left) <= tolerance
                 and all(f.sources[0].page == a.page
                         and min(s.bbox[1] for s in f.sources) >= last.bbox[3]-tolerance
                         and max(s.bbox[3] for s in f.sources) <= first.bbox[1]+tolerance
                         and f.sources[0].bbox[0] <= previous.container_left+tolerance
                         and f.sources[0].bbox[2] >= previous.container_left+previous.container_width-tolerance
                         for f in forms))
    if not widths_match or not route:
        pages = tuple(range(a.page,b.page+1))
        warn(ConversionWarning("insert_continuation", "Возможное продолжение через бланк не соединено: геометрия неоднозначна; проверьте границу абзаца.", pages))
        return False
    previous.text = join_lines([previous.text,following.text],lexicon,b.page,warn,resolver)
    previous.sources += following.sources
    previous.lines += following.lines
    previous.container_left,previous.container_width = following.container_left,following.container_width
    return True
