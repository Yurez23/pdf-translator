"""Conversion results retain source geometry for inspection and future UIs."""

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Literal

from .diagnostics import DiagnosticError

type BBox = tuple[float, float, float, float]
type BlockKind = Literal["paragraph", "heading", "list_item", "table", "aside", "footnote", "raw"]


class ConversionError(DiagnosticError):
    """An expected input or conversion failure suitable for display to a user."""


@dataclass(frozen=True)
class SourceRegion:
    page: int
    bbox: BBox


@dataclass(frozen=True)
class ConversionWarning:
    code: str
    message: str
    pages: tuple[int, ...]


@dataclass(frozen=True)
class HyphenationIssue:
    page: int
    hyphenated: str
    joined: str
    context: str


type HyphenResolver = Callable[[HyphenationIssue], bool]


@dataclass
class Glyph:
    """Original character or detected vector, before semantic text expansion."""

    text: str
    font: str
    bbox: BBox
    cid: int | None = None
    font_sha256: str = ""
    symbol: str = ""
    origin: str = "text"
    glyph_name: str = ""


@dataclass
class Word:
    text: str
    bbox: BBox
    size: float
    font: str = ""
    upright: bool = True
    angle: float = 0.0
    color: tuple[float, ...] | None = None
    glyphs: tuple[Glyph, ...] = field(default_factory=tuple)

    @property
    def source_length(self) -> int:
        return sum(max(1, len(g.text)) for g in self.glyphs) if self.glyphs else len(self.text)


@dataclass
class Line:
    words: list[Word]
    bbox: BBox
    size: float

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def source_length(self) -> int:
        return sum(w.source_length for w in self.words) + max(0, len(self.words)-1)

    @property
    def text_without_symbols(self) -> str:
        return " ".join("".join(g.text for g in w.glyphs if not g.symbol)
                        if any(g.symbol for g in w.glyphs) else w.text for w in self.words).strip()

    @property
    def text_left(self) -> float:
        """Text alignment without an added leading semantic icon."""
        positions = [g.bbox[0] for w in self.words for g in w.glyphs if not g.symbol and g.text.strip()]
        positions.extend(w.bbox[0] for w in self.words if not w.glyphs)
        return min(positions, default=self.bbox[0])

    @property
    def bold_start(self) -> bool:
        return bool(self.words and any(s in self.words[0].font.lower() for s in ("bold", "demi", "black")))


@dataclass
class Block:
    kind: BlockKind
    text: str
    sources: tuple[SourceRegion, ...]
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)
    # Lines are retained for conservative continuation across columns/pages.
    lines: list[Line] = field(default_factory=list, repr=False)
    body_size: float = field(default=10.0, repr=False)
    container_width: float = field(default=0.0, repr=False)
    role: str = field(default="", repr=False)
    container_left: float | None = field(default=None, repr=False)
    # A framed sidebar owns its heading/paragraph/list blocks. Its flat text is
    # retained for search/compatibility; exporters use children without duplication.
    children: list["Block"] = field(default_factory=list)


@dataclass(frozen=True)
class ConversionResult:
    markdown: str
    text: str
    warnings: tuple[ConversionWarning, ...]
    blocks: tuple[Block, ...]


def union_bbox(boxes: list[BBox]) -> BBox:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
