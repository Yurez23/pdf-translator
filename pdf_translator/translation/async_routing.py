"""Cooperative model fallback and recovery probes, independent of the SDK."""

import asyncio
from datetime import datetime, timezone

from ..telemetry import emit, fields, span
from .model_routing import AvailabilityStore, cooldown_for, _TRANSIENT_MODEL_STATUSES
from .models import ProviderError, QuotaError, TranslationError, TranslationPaused, TranslationSegment
from .ports import TranslationResponse
from .validation import validate_translations
from .response_recovery import translate_checked_async


class _ModelBlocked(Exception):
    pass


class _RoutingExecutor:
    def __init__(self, executor, cache, namespace, model, ignore_cache):
        self.executor, self.cache, self.namespace, self.model = executor, cache, namespace, model
        self.ignore_cache = ignore_cache

    async def call(self, operation, **kwargs):
        def check():
            # Recheck after minute waits/backoff: another worker may have learned
            # about a quota while this attempt was waiting for admission.
            if not self.ignore_cache:
                if self.cache.blocked(self.namespace, "*"):
                    raise TranslationPaused("Общее ограничение проекта Gemini. Прогресс сохранён.")
                if self.cache.blocked(self.namespace, self.model):
                    raise _ModelBlocked()
        async def one_attempt():
            try:
                return await operation()
            except QuotaError as error:
                raise QuotaError(kind=error.kind, scope=error.scope, model=error.model,
                                 retry_after=error.retry_after, retryable=False, message=str(error)) from None
        return await self.executor.call(one_attempt, before_attempt=check, **kwargs)


class AsyncAutoRoutingSession:
    def __init__(self, options, *, gateway, catalog, session_factory, cache: AvailabilityStore, namespace,
                 ignore_cache, progress, clock):
        self.options, self.gateway, self.catalog = options, gateway, catalog
        self.session_factory, self.cache, self.namespace = session_factory, cache, namespace
        self.ignore_cache, self.progress, self.clock = ignore_cache, progress, clock

    def _project_check(self):
        if not self.ignore_cache and self.cache.blocked(self.namespace, "*"):
            emit("routing.paused", "WARNING", reason="project")
            raise TranslationPaused("Общее ограничение проекта Gemini. Прогресс сохранён; повторите запуск позже.")

    async def translate(self, request, *, executor):
        catalog = await self.catalog.get(self.gateway, executor)
        names, deadlines = tuple(c.name for c in catalog), []
        for candidate in catalog:
            self._project_check()
            if candidate.output_tokens < self.options.max_output_tokens:
                emit("routing.skipped", model=candidate.name, reason="output_limit")
                continue
            while True:
                self._project_check()
                admission, owner = self.cache.admit(
                    self.namespace, candidate.name,
                    seconds=60 + self.options.timeout_seconds * 3 * (self.options.max_retries + 1),
                    ignore_cooldown=self.ignore_cache)
                if admission != "busy":
                    break
                # An occupied recovery probe is not evidence that a weaker model
                # should be used. Wait for its outcome, yielding to other workers.
                with span("routing.probe_wait", model=candidate.name, reason="busy", delay_seconds=0.1):
                    await asyncio.sleep(0.1)
            if admission == "blocked":
                blocked = self.cache.blocked(self.namespace, candidate.name)
                if blocked:
                    deadlines.append(blocked.retry_at)
                    self.progress(f"Кэш: пропущена {candidate.name} ({blocked.reason}).")
                    emit("routing.skipped", model=candidate.name, reason=blocked.reason)
                continue
            started = self.clock()
            try:
                self.progress(f"Выбрана модель: {candidate.name}.")
                emit("routing.selected", model=candidate.name)
                with fields(model=candidate.name):
                    response = await translate_checked_async(self.session_factory(candidate), request,
                        executor=_RoutingExecutor(executor, self.cache, self.namespace, candidate.name, self.ignore_cache), options=self.options)
                segments = tuple(TranslationSegment(item.id, item.id, item.text) for item in request.items)
                translated, _ = validate_translations(response.translations, segments)
                self.cache.remember_alias(self.namespace, candidate.name, response.model)
                self.cache.succeeded(self.namespace, response.model, started_at=started)
                emit("routing.succeeded", model=candidate.name, actual_model=response.model)
                return TranslationResponse(translated, response.model, names)
            except _ModelBlocked:
                blocked = self.cache.blocked(self.namespace, candidate.name)
                if blocked:
                    deadlines.append(blocked.retry_at)
                    emit("routing.skipped", model=candidate.name, reason=blocked.reason)
            except ProviderError as error:
                if not isinstance(error, QuotaError) and error.status_code not in _TRANSIENT_MODEL_STATUSES | {404}:
                    raise
                reason, retry_at, basis = cooldown_for(error, self.clock())
                if isinstance(error, QuotaError) and error.scope == "project":
                    self.cache.defer(self.namespace, "*", reason=reason, retry_at=retry_at, basis=basis)
                    emit("routing.paused", "WARNING", reason="project", status_code=429)
                    raise TranslationPaused("Общее ограничение проекта Gemini. Прогресс сохранён; повторите запуск позже.") from None
                if isinstance(error, QuotaError) and error.model:
                    self.cache.remember_alias(self.namespace, candidate.name, error.model)
                self.cache.defer(self.namespace, candidate.name, reason=reason, retry_at=retry_at, basis=basis)
                deadlines.append(retry_at)
                self.progress(f"Fallback: {candidate.name}, HTTP {error.status_code}, {reason}.")
                emit("routing.fallback", "WARNING", model=candidate.name, reason=reason,
                     status_code=error.status_code, delay_seconds=max(0, retry_at - self.clock()))
            finally:
                if owner is not None:
                    self.cache.release(self.namespace, candidate.name, owner)
        if deadlines:
            emit("routing.paused", "WARNING", reason="unavailable")
            earliest = datetime.fromtimestamp(min(deadlines), timezone.utc).isoformat(timespec="seconds")
            raise TranslationPaused(f"Доступные модели временно закончились. Ближайшая оценка повторной попытки: {earliest}.")
        raise TranslationError("Ни одна модель не подходит под max_output_tokens. Уменьшите предел ответа.")
