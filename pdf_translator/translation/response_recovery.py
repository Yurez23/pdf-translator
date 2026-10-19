"""Bounded whole-response recovery. No SDK, model selection, sleeps or splitting."""

from dataclasses import replace

from ..telemetry import current_field, emit, span
from .models import BatchResponseFailed, ResponseValidationError, TranslationError, TranslationSegment
from .ports import TranslationResponse
from .journal import valid_catalog, valid_model_id
from .validation import validate_translations


def checked_response(response, request):
    value = response.translations if isinstance(response, TranslationResponse) else response
    if isinstance(response, TranslationResponse) and (not valid_model_id(response.model) or not valid_catalog(response.catalog)):
        raise TranslationError("Адаптер вернул некорректные сведения о модели/каталоге.")
    segments = tuple(TranslationSegment(item.id, item.id, item.text) for item in request.items)
    translated, _ = validate_translations(value, segments)
    return replace(response, translations=translated) if isinstance(response, TranslationResponse) else translated


def rejected(error, attempt, options, request):
    known = {item.id for item in request.items}
    emit("translation.response_rejected", "WARNING", response_issue=error.code, response_attempt=attempt,
         expected_count=len(request.items), received_count=error.received_count,
         missing_count=len(error.missing_ids) if error.code == "id_mismatch" else None,
         unexpected_count=error.unexpected_count if error.code == "id_mismatch" else None,
         segment_id=error.segment_id if error.segment_id in known else None)
    # Only IDs from the original request are eligible for diagnostics; never log
    # arbitrary strings supplied as unexpected/duplicate IDs by the model.
    for identifier in [i for i in error.missing_ids if i in known][:100]:
        emit("translation.response_missing", "WARNING", segment_id=identifier)
    if attempt > options.max_response_retries:
        raise BatchResponseFailed(error, attempt, current_field("model", options.model)) from None
    emit("translation.response_retry", "WARNING", response_issue=error.code, response_attempt=attempt + 1)


def translate_checked(session, request, *, executor, options):
    current = request
    for attempt in range(1, options.max_response_retries + 2):
        try:
            with span("translation.response", response_attempt=attempt):
                return checked_response(session.translate(current, executor=executor), request)
        except ResponseValidationError as error:
            rejected(error, attempt, options, request)
            current = replace(request, response_issue=error.code)
    raise AssertionError("unreachable")


async def translate_checked_async(session, request, *, executor, options):
    current = request
    for attempt in range(1, options.max_response_retries + 2):
        try:
            with span("translation.response", response_attempt=attempt):
                return checked_response(await session.translate(current, executor=executor), request)
        except ResponseValidationError as error:
            rejected(error, attempt, options, request)
            current = replace(request, response_issue=error.code)
    raise AssertionError("unreachable")
