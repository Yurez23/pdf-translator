"""Provider-independent response validation; no parsing, planning or storage."""

import re
from collections import Counter
from collections.abc import Mapping

from .inline import MARKER
from .models import ResponseValidationError, TranslationError, TranslationSegment, TranslationWarning


def validate_text(source: str, translated: str, identifier: str) -> tuple[str, list[TranslationWarning]]:
    if not isinstance(translated, str) or not translated.strip() or "\x00" in translated:
        raise ResponseValidationError("invalid_text", segment_id=identifier)
    translated = re.sub(r"\s+", " ", translated).strip()
    if MARKER.findall(source) != MARKER.findall(translated):
        raise ResponseValidationError("protected_markers", segment_id=identifier)
    original = MARKER.sub("", source).strip()
    visible = MARKER.sub("", translated).strip()
    if len(original) >= 100 and not 0.2 <= len(visible) / len(original) <= 6:
        raise ResponseValidationError("response_length", segment_id=identifier)
    warnings = []
    if Counter(re.findall(r"\d+", original)) != Counter(re.findall(r"\d+", visible)):
        warnings.append(TranslationWarning("numbers_changed", f"{identifier}: изменился набор чисел; требуется сверка с оригиналом.", identifier))
    if len(original) >= 40 and visible == original:
        warnings.append(TranslationWarning("unchanged_text", f"{identifier}: текст не изменился; проверьте, выполнен ли перевод.", identifier))
    return translated, warnings


def validate_translations(value: Mapping[str, str], batch: tuple[TranslationSegment, ...]) -> tuple[dict[str, str], list[TranslationWarning]]:
    if not isinstance(value, Mapping):
        raise TranslationError("Адаптер должен вернуть отображение ID → перевод.")
    expected = {segment.id: segment for segment in batch}
    if set(value) != set(expected):
        raise ResponseValidationError("id_mismatch", expected_count=len(expected), received_count=len(value),
                                      missing_ids=tuple(i for i in expected if i not in value),
                                      unexpected_count=len(set(value) - set(expected)))
    output, warnings = {}, []
    for identifier, segment in expected.items():
        output[identifier], checks = validate_text(segment.text, value[identifier], identifier)
        warnings.extend(checks)
    return output, warnings
