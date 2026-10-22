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
            sizes[round(word.size, 1)] += word.source_length
    return sizes.most_common(1)[0][0] if sizes else 10.0


def group_lines(words: list[Word], size: float, *, split_gaps: bool = False) -> list[Line]:
    """Group on baselines so mixed sizes/small capitals stay on the same line."""
    rows: list[list[Word]] = []
    baselines: list[float] = []
    markers = [w for w in words if w.text in "•●▪◦‣◆◇" and len(w.text) == 1 and w.size <= size * 1.1]
    marker_ids = {id(w) for w in markers}
    for word in sorted((w for w in words if id(w) not in marker_ids), key=lambda w: (w.bbox[3], w.bbox[0])):
        candidates = [i for i in range(max(0, len(rows) - 4), len(rows)) if abs(baselines[i] - word.bbox[3]) <= max(2.0, size * 0.25)]
        if candidates:
            i = min(candidates, key=lambda j: abs(baselines[j] - word.bbox[3]))
            rows[i].append(word)
            baselines[i] = median(w.bbox[3] for w in rows[i])
        else:
            rows.append([word])
            baselines.append(word.bbox[3])
    # Small dingbat bullets do not share the prose baseline metrics. Attach
    # only to nearby text on the right with a matching vertical centre.
    for marker in markers:
        candidates = []
        for i, row in enumerate(rows):
            for word in row:
                gap = word.bbox[0] - marker.bbox[2]
                dy = abs((word.bbox[1] + word.bbox[3] - marker.bbox[1] - marker.bbox[3]) / 2)
                if (0 <= gap <= size * 1.8 and dy <= size * .4
                        and min(word.bbox[3], marker.bbox[3]) > max(word.bbox[1], marker.bbox[1])):
                    candidates.append((dy, gap, i))
        if candidates:
            rows[min(candidates)[2]].append(marker)
        else:
            rows.append([marker])
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
                weights[round(word.size, 1)] += word.source_length
            lines.append(Line(group, union_bbox([w.bbox for w in group]), weights.most_common(1)[0][0]))
    return sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))
