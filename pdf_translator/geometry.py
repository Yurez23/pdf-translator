"""Shared geometry and line grouping; independent of the layout engine."""

from collections import Counter
from statistics import median
from .models import BBox, Word, Line, union_bbox


def contains(box: BBox, word: Word) -> bool:
    x = (word.bbox[0] + word.bbox[2]) / 2
    y = (word.bbox[1] + word.bbox[3]) / 2
    return box[0] - 1 <= x <= box[2] + 1 and box[1] - 1 <= y <= box[3] + 1


def body_size(words: list[Word]) -> float:
    sizes: Counter[float] = Counter()
    for word in words:
        if word.upright:
            sizes[round(word.size, 1)] += len(word.text)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def group_lines(words: list[Word], size: float, *, split_gaps: bool = False) -> list[Line]:
    """Group on baselines so mixed sizes/small capitals stay on the same line."""
    rows: list[list[Word]] = []
    baselines: list[float] = []
    for word in sorted(words, key=lambda w: (w.bbox[3], w.bbox[0])):
        candidates = [i for i in range(max(0, len(rows) - 4), len(rows)) if abs(baselines[i] - word.bbox[3]) <= max(2.0, size * 0.25)]
        if candidates:
            i = min(candidates, key=lambda j: abs(baselines[j] - word.bbox[3]))
            rows[i].append(word)
            baselines[i] = median(w.bbox[3] for w in rows[i])
        else:
            rows.append([word])
            baselines.append(word.bbox[3])
    lines: list[Line] = []
    for row in rows:
        groups: list[list[Word]] = [[]]
        for word in sorted(row, key=lambda w: w.bbox[0]):
            if split_gaps and groups[-1] and word.bbox[0] - groups[-1][-1].bbox[2] > size * 3:
                groups.append([])
            groups[-1].append(word)
        for group in groups:
            weights = Counter()
            for word in group:
                weights[round(word.size, 1)] += len(word.text)
            lines.append(Line(group, union_bbox([w.bbox for w in group]), weights.most_common(1)[0][0]))
    return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))
