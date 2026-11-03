"""Local typographic evidence for paragraphs and hanging labelled entries."""

import re
from collections import Counter

from .models import Line
from .text import is_list, terminal


def bold(word) -> bool:
    return any(part in word.font.lower() for part in ("bold", "demi", "black"))


def italic(line: Line) -> bool:
    total = sum(w.source_length for w in line.words)
    marked = sum(w.source_length for w in line.words if any(
        part in w.font.lower() for part in ("italic", "oblique")))
    # A roman emphasized word inside an italic quotation does not end it.
    return bool(total) and marked >= total * .8


def _display_style(line: Line) -> tuple[str, tuple[float, ...] | None]:
    styles = Counter()
    for word in line.words:
        font = re.sub(r"^[A-Z]{6}\+", "", word.font).casefold()
        styles[font, word.color] += word.source_length
    return styles.most_common(1)[0][0]


def _center(line: Line) -> float:
    return (line.bbox[0] + line.bbox[2]) / 2


def quotation_opener(first: Line, following: Line, body: float) -> bool:
    """A completed roman line can introduce a matching centered italic run."""
    font, color = _display_style(first)
    next_font, next_color = _display_style(following)
    face = r"[-, ]?(?:roman|regular|italic|oblique)$"
    return (not italic(first) and italic(following) and not first.bold_start
            and len(first.words) >= 4 and terminal(first.text) and not is_list(first.text)
            and color == next_color and re.sub(face, "", font) == re.sub(face, "", next_font)
            and abs(first.size - following.size) <= .5
            and abs(_center(first) - _center(following)) <= body * .5
            and 0 <= following.bbox[1] - first.bbox[3] <= body)


def centered_quotation_lines(lines: list[Line], body: float) -> set[int]:
    """Recognize sustained centered verse before short-label filtering."""
    selected: set[int] = set()
    start = 0
    while start < len(lines):
        first = lines[start]
        if not (body * 1.15 <= first.size <= body * 1.8 and italic(first)):
            start += 1
            continue
        style = _display_style(first)
        end = start + 1
        while end < len(lines):
            line = lines[end]
            if not (italic(line) and _display_style(line) == style
                    and abs(line.size - first.size) <= .5
                    and abs(_center(line) - _center(first)) <= body * .5
                    and 0 <= line.bbox[1] - lines[end - 1].bbox[3] <= body):
                break
            end += 1
        run = lines[start:end]
        if (len(run) >= 5 and sum(terminal(line.text) for line in run) >= 2
                and max(line.bbox[0] for line in run) - min(line.bbox[0] for line in run) >= body * 2):
            selected.update(id(line) for line in run)
            if start and quotation_opener(lines[start - 1], first, body):
                selected.add(id(lines[start - 1]))
        start = end
    return selected


def lead_in(line: Line) -> bool:
    prefix = []
    for word in line.words:
        if not bold(word):
            break
        prefix.append(word.text)
    return bool(prefix and len(prefix) < len(line.words) and prefix[-1].endswith((".", ":")))


def bold_continuation(previous: Line, following: Line) -> bool:
    """A bold phrase can wrap before its final period; that is not a label."""
    if terminal(previous.text) or not previous.words or not following.words:
        return False
    a, b = previous.words[-1], following.words[0]
    strip = lambda font: re.sub(r"^[A-Z]{6}\+", "", font)
    return bold(a) and bold(b) and strip(a.font) == strip(b.font)


def hanging_entry(lines: list[Line], index: int, body: float) -> bool:
    """Require hanging continuation evidence, including completed first lines.

    A second aligned continuation or a peer label distinguishes a labelled
    entry from a single-line sentence followed by a first-line-indented paragraph.
    Colons additionally identify speaker/definition labels, not particular names.
    """
    line = lines[index]
    if index + 1 == len(lines) or not lead_in(line):
        return False
    following = lines[index + 1]
    offset = following.bbox[0] - line.bbox[0]
    if not (body * .5 < offset < body * 3
            and 0 <= following.bbox[1] - line.bbox[3] <= body * .8
            and abs(following.size - line.size) <= body * .15
            and italic(following) == italic(line)
            and not lead_in(following) and not is_list(following.text)):
        return False
    if not terminal(line.text):
        return True
    if returns_left(lines, index + 1, line.bbox[0], body):
        return False
    if any(w.text.endswith(":") for w in line.words if bold(w)):
        return True
    if index + 2 >= len(lines):
        return False
    after = lines[index + 2]
    if not (0 <= after.bbox[1] - following.bbox[3] <= body * .8
            and abs(after.size - line.size) <= body * .15):
        return False
    continuation = abs(after.bbox[0] - following.bbox[0]) <= body * .35 and not lead_in(after)
    peer = abs(after.bbox[0] - line.bbox[0]) <= body * .35 and lead_in(after)
    return (continuation or peer) and italic(after) == italic(line)


def returns_left(lines: list[Line], index: int, anchor: float, body: float) -> bool:
    """An indented first line followed by body text at the column edge."""
    if index + 1 >= len(lines):
        return False
    line, following = lines[index:index + 2]
    return (abs(following.bbox[0] - anchor) <= body * .45
            and following.bbox[0] < line.bbox[0] - body * .5
            and 0 <= following.bbox[1] - line.bbox[3] <= body * .8
            and abs(following.size - line.size) <= body * .15
            and italic(following) == italic(line)
            and not all(bold(word) for word in following.words)
            and not lead_in(following) and not is_list(following.text))


def hanging_run(lines: list[Line], index: int, body: float) -> bool:
    """A bold-started paragraph with at least two aligned hanging wraps."""
    if index + 2 >= len(lines) or not lines[index].bold_start:
        return False
    first, second, third = lines[index:index+3]
    continuation = abs(third.bbox[0]-second.bbox[0]) <= body*.35
    peer = (third.bold_start and terminal(second.text)
            and abs(third.bbox[0]-first.bbox[0]) <= body*.35)
    return (body * .5 < second.bbox[0]-first.bbox[0] < body * 3
            and (continuation or peer)
            and all(0 <= b.bbox[1]-a.bbox[3] <= body * .8
                    and abs(b.size-a.size) <= body * .15
                    for a,b in ((first,second),(second,third)))
            and not is_list(second.text) and not lead_in(third))


def style_boundary(previous: Line, following: Line, body: float) -> bool:
    """Separate standalone italic narration, not an inline italic phrase."""
    return (italic(previous) != italic(following) and terminal(previous.text)
            and following.bbox[1] - previous.bbox[3] >= body * .5)
