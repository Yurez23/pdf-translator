"""Resumable, budgeted translation with per-batch validation and checkpoints."""

import math
import time
import re
from pathlib import Path

from .chunking import split_batch
from .document import render_document
from .models import (Progress, ProviderError, RequestLimitReached, TranslationError,
                     TranslationOptions, TranslationPlan, TranslationResult, TranslationPaused)
from .ports import BatchTooLarge, TranslationProvider, TranslationResponse
from .planning import digest, make_request, plan_translation, validate_translations, validate_text
from .storage import check_output_paths, checkpoint_lock, load_checkpoint, save_checkpoint, write_outputs


class RequestController:
    """One budget for ALL external calls, implementing the RequestExecutor port."""

    def __init__(self, options, state, checkpoint, progress, *, clock=time.time, sleep=time.sleep):
        self.options, self.state, self.checkpoint = options, state, checkpoint
        self.progress, self.clock, self.sleep = progress, clock, sleep
        self.calls = 0

    def call(self, operation, *, tokens: int = 0, label: str):
        if type(tokens) is not int or tokens < 0:
            raise TranslationError("Резерв токенов должен быть неотрицательным целым числом.")
        if tokens > self.options.tokens_per_minute:
            raise TranslationError("Один запрос превышает tokens_per_minute с резервом ответа. Уменьшите пакет/ответ или уточните квоту.")
        for attempt in range(self.options.max_retries + 1):
            if self.calls >= self.options.max_requests:
                raise RequestLimitReached("Достигнут лимит API-запросов этого запуска.")
            while True:
                now = self.clock()
                recent = [item for item in self.state["recent_requests"] if item["time"] > now - 60]
                self.state["recent_requests"] = recent
                if len(recent) < self.options.requests_per_minute and sum(item["tokens"] for item in recent) + tokens <= self.options.tokens_per_minute:
                    break
                wait = min(60.0, max(0.05, recent[0]["time"] + 60.05 - now))
                self.progress(f"Ожидание минутной квоты: {wait:.1f} с.")
                self.sleep(wait)
            self.calls += 1
            self.state["calls_total"] += 1
            self.state["recent_requests"].append({"time": self.clock(), "tokens": tokens})
            save_checkpoint(self.checkpoint, self.state)
            self.progress(f"API {self.calls}/{self.options.max_requests}: {label}.")
            try:
                return operation()
            except ProviderError as error:
                if not error.retryable or attempt >= self.options.max_retries:
                    raise
                delay = max(error.retry_after, min(30.0, 2.0 ** (attempt + 1)))
                if delay > 60:
                    raise
                self.progress(f"Временная ошибка; повтор через {delay:.1f} с.")
                self.sleep(delay)
        raise AssertionError("unreachable")


def _validate_state(state: dict, plan: TranslationPlan) -> None:
    if state.get("version") != 3 or state.get("fingerprint") != plan.fingerprint or state.get("manifest") != plan.manifest:
        raise TranslationError("Журнал относится к другому исходнику, адаптеру/его ревизии, модели, глоссарию или плану. Используйте прежние настройки либо новую папку.")
    expected = {segment.id: segment for segment in plan.segments}
    translations = state.get("translations")
    if not isinstance(translations, dict) or not set(translations) <= set(expected):
        raise TranslationError("В журнале есть неизвестные или некорректные сегменты.")
    models = state.get("segment_models")
    if not isinstance(models, dict) or set(models) != set(translations) or any(not _model_id(value) for value in models.values()):
        raise TranslationError("В журнале отсутствует или повреждена информация о моделях сегментов.")
    if not _catalog(state.get("catalog")):
        raise TranslationError("Некорректный снимок каталога моделей в журнале.")
    for identifier, value in translations.items():
        normalized, _ = validate_text(expected[identifier].text, value, identifier)
        if normalized != value:
            raise TranslationError("Перевод в журнале изменён или имеет неверный формат.")
    if type(state.get("calls_total")) is not int or state["calls_total"] < 0:
        raise TranslationError("Некорректный счётчик запросов в журнале.")
    recent = state.get("recent_requests")
    if not isinstance(recent, list) or len(recent) > 10000:
        raise TranslationError("Некорректная история минутной квоты.")
    for item in recent:
        if (not isinstance(item, dict) or set(item) != {"time", "tokens"}
                or type(item["time"]) not in (int, float) or not math.isfinite(item["time"])
                or item["time"] > time.time() + 60 or item["time"] < 0
                or type(item["tokens"]) is not int or item["tokens"] < 0):
            raise TranslationError("Некорректная запись минутной квоты в журнале.")


def _model_id(value):
    return isinstance(value, str) and 0 < len(value) <= 200 and all(c.isprintable() and not c.isspace() for c in value)


def _catalog(value):
    return isinstance(value, (list, tuple)) and len(value) <= 10000 and all(_model_id(item) for item in value) and len(set(value)) == len(value)


