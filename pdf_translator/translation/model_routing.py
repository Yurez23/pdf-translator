"""Quota/service-aware routing; injected catalog, sessions and availability storage."""

import math
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..telemetry import emit, fields
from .catalog import ModelCandidate, rank_models
from .models import ProviderError, QuotaError, TranslationError, TranslationPaused, TranslationSegment
from .ports import RequestExecutor, TranslationResponse, TranslationSession
from .validation import validate_translations
from .response_recovery import translate_checked

_TRANSIENT_MODEL_STATUSES = frozenset((503, 504))


class CatalogReader(Protocol):
    def page(self, token: str | None, *, executor: RequestExecutor) -> tuple[tuple, str | None]: ...


class AvailabilityStore(Protocol):
    def blocked(self, namespace, model): ...
    def claim(self, namespace, model, *, seconds, ignore_cooldown=False): ...
    def release(self, namespace, model, owner): ...
    def defer(self, namespace, model, *, reason, retry_at, basis): ...
    def remember_alias(self, namespace, alias, canonical): ...
    def succeeded(self, namespace, model, *, started_at): ...
    def admit(self, namespace, model, *, seconds, ignore_cooldown=False): ...


def cooldown_for(error: ProviderError, now: float) -> tuple[str, float, str]:
    delay = error.retry_after if isinstance(error.retry_after, (int, float)) else 0
    if delay < 0 or not math.isfinite(delay):
        delay = 0
    if not isinstance(error, QuotaError) and error.status_code in _TRANSIENT_MODEL_STATUSES:
        # A temporary service failure is not a missing model or a daily quota.
        return "unavailable", now + max(delay, 300), "server" if delay else "estimated"
    kind = error.kind if isinstance(error, QuotaError) else "unavailable"
    if kind == "daily":
        if delay > 0:
            return kind, now + delay, "server"
        try:
            zone = ZoneInfo("America/Los_Angeles")
            tomorrow = datetime.fromtimestamp(now, zone).date() + timedelta(days=1)
            reset = datetime.combine(tomorrow, datetime.min.time(), zone).timestamp()
            return kind, reset + 5, "pacific-midnight"
        except ZoneInfoNotFoundError:
            # Windows without IANA tzdata: conservative, never guess DST offsets.
            return kind, now + 25 * 3600, "conservative-25h-no-tzdata"
    defaults = {"minute": 60, "zero": 6 * 3600, "unknown": 60, "unavailable": 3600}
    return kind, now + max(delay, defaults.get(kind, 60)), "server" if delay else "estimated"


class _RoutingExecutor:
    """Model quota errors reach the router before the normal retry machinery."""

    def __init__(self, executor):
        self.executor = executor

    def call(self, operation, **kwargs):
        def one_attempt():
            try:
                return operation()
            except QuotaError as error:
                raise QuotaError(kind=error.kind, scope=error.scope, model=error.model,
                                 retry_after=error.retry_after, retryable=False, message=str(error)) from None
        return self.executor.call(one_attempt, **kwargs)


