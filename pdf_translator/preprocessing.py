"""Page and document cleanup; preserves extraction and reading-order policies."""

import math
import re
from .geometry import group_lines
from .page_data import PageData
from .models import Line, Word, union_bbox
from .text import normalize


def remove_marginal_credits(page: PageData) -> None:
    """Drop small, short, name-like vertical credits outside the body columns.

    No single signal (not even rotation) is enough. Other rotated text is retained
    with the usual warning. Font size along rotated text is unreliable in PDF
    extraction, so use the transverse width of the vertical group instead.
    """
    body = [w for w in page.words if w.upright and page.body_size * 0.85 <= w.size <= page.body_size * 1.15 and len(w.text) > 1]
    if not body:
        return
    left, right = min(w.bbox[0] for w in body), max(w.bbox[2] for w in body)
    candidates = []
    for word in page.words:
        vertical = not word.upright and min(abs(word.angle-90), abs(word.angle-270)) <= 2
        outside = (word.bbox[2] <= page.width * 0.08 and word.bbox[2] < left - page.body_size * 0.5
                   or word.bbox[0] >= page.width * 0.92 and word.bbox[0] > right + page.body_size * 0.5)
        if vertical and outside:
            candidates.append(word)
    groups: list[list[Word]] = []
    for word in sorted(candidates, key=lambda w: (w.font, round(w.bbox[0]), w.bbox[1])):
        compatible = [group for group in groups if group[0].font == word.font and abs(group[0].angle-word.angle) <= 2
                      and abs(group[0].bbox[0]-word.bbox[0]) <= page.body_size * 0.35
                      and 0 <= word.bbox[1] - group[-1].bbox[3] <= page.body_size * 1.5]
        if compatible:
            compatible[-1].append(word)
        else:
            groups.append([word])
    ignored: set[int] = set()
    for group in groups:
        box = union_bbox([w.bbox for w in group])
        name_parts = ["".join(c for c in w.text if c.isalpha()) for w in group if w.text != "&"]
        name_like = (2 <= len(name_parts) <= 10 and sum(map(len, name_parts)) <= 100
                     and not any(any(c.isdigit() for c in w.text) for w in group)
                     and all(part.isalpha() and (part.isupper() or part.istitle() or part[::-1].istitle()) for part in name_parts))
        if (name_like and box[2]-box[0] <= page.body_size * 0.95
                and box[3]-box[1] >= (box[2]-box[0]) * 2
                and box[3]-box[1] <= page.body_size * (30 if any('&' in w.text or ';' in w.text for w in group) else 20)):
            ignored.update(id(w) for w in group)
    page.words = [w for w in page.words if id(w) not in ignored]


def integrate_dropcaps(page: PageData) -> None:
    """A large initial may share several lines, but belongs to the first word."""
    for cap in list(page.words):
        if not (cap.upright and len(cap.text) == 1 and cap.text.isalpha() and cap.size >= page.body_size * 2.5):
            continue
        candidates = [w for w in page.words if w is not cap and w.upright and w.text[:1].isalpha()
                      and page.body_size * .8 <= w.size <= page.body_size * 1.2
                      and 0 <= w.bbox[0] - cap.bbox[2] <= page.body_size * 2
                      and cap.bbox[1] - 2 <= w.bbox[1] <= cap.bbox[1] + cap.size * 0.5]
        if not candidates:
            continue
        first = min(candidates, key=lambda w: (w.bbox[1], w.bbox[0]))
        page.dropcaps.append(cap.bbox)
        # A capitalized following word is distinct (e.g. a standalone "I").
        # Preserve the source spelling; never invent a missing letter.
        first.text = cap.text + (" " if first.text[:1].isupper() else "") + first.text
        first.bbox = (cap.bbox[0], first.bbox[1], first.bbox[2], first.bbox[3])
        first.size = page.body_size
        page.words.remove(cap)


def remove_running_margins(pages: list[PageData]) -> None:
    candidates: list[tuple[PageData, Line, tuple[str, str]]] = []
    occurrences: dict[tuple[str, str], set[int]] = {}
    for page in pages:
        for line in group_lines([w for w in page.words if w.upright], page.body_size, split_gaps=True):
            side = "top" if line.bbox[3] < page.height * 0.09 else "bottom" if line.bbox[1] > page.height * 0.91 else ""
            if not side:
                continue
            key = (side, normalize(re.sub(r"\d+", "", normalize(line.text).casefold())))
            occurrences.setdefault(key, set()).add(page.number)
            candidates.append((page, line, key))
    # Recto and verso pages often use different running titles.
    threshold = max(2, math.ceil(len(pages) * 0.3))
    remove: set[int] = set()
    for page, line, key in candidates:
        number = bool(re.fullmatch(r"\d{1,6}", line.text.strip()))
        if number or len(occurrences[key]) >= threshold:
            remove.update(id(w) for w in line.words)
    for page in pages:
        page.words = [w for w in page.words if id(w) not in remove]
