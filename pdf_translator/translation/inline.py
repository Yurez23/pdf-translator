"""Protected inline markup shared by parsing, chunking and validation."""

import re

from .models import TranslationError, TranslationUnit


MARKER = re.compile(r"⟦P\d{5}:\d+⟧")


def escape_inline(text: str) -> str:
    return re.sub(r"([\\`*_\[\]<>|~])", r"\\\1", text)


def restore_inline(unit: TranslationUnit, text: str, *, markdown: bool) -> str:
    tokens = {token.marker: token for token in unit.protected}
    parts = []
    cursor = 0
    for match in MARKER.finditer(text):
        literal = text[cursor:match.start()]
        parts.append(escape_inline(literal) if markdown else literal)
        token = tokens.get(match[0])
        if token is None:
            raise TranslationError(f"Неизвестный защищённый маркер в {unit.id}.")
        parts.append(token.markdown if markdown else token.text)
        cursor = match.end()
    literal = text[cursor:]
    parts.append(escape_inline(literal) if markdown else literal)
    return "".join(parts)
