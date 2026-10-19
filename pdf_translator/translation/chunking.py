"""Deterministic semantic boundaries, independent of providers and tokenizers."""

import re
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass

from .inline import MARKER
from .models import TranslationDocument, TranslationError, TranslationSegment, TranslationWarning

POLICY_VERSION = "sections-paragraphs-sentences-v1"

# This is deliberately conservative, not a claim of linguistic sentence parsing.
_ENDS = re.compile(r'''[.!?…](?:["'’”»)\]]|⟦P\d{5}:\d+⟧)*(\s+)''')
_LEADING = re.compile(r'''(?:⟦P\d{5}:\d+⟧|["'‘“«([{ ])*''')
_ABBREVIATIONS = frozenset((
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "fig",
    "figs", "no", "nos", "vol", "vols", "pp", "p", "ch", "sec", "ed", "eds",
    "inc", "ltd", "approx", "dept", "rev", "cf", "al",
))


@dataclass(frozen=True)
class _Atom:
    unit_id: str
    text: str
    separator: str = ""


def _sentences(text: str) -> list[tuple[str, str]]:
    """Retain exact normalized separators; never fall back to a word boundary."""
    result, start = [], 0
    for match in _ENDS.finditer(text):
        before = MARKER.sub("", text[max(start, match.start() - 120):match.start() + 1])
        following = _LEADING.match(text, match.end()).end()
        if following == len(text):
            continue
        # Initials, acronyms, abbreviations and lowercase quote attributions do
        # not provide sufficiently confident sentence boundaries.
        if text[following].islower():
            continue
        if text[match.start()] == ".":
            word = re.search(r"([A-Za-z]+)\.$", before)
            if (word and (word[1].casefold() in _ABBREVIATIONS or len(word[1]) == 1)
                    or re.search(r"(?:\b[A-Za-z]\.){2,}$", before)):
                continue
        result.append((text[start:match.start(1)], match[1]))
        start = match.end()
    if start < len(text):
        result.append((text[start:], ""))
    return result


class _Layout:
    """Boundary ranks plus atomic containers and heading/body attachments.

    A section boundary outranks a block, then an inner container boundary,
    then a sentence. Headings inside containers are never global sections.
    """

    def __init__(self, document: TranslationDocument, atoms):
        self.atoms = atoms
        self.starts, self.ends = [], []
        offset = 0
        spans = {}
        self.ranks = {i: 0 for i in range(len(atoms) + 1)}
        for i, atom in enumerate(atoms):
            self.starts.append(offset)
            self.ends.append(offset + len(atom.text))
            if atom.unit_id not in spans:
                spans[atom.unit_id] = [i, i + 1]
            spans[atom.unit_id][1] = i + 1
            same = i + 1 < len(atoms) and atom.unit_id == atoms[i + 1].unit_id
            offset = self.ends[-1] + len(atom.separator if same else "\n\n")
        cursor = 0
        for unit in document.units:
            if unit.id in spans:
                cursor = spans[unit.id][1]
            elif unit.kind == "heading" and not any(c.isalpha() for c in MARKER.sub("", unit.text)):
                # Numeric headings are structural even though no translation is
                # requested for them. Their zero-width span still starts a section.
                spans[unit.id] = [cursor, cursor]
        self.containers = []
        self.container_labels = []
        self.attachments = []

        def visit(node, depth):
            owned = [tuple(spans[node.unit_id])] if node.unit_id in spans else []
            for row in node.rows:
                cells = [spans[cell] for cell in row if cell in spans]
                if cells:
                    row_span = (cells[0][0], cells[-1][1])
                    self.containers.append(row_span)
                    self.ranks[row_span[1]] = max(self.ranks[row_span[1]], 1)
                    owned.append(row_span)
            owned.extend(visit_nodes(node.children, depth + 1))
            if not owned:
                return None
            span = (owned[0][0], owned[-1][1])
            if node.kind in ("table", "list", "literal_list", "quote", "item"):
                self.containers.append(span)
                if node.kind != "item":
                    self.container_labels.append((span, node.kind))
            self.ranks[span[1]] = max(self.ranks[span[1]], 2 if depth == 0 else 1)
            return span

        def visit_nodes(nodes, depth):
            owned = []
            pending_heading = None
            for node in nodes:
                span = visit(node, depth)
                if span is None:
                    continue
                owned.append(span)
                if node.kind == "heading":
                    if depth == 0 and span[0] and pending_heading is None:
                        self.ranks[span[0]] = 3
                    if pending_heading is None:
                        pending_heading = span[0]
                elif pending_heading is not None:
                    # Include the first sentence/body atom, not an entire long
                    # paragraph. Consecutive parent/child headings stay together.
                    self.attachments.append((pending_heading, span[0] + 1))
                    pending_heading = None
            if pending_heading is not None:
                # An actual trailing empty heading remains source text, but a
                # run of consecutive headings must still not be split apart.
                self.attachments.append((pending_heading, owned[-1][1]))
            return owned

        visit_nodes(document.nodes, 0)
        self.ranks[len(atoms)] = 4
        # Range differences avoid rescanning every table/list for every cut.
        self._guard_cache = {}
        self.attached = self._mask(self.attachments)

    def size(self, start, end):
        if start == end:
            return 0
        return self.ends[end - 1] - self.starts[start]

    def _mask(self, spans):
        changes = [0] * (len(self.atoms) + 2)
        for start, end in spans:
            if end - start > 1:
                changes[start + 1] += 1
                changes[end] -= 1
        active, mask = 0, []
        for delta in changes:
            active += delta
            mask.append(bool(active))
        return mask

    def guards(self, limit):
        if limit not in self._guard_cache:
            self._guard_cache[limit] = self._mask(
                (start, end) for start, end in self.containers if self.size(start, end) <= limit)
        return self._guard_cache[limit]

    def choose(self, start, end, limit):
        # Search the hard window backwards, first for section endings. Containers
        # that fit the cap are atomic unless their heading attachment makes that
        # impossible (e.g. a full-size table preceded by its heading).
        guards = self.guards(limit)
        for preserve_containers in (True, False):
            best, rank = None, -1
            for boundary in range(end, start, -1):
                if self.attached[boundary] or preserve_containers and guards[boundary]:
                    continue
                priority = self.ranks[boundary]
                if priority > rank:
                    best, rank = boundary, priority
                    if rank >= 3:
                        break
            if best is not None:
                return best
        raise TranslationError(
            "Заголовок и начало его текста не помещаются вместе в max_chunk_chars; "
            "уменьшите заголовок/предложение или выберите другой исходный фрагмент.")