class AutoRoutingSession:
    def __init__(self, options, *, reader: CatalogReader,
                 session_factory: Callable[[ModelCandidate], TranslationSession],
                 cache: AvailabilityStore, namespace, ignore_cache=False, progress=None, clock=time.time):
        self.options = options
        self.reader, self.session_factory = reader, session_factory
        self.cache, self.namespace = cache, namespace
        self.ignore_cache, self.clock = ignore_cache, clock
        self.progress = progress or (lambda _: None)
        self.catalog = None

    def _load_catalog(self, executor):
        models, token, seen = [], None, set()
        for _ in range(100):
            with fields(request_kind="catalog", model="auto"):
                page, next_token = self.reader.page(token, executor=executor)
            models.extend(page)
            if not next_token:
                break
            if next_token in seen:
                raise TranslationError("Каталог моделей повторяет страницу; автоматический выбор остановлен.")
            seen.add(next_token)
            token = next_token
        else:
            raise TranslationError("Каталог моделей превысил безопасный предел страниц.")
        self.catalog = rank_models(models)
        self.progress(f"Каталог: {len(self.catalog)} подходящих текстовых моделей.")
        emit("routing.catalog", catalog_size=len(self.catalog))
        if not self.catalog:
            raise TranslationError("В каталоге нет совместимых текстовых моделей Pro/Flash/Flash Lite.")

    def translate(self, request, *, executor):
        if self.catalog is None:
            # Catalog-level failures are not a model-specific fallback signal.
            self._load_catalog(executor)
        names = tuple(candidate.name for candidate in self.catalog)
        deadlines = []
        project_block = None if self.ignore_cache else self.cache.blocked(self.namespace, "*")
        if project_block:
            emit("routing.paused", "WARNING", reason="project")
            until = datetime.fromtimestamp(project_block.retry_at, timezone.utc).isoformat(timespec="seconds")
            raise TranslationPaused(f"Общее ограничение проекта Gemini до {until}. Прогресс сохранён.")
        for candidate in self.catalog:
            blocked = None if self.ignore_cache else self.cache.blocked(self.namespace, candidate.name)
            if blocked:
                deadlines.append(blocked.retry_at)
                self.progress(f"Кэш: пропущена {candidate.name} ({blocked.reason}).")
                emit("routing.skipped", model=candidate.name, reason=blocked.reason)
                if self.cache.blocked(self.namespace, "*"):
                    break
                continue
            if candidate.output_tokens < self.options.max_output_tokens:
                self.progress(f"Пропущена {candidate.name}: недостаточный предел ответа.")
                emit("routing.skipped", model=candidate.name, reason="output_limit")
                continue
            started = self.clock()
            owner = self.cache.claim(self.namespace, candidate.name,
                                     seconds=60 + self.options.timeout_seconds * 3 * (self.options.max_retries + 1),
                                     ignore_cooldown=self.ignore_cache)
            if owner is None:
                emit("routing.skipped", model=candidate.name, reason="busy")
                # Another process may have just recorded a restriction or claimed it.
                blocked = self.cache.blocked(self.namespace, candidate.name)
                deadlines.append(blocked.retry_at if blocked else started + 60)
                continue
            try:
                self.progress(f"Выбрана модель: {candidate.name}.")
                emit("routing.selected", model=candidate.name)
                with fields(model=candidate.name):
                    response = translate_checked(self.session_factory(candidate), request,
                                                 executor=_RoutingExecutor(executor), options=self.options)
                # Do not clear negative state for an invalid/partial response.
                segments = tuple(TranslationSegment(item.id, item.id, item.text) for item in request.items)
                translated, _ = validate_translations(response.translations, segments)
                actual = response.model
                self.cache.remember_alias(self.namespace, candidate.name, actual)
                self.cache.succeeded(self.namespace, actual, started_at=started)
                emit("routing.succeeded", model=candidate.name, actual_model=actual)
                return TranslationResponse(translated, actual, names)
            except QuotaError as error:
                reason, retry_at, basis = cooldown_for(error, self.clock())
                if error.scope == "project":
                    emit("routing.paused", "WARNING", reason="project", status_code=429)
                    self.cache.defer(self.namespace, "*", reason=reason, retry_at=retry_at, basis=basis)
                    raise TranslationPaused("Общее ограничение проекта Gemini. Прогресс сохранён; повторите запуск позже.") from None
                if error.model:
                    self.cache.remember_alias(self.namespace, candidate.name, error.model)
                self.cache.defer(self.namespace, candidate.name, reason=reason, retry_at=retry_at, basis=basis)
                deadlines.append(retry_at)
                self.progress(f"Fallback: {candidate.name}, ограничение {reason}.")
                emit("routing.fallback", "WARNING", model=candidate.name, reason=reason,
                     status_code=429, delay_seconds=max(0, retry_at - self.clock()))
            except ProviderError as error:
                if error.status_code not in _TRANSIENT_MODEL_STATUSES | {404}:
                    # Credentials, bad requests, filters and exhausted transport
                    # retries never trigger blind model hopping.
                    raise
                # The shared executor has already applied its normal retry policy.
                reason, retry_at, basis = cooldown_for(error, self.clock())
                self.cache.defer(self.namespace, candidate.name, reason=reason, retry_at=retry_at, basis=basis)
                deadlines.append(retry_at)
                self.progress(f"Fallback: {candidate.name} недоступна (HTTP {error.status_code}).")
                emit("routing.fallback", "WARNING", model=candidate.name, reason=reason,
                     status_code=error.status_code, delay_seconds=max(0, retry_at - self.clock()))
            finally:
                self.cache.release(self.namespace, candidate.name, owner)
        if deadlines:
            emit("routing.paused", "WARNING", reason="unavailable")
            earliest = datetime.fromtimestamp(min(deadlines), timezone.utc).isoformat(timespec="seconds")
            raise TranslationPaused(f"Доступные модели временно закончились. Ближайшая оценка повторной попытки: {earliest}.")
        raise TranslationError("Ни одна модель не подходит под max_output_tokens. Уменьшите предел ответа.")
