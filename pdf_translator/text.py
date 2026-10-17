"""Conservative normalization: never paraphrase the source."""

import re
import unicodedata
from collections.abc import Callable, Iterable

from .models import ConversionWarning

_LIGATURES = str.maketrans({"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st"})
_WORD = re.compile(r"[^\W\d_]+(?:[-’'][^\W\d_]+)*", re.UNICODE)
_LIST = re.compile(r"^(?:[•●▪◦‣*\-–]\s+|\(?\d+[.)]\s+|[A-Za-z][.)]\s+)")


def normalize(text: str) -> str:
    # NFC preserves meaningful typography and mathematical characters.
    text = unicodedata.normalize("NFC", text.translate(_LIGATURES))
    return re.sub(r"[\s\u00a0\u2007\u202f]+", " ", text).strip()


def vocabulary(texts: Iterable[str]) -> set[str]:
    return {match.group().casefold() for text in texts for match in _WORD.finditer(normalize(text))}


def is_list(text: str) -> bool:
    return bool(_LIST.match(text))


def join_lines(
    lines: Iterable[str],
    lexicon: set[str],
    page: int,
    warn: Callable[[ConversionWarning], None],
) -> str:
    result = ""
    for raw in lines:
        line = normalize(raw)
        if not line:
            continue
        if not result:
            result = line.replace("\u00ad", "") if not line.endswith("\u00ad") else line
            continue
        if result.endswith("\u00ad"):
            result = result[:-1] + line
        elif result.endswith("-"):
            left = re.search(r"([^\W\d_]+)-$", result)
            right = re.match(r"([^\W\d_]+)", line)
            if left and right:
                whole = (left[1] + right[1]).casefold()
                compound = (left[1] + "-" + right[1]).casefold()
                if whole in lexicon and compound not in lexicon:
                    result = result[:-1] + line
                else:
                    result += line
                    if compound not in lexicon:
                        warn(ConversionWarning("ambiguous_hyphen", f"Неоднозначный перенос: {left[1]}-{right[1]}; дефис сохранён.", (page,)))
            else:
                result += line if re.search(r"\d-$", result) and re.match(r"\d", line) else " " + line
        else:
            result += " " + line
        # Internal discretionary hyphens are never significant.
        if not result.endswith("\u00ad"):
            result = result.replace("\u00ad", "")
    return result.replace("\u00ad", "")