def chunk_document(document: TranslationDocument, limit: int):
    """Return stable unit segments and section-aware batches within a hard cap.

    The counted payload is item text plus two newline characters between units;
    instructions/context/wire encoding are separately subject to token limits.
    """
    atoms = []
    for unit in document.units:
        if not any(c.isalpha() for c in MARKER.sub("", unit.text)):
            continue
        parts = [(unit.text, "")] if unit.kind == "heading" else _sentences(unit.text)
        for text, separator in parts:
            if len(text) > limit:
                raise TranslationError(
                    f"{unit.id}: одно предложение или неделимый заголовок длиннее "
                    f"max_chunk_chars ({limit}). Текст не обрезан; сократите исходный "
                    "элемент или увеличьте лимит, но не выше 16000.")
            atoms.append(_Atom(unit.id, text, separator))
    if not atoms:
        return (), (), ()
    layout = _Layout(document, atoms)
    batches, segments = [], []
    counters = defaultdict(int)
    start, cuts = 0, []
    while start < len(atoms):
        window = bisect_right(layout.ends, layout.starts[start] + limit)
        end = layout.choose(start, window, limit)
        batch, i = [], start
        while i < end:
            first, i = i, i + 1
            while i < end and atoms[i].unit_id == atoms[first].unit_id:
                i += 1
            unit_id = atoms[first].unit_id
            text = "".join(atom.text + atom.separator for atom in atoms[first:i - 1]) + atoms[i - 1].text
            segment = TranslationSegment(f"{unit_id}.s{counters[unit_id]:04d}", unit_id,
                                         text, atoms[i - 1].separator)
            counters[unit_id] += 1
            batch.append(segment)
        segments.extend(batch)
        batches.append(tuple(batch))
        cuts.append(end)
        start = end
    warnings = []
    for (start, end), kind in layout.container_labels:
        index = bisect_right(cuts, start)
        if index < len(cuts) and cuts[index] < end:
            warnings.append(TranslationWarning("container_split",
                            f"{atoms[start].unit_id}: контейнер {kind} разделён между пакетами "
                            "по внутренним границам из-за max_chunk_chars; при экспорте он восстановится.", atoms[start].unit_id))
    return tuple(segments), tuple(batches), tuple(warnings)


def split_batch(document: TranslationDocument, batch: tuple[TranslationSegment, ...]):
    """Token-limit backoff without new IDs, split sentences or orphan headings.

    Keep useful semantic groups where possible. A single existing segment cannot
    be subdivided after checkpoints have been planned; fail with an actionable
    error instead of silently changing the plan or reusing incompatible progress.
    """
    if len(batch) < 2:
        raise TranslationError(
            "Один сегмент с контекстом/глоссарием не помещается в лимиты адаптера. "
            "Уменьшите max_chunk_chars или контекст/глоссарий; нужен новый план.")
    layout = _Layout(document, batch)
    half = layout.size(0, len(batch)) // 2
    end = max(1, bisect_right(layout.ends, half))
    try:
        boundary = layout.choose(0, end, half)
    except TranslationError:
        # The first whole heading/body group may be bigger than half. There may
        # still be a valid split later; choose the nearest such point.
        boundary = next((i for i in range(end + 1, len(batch)) if not layout.attached[i]), None)
        if boundary is None:
            raise TranslationError(
                "Заголовок с текстом не делится безопасно в лимитах адаптера. "
                "Уменьшите max_chunk_chars/контекст/глоссарий; нужен новый план.") from None
    return batch[:boundary], batch[boundary:]
