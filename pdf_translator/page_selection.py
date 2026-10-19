"""Physical 1-based page selection, independent of the PDF reader."""

import re
from collections.abc import Sequence
from .models import ConversionError


def parse_pages(pages: str | Sequence[int] | None, total: int) -> list[int]:
    if pages is None:
        return list(range(1, total + 1))
    if isinstance(pages, str):
        selected: set[int] = set()
        for part in pages.split(","):
            match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
            if not match:
                raise ConversionError("Страницы задаются числами или диапазонами, например: 1,3-5.")
            start, end = int(match[1]), int(match[2] or match[1])
            if not 1 <= start <= end <= total:
                raise ConversionError(f"Недопустимый диапазон страниц: {part.strip()}. В PDF страниц: {total}.")
            selected.update(range(start, end + 1))
    else:
        if any(type(n) is not int or not 1 <= n <= total for n in pages):
            raise ConversionError(f"Номера страниц должны быть целыми числами от 1 до {total}.")
        selected = set(pages)
    if not selected:
        raise ConversionError("Не выбраны страницы для обработки.")
    return sorted(selected)
