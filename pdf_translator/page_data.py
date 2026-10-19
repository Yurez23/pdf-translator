"""Neutral extracted page data; no PDF SDK or layout engine imports."""

from dataclasses import dataclass, field
from .models import BBox, Word


@dataclass
class TableData:
    bbox: BBox
    rows: list[list[str | None]]


@dataclass
class PageData:
    number: int
    width: float
    height: float
    words: list[Word]
    body_size: float
    tables: list[TableData] = field(default_factory=list)
    frames: list[BBox] = field(default_factory=list)
    separators: list[float] = field(default_factory=list)
    dropcaps: list[BBox] = field(default_factory=list)
    images: list[BBox] = field(default_factory=list)
