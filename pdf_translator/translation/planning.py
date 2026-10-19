"""Provider-independent offline planning, requests and domain validation."""

import json
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path

from ..telemetry import emit, instrument, span, translation_warning
from .chunking import POLICY_VERSION, chunk_document
from .document import parse_document
from .inline import MARKER
from .models import TranslationError, TranslationOptions, TranslationPlan, TranslationSegment, TranslationWarning
from .ports import ProviderIdentity, TranslationItem, TranslationRequest
from .serialization import digest


def normalize_glossary(glossary: Mapping[str, str] | None) -> dict[str, str]:
    if glossary is None:
        return {}
    if not isinstance(glossary, Mapping) or len(glossary) > 500:
        raise TranslationError("Глоссарий должен быть JSON-объектом не более чем из 500 пар.")
    result = {}
    for source, target in glossary.items():
        if not isinstance(source, str) or not isinstance(target, str) or not source.strip() or not target.strip():
            raise TranslationError("Термины глоссария должны быть непустыми строками.")
        if max(len(source), len(target)) > 500 or any(c in source + target for c in ("\x00", "\n", "\r")):
            raise TranslationError("Термин глоссария должен занимать одну строку, не более 500 символов.")
        source, target = source.strip(), target.strip()
        if source in result:
            raise TranslationError("Повторяющийся термин после удаления пробелов в глоссарии.")
        result[source] = target
    if len(json.dumps(result, ensure_ascii=False)) > 30000:
        raise TranslationError("Глоссарий слишком велик: максимум 30000 символов JSON.")
    return dict(sorted(result.items()))


@instrument("translation.plan")
def plan_translation(input_path: str | Path, options: TranslationOptions, *,
                     provider: ProviderIdentity,
                     glossary: Mapping[str, str] | None = None, limit_units: int | None = None) -> TranslationPlan:
    if not isinstance(provider, ProviderIdentity):
        raise TranslationError("Для плана требуется явный ProviderIdentity адаптера.")
    source = Path(input_path)
    if source.suffix.lower() not in (".md", ".txt"):
        raise TranslationError("Перевод принимает .md или .txt, а не PDF. Сначала выполните локальную конвертацию.")
    try:
        if source.stat().st_size > 20 * 1024 * 1024:
            raise TranslationError("Текстовый файл больше 20 МиБ; выберите отдельную главу.")
        raw = source.read_bytes()
        text = raw.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise TranslationError(f"Не удалось прочитать исходный UTF-8 файл ({type(exc).__name__}).") from None
    if limit_units is not None and (type(limit_units) is not int or limit_units < 1):
        raise TranslationError("limit_units должен быть положительным целым числом.")
    glossary = normalize_glossary(glossary)
    with span("translation.parse"):
        document = parse_document(text, markdown=source.suffix.lower() == ".md")
    # A preview selects whole top-level nodes, never half a table/sidebar/list.
    if limit_units is not None:
        from .models import TranslationDocument
        selected = []
        ids: set[str] = set()

        def owned(node):
            result = {node.unit_id} if node.unit_id else set()
            result.update(cell for row in node.rows for cell in row)
            for child in node.children:
                result.update(owned(child))
            return result

        for node in document.nodes:
            if len(ids) >= limit_units:
                break
            selected.append(node)
            ids.update(owned(node))
        document = TranslationDocument(tuple(selected), tuple(unit for unit in document.units if unit.id in ids),
                                       document.warnings + (TranslationWarning("preview", "Выбрана начальная проба документа; целиком сохранены её таблицы и контейнеры."),))
    with span("translation.chunk"):
        segments, batches, warnings = chunk_document(document, options.max_chunk_chars)
    if warnings:
        document = replace(document, warnings=document.warnings + warnings)
    semantic_options = asdict(options)
    for key in ("max_requests", "max_retries", "requests_per_minute", "tokens_per_minute", "timeout_seconds", "max_concurrency", "max_response_retries"):
        semantic_options.pop(key)
    manifest = {
        "source_sha256": digest(raw), "format": source.suffix.lower(), "options": semantic_options,
        "limit_units": limit_units, "glossary_sha256": digest(glossary),
        "provider": asdict(provider), "contract_version": 3, "chunking_policy": POLICY_VERSION,
        "plan_sha256": digest({"segments": [asdict(segment) for segment in segments],
                               "batches": [[segment.id for segment in batch] for batch in batches]}),
    }
    emit("translation.plan_stats", source_bytes=len(raw), units=len(document.units), segments=len(segments), batches=len(batches))
    for warning in document.warnings:
        translation_warning(warning)
    return TranslationPlan(document, tuple(segments), tuple(batches), manifest, digest(manifest), glossary)


def make_request(plan: TranslationPlan, batch: tuple[TranslationSegment, ...], options: TranslationOptions) -> TranslationRequest:
    units = {unit.id: unit for unit in plan.document.units}
    contexts = []
    for segment in batch:
        context = MARKER.sub("", units[segment.unit_id].context)
        if context and context not in contexts:
            contexts.append(context)
    # Include adjacent original prose, not previously generated model answers.
    ids = [unit.id for unit in plan.document.units]
    indexes = [ids.index(segment.unit_id) for segment in batch]
    for neighbor in (min(indexes) - 1, max(indexes) + 1):
        if 0 <= neighbor < len(ids):
            contexts.append(MARKER.sub("", plan.document.units[neighbor].text))
    return TranslationRequest(
        tuple(TranslationItem(segment.id, segment.text, units[segment.unit_id].kind) for segment in batch),
        options.source_language, options.target_language, dict(plan.glossary),
        "\n".join(contexts)[:options.context_chars],
    )
