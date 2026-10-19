"""Provider-independent batch workflow; no filesystem, SDK or concrete stores."""

from dataclasses import dataclass
from collections.abc import Callable

from ..telemetry import emit, span, translation_warning
from .chunking import split_batch
from .document import render_document
from .execution import RequestController
from .journal import RunJournal, valid_catalog, valid_model_id
from .models import BatchFailure, BatchResponseFailed, TranslationError, TranslationOptions, TranslationPaused, TranslationPlan, TranslationWarning
from .response_recovery import translate_checked
from .planning import make_request
from .ports import BatchTooLarge, TranslationProvider, TranslationResponse
from .validation import validate_translations, validate_text


@dataclass(frozen=True)
class WorkflowResult:
    status: str
    completed: int
    warnings: tuple[TranslationWarning, ...]
    pause_reason: str | None
    failed_batches: tuple[BatchFailure, ...] = ()


def run_translation(plan: TranslationPlan, options: TranslationOptions, provider: TranslationProvider,
                    journal: RunJournal, controller: RequestController, *,
                    resume: bool, check_source: Callable[[], None],
                    export: Callable[[str, str], None], progress: Callable[[str], None]) -> WorkflowResult:
    cached = journal.translations
    status = "complete"
    pause_reason = None
    try:
        if any(segment.id not in cached for segment in plan.segments):
            with span("provider.prepare"):
                session = provider.prepare(options)
                if not callable(getattr(session, "translate", None)):
                    raise TranslationError("prepare(options) должен вернуть TranslationSession; обновите адаптер по инструкции перехода.")
        if not resume:
            journal.save()
        if any(segment.id not in cached for segment in plan.segments):
            pending = [(f"b{index + 1:06d}", tuple(segment for segment in batch if segment.id not in cached))
                       for index, batch in enumerate(plan.batches)]
            pending = [(identifier, batch) for identifier, batch in pending if batch]
            while pending:
                check_source()
                batch_id, batch = pending.pop(0)
                with span("translation.batch", batch_id=batch_id, segments=len(batch)) as outcome:
                    request = make_request(plan, batch, options)
                    try:
                        response = translate_checked(session, request, executor=controller, options=options)
                    except BatchTooLarge:
                        left, right = split_batch(plan.document, batch)
                        pending[0:0] = [(batch_id + ".1", left), (batch_id + ".2", right)]
                        emit("translation.batch_split", reason="input_limit", segments=len(batch))
                        outcome["status"] = "split"
                        continue
                    except BatchResponseFailed as error:
                        check_source()
                        record_failed_batch(batch_id, batch, error, journal, progress)
                        outcome["status"] = "incomplete"
                        continue
                    accept_response(response, batch, options, journal, len(plan.segments), progress)
                    cached = journal.translations
    except TranslationPaused as error:
        status = "paused"
        pause_reason = str(error)
    return finish_translation(plan, journal, controller, status, pause_reason, check_source, export)


def accept_response(response, batch, options, journal, total, progress):
    if isinstance(response, TranslationResponse):
        model, catalog = response.model, response.catalog
        response = response.translations
    else:
        model, catalog = options.model, ()
    if not valid_model_id(model) or not valid_catalog(catalog):
        raise TranslationError("Адаптер вернул некорректные сведения о модели/каталоге.")
    with span("translation.validate"):
        translated, _ = validate_translations(response, batch)
    journal.accept(translated, model, catalog)
    emit("translation.batch_saved", model=model, completed=len(journal.translations), total=total)
    progress(f"Сохранён прогресс: {len(journal.translations)}/{total} сегментов.")


def record_failed_batch(batch_id, batch, error, journal, progress):
    journal.fail_batch(BatchFailure(batch_id, tuple(s.id for s in batch), error.error.code, error.attempts, error.model))
    emit("translation.batch_failed", "WARNING", response_issue=error.error.code, segments=len(batch), response_attempt=error.attempts)
    progress(f"Проблемный пакет {batch_id}: {len(batch)} сегментов, {error} Остальные пакеты продолжаются.")


def finish_translation(plan, journal, controller, status, pause_reason, check_source, export):
    cached = journal.translations
    if status == "complete" and journal.failed_batches:
        status = "incomplete"
    if status == "complete" and len(cached) != len(plan.segments):
        raise TranslationError("Перевод неполон; экспорт запрещён, используйте --resume.")
    warnings = list(plan.document.warnings)
    for segment in plan.segments:
        if segment.id in cached:
            _, checks = validate_text(segment.text, cached[segment.id], segment.id)
            warnings.extend(checks)
    for warning in warnings:
        translation_warning(warning)
    if status == "complete":
        check_source()
        assembled = {}
        for segment in plan.segments:
            assembled[segment.unit_id] = assembled.get(segment.unit_id, "") + cached[segment.id] + segment.separator
        with span("translation.render"):
            markdown, text = render_document(plan.document, assembled)
        export(markdown, text)
    emit("translation.summary", status="success" if status == "complete" else status,
         completed=len(cached), total=len(plan.segments), calls=controller.calls, warnings=len(set(warnings)))
    return WorkflowResult(status, len(cached), tuple(dict.fromkeys(warnings)), pause_reason, journal.failed_batches)
