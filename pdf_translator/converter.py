"""Coordinate local PDF conversion without SDK-specific page objects."""

from collections.abc import Sequence
from pathlib import Path

from .export import render
from .headings import assign_heading_levels
from .layout import layout_page
from .continuations import merge_continuations
from .preprocessing import remove_running_margins
from .pdf_reader import read_pdf
from .models import ConversionError, ConversionResult, ConversionWarning, HyphenResolver
from .text import vocabulary
from .telemetry import emit, instrument, span


@instrument("conversion")
def convert_pdf(input_path: str | Path, *, pages: str | Sequence[int] | None = None, hyphen_resolver: HyphenResolver | None = None) -> ConversionResult:
    """Convert selected 1-based PDF pages without network access or file writes.

    ``pages`` accepts e.g. ``"1,3-5"`` or a sequence of physical page numbers.
    Heading styles and running margins use the whole PDF for context; only
    selected pages contribute output, hyphenation choices, and warnings.
    Warnings are structured data; the CLI is responsible for displaying them.
    ``hyphen_resolver(issue)`` may return True to join or False to keep a hyphen.
    Without a resolver, ambiguous hyphens are retained with warnings.
    """
    source = Path(input_path)
    if not source.is_file():
        raise ConversionError(f"PDF не найден: {source}")
    warnings: list[ConversionWarning] = []
    seen: set[tuple] = set()

    def warn(item: ConversionWarning) -> None:
        key = (item.code, item.message, item.pages)
        if key not in seen:
            seen.add(key)
            warnings.append(item)
            emit("conversion.warning", "WARNING", code=item.code, pages=item.pages)

    if hyphen_resolver is not None:
        original_resolver = hyphen_resolver

        def hyphen_resolver(issue):
            with span("conversion.hyphen_choice", page=issue.page):
                choice = original_resolver(issue)
                if type(choice) is bool:
                    emit("conversion.hyphen_decision", decision="join" if choice else "keep")
                return choice

    numbers, extracted = read_pdf(source, pages, warn)
    selected_pages = set(numbers)
    if not any(page.words for page in extracted if page.number in selected_pages):
        raise ConversionError("В выбранных страницах нет извлекаемого текста. Для сканов требуется OCR, который пока не поддерживается.")
    with span("conversion.vocabulary"):
        lexicon = vocabulary(w.text for page in extracted if page.number in selected_pages for w in page.words)
    with span("conversion.margins_filter"):
        remove_running_margins(extracted)
    document_blocks = []
    for page in extracted:
        with span("conversion.page_layout", page=page.number, selected=page.number in selected_pages):
            if page.number in selected_pages:
                document_blocks.extend(layout_page(page, lexicon, warn, hyphen_resolver))
            else:
                document_blocks.extend(layout_page(page, set(), lambda _: None))
    with span("conversion.headings"):
        assign_heading_levels(document_blocks, selected_pages, warn)
    blocks = [block for block in document_blocks if block.sources[0].page in selected_pages]
    with span("conversion.continuations"):
        empty_pages = {page.number for page in extracted if page.number in selected_pages and not page.words}
        blocks = merge_continuations(blocks, lexicon, warn, hyphen_resolver, empty_pages=empty_pages)
    with span("conversion.render"):
        markdown, text = render(blocks)
    if not text.strip():
        raise ConversionError("После обработки не осталось текста для экспорта.")
    emit("conversion.summary", pages_selected=len(numbers), blocks=len(blocks), warnings=len(warnings), characters=len(text))
    return ConversionResult(markdown, text, tuple(warnings), tuple(blocks))