def translate_file(input_path: str | Path, *, output_dir: str | Path, options: TranslationOptions,
                   provider: TranslationProvider,
                   allow_network: bool = False, glossary=None, limit_units: int | None = None,
                   resume: bool = False, overwrite: bool = False,
                   progress: Progress | None = None) -> TranslationResult:
    """Write a resumable translation. Network authorization must be explicit.

    The caller MUST inject a provider and owns its resource lifecycle. Core code
    never selects/imports an SDK or constructs an adapter. The allow_network flag
    remains required, including for offline providers, to keep opt-in explicit.
    """
    if not allow_network:
        raise TranslationError("Перевод требует явного allow_network=True. Для локального плана используйте plan_translation().")
    plan = plan_translation(input_path, options, provider=provider.identity,
                            glossary=glossary, limit_units=limit_units)
    progress = progress or (lambda _: None)
    source = Path(input_path)
    folder = Path(output_dir)
    stem = source.stem + "." + options.target_language.lower()
    markdown_path, text_path = folder / (stem + ".md"), folder / (stem + ".txt")
    checkpoint = folder / (stem + ".translation.json")
    check_output_paths([markdown_path, text_path], source, overwrite=overwrite)
    if checkpoint.is_symlink():
        raise TranslationError("Журнал перевода не может быть символической ссылкой.")
    if checkpoint.exists() and not resume:
        raise TranslationError("Журнал уже существует. Для продолжения укажите --resume; для нового перевода выберите другую папку.")
    if resume and not checkpoint.is_file():
        raise TranslationError("Журнал для --resume не найден.")

    def check_source():
        if digest(source.read_bytes()) != plan.manifest["source_sha256"]:
            raise TranslationError("Исходник изменился во время перевода. Экспорт остановлен; журнал сохраняет прежний снимок.")

    with checkpoint_lock(checkpoint):
        # Recheck after locking; a competing run could finish before lock acquisition.
        if resume:
            state = load_checkpoint(checkpoint)
            _validate_state(state, plan)
        else:
            if checkpoint.exists():
                raise TranslationError("Журнал появился в другом запуске; используйте --resume.")
            state = {"version": 3, "fingerprint": plan.fingerprint, "manifest": plan.manifest,
                     "translations": {}, "segment_models": {}, "catalog": [],
                     "calls_total": 0, "recent_requests": []}
        controller = RequestController(options, state, checkpoint, progress)
        cached = state["translations"]
        status = "complete"
        pause_reason = None
        try:
            if any(segment.id not in cached for segment in plan.segments):
                provider.prepare(options)
            if not resume:
                save_checkpoint(checkpoint, state)
            if any(segment.id not in cached for segment in plan.segments):
                pending = [tuple(segment for segment in batch if segment.id not in cached) for batch in plan.batches]
                pending = [batch for batch in pending if batch]
                while pending:
                    check_source()
                    batch = pending.pop(0)
                    request = make_request(plan, batch, options)
                    try:
                        response = provider.translate(request, executor=controller)
                    except BatchTooLarge:
                        pending[0:0] = split_batch(plan.document, batch)
                        continue
                    if isinstance(response, TranslationResponse):
                        model, catalog = response.model, response.catalog
                        response = response.translations
                    else:
                        model, catalog = options.model, ()
                    if not _model_id(model) or not _catalog(catalog):
                        raise TranslationError("Адаптер вернул некорректные сведения о модели/каталоге.")
                    translated, _ = validate_translations(response, batch)
                    cached.update(translated)
                    state["segment_models"].update({identifier: model for identifier in translated})
                    if catalog:
                        state["catalog"] = list(catalog)
                    save_checkpoint(checkpoint, state)
                    progress(f"Сохранён прогресс: {len(cached)}/{len(plan.segments)} сегментов.")
        except TranslationPaused as error:
            status = "paused"
            pause_reason = str(error)
        warnings = list(plan.document.warnings)
        for segment in plan.segments:
            if segment.id in cached:
                _, checks = validate_text(segment.text, cached[segment.id], segment.id)
                warnings.extend(checks)
        if status == "complete":
            check_source()
            assembled = {}
            for segment in plan.segments:
                assembled[segment.unit_id] = assembled.get(segment.unit_id, "") + cached[segment.id] + segment.separator
            markdown, text = render_document(plan.document, assembled)
            check_output_paths([markdown_path, text_path], source, overwrite=overwrite)
            write_outputs({markdown_path: markdown, text_path: text}, overwrite=overwrite)
        return TranslationResult(status, len(cached), len(plan.segments), controller.calls,
                                 tuple(dict.fromkeys(warnings)), str(checkpoint.resolve()),
                                 str(markdown_path.resolve()) if status == "complete" else None,
                                 str(text_path.resolve()) if status == "complete" else None, pause_reason)
