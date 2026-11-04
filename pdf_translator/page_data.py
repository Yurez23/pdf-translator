"""Neutral extracted page data; no PDF SDK or layout engine imports."""

from dataclasses import dataclass, field
from .models import BBox, Word
from .diagram_geometry import DiagramPath, DiagramStructure


@dataclass(frozen=True)
class TableCell:
    """One physical cell; covered slots are continuations, never empty cells."""

    row: int
    column: int
    rowspan: int
    colspan: int
    text: str
    bbox: BBox


@dataclass(frozen=True)
class TableStructure:
    cells: tuple[TableCell, ...]
    row_edges: tuple[float, ...]
    column_edges: tuple[float, ...]
    header_rows: int = 1
    group_rows: tuple[int, ...] = ()


@dataclass
class TableData:
    bbox: BBox
    rows: list[list[str | None]]
    repaired_gaps: bool = False
    cell_boxes: list[list[BBox | None]] = field(default_factory=list)
    structure: TableStructure | None = None


@dataclass
class FormRegion:
    """Confirmed static form and its non-overlapping field panels."""

    bbox: BBox
    fields: tuple[BBox, ...]
    attachments: tuple[BBox, ...] = ()


@dataclass(frozen=True)
class HeadingHint:
    """Geometric evidence, not a preassigned outline depth."""

    bbox: BBox
    role: str = ""
    scope: str = ""
    protect: bool = False


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
    diagram_boxes: list[BBox] = field(default_factory=list)
    caption_styles: set[tuple] = field(default_factory=set)
    form_regions: list[FormRegion] = field(default_factory=list)
    heading_panels: list[BBox] = field(default_factory=list)
    heading_hints: list[HeadingHint] = field(default_factory=list)
    heading_rules: list[BBox] = field(default_factory=list)
    diagram_paths: list[DiagramPath] = field(default_factory=list)
    diagram_issues: tuple[str, ...] = ()
    diagram_obstacles: list[BBox] = field(default_factory=list)
    diagram_mappings: list[DiagramStructure] = field(default_factory=list)
    diagram_arrows: list[DiagramPath] = field(default_factory=list)
    circle_outlines: list[BBox] = field(default_factory=list)
    # Confirmed before caption filtering; individual label boxes retain order.
    quotation_regions: list[BBox] = field(default_factory=list)
    graphic_label_groups: list[tuple[BBox, ...]] = field(default_factory=list)
