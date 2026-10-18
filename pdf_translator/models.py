"""Conversion results retain source geometry for inspection and future UIs."""

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Literal

type BBox = tuple[float, float, float, float]
type BlockKind = Literal["paragraph", "heading", "list_item", "table", "aside", "footnote", "raw"]


class ConversionError(ValueError):
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
class Word:
    text: str
    bbox: BBox
    size: float
    font: str = ""
    upright: bool = True
    angle: float = 0.0
    color: tuple[float, ...] | None = None


@dataclass
class Line:
    words: list[Word]
    bbox: BBox
    size: float

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

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


@dataclass(frozen=True)
class ConversionResult:
    markdown: str
    text: str
    warnings: tuple[ConversionWarning, ...]
    blocks: tuple[Block, ...]


def union_bbox(boxes: list[BBox]) -> BBox:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
