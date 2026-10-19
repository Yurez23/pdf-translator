"""Gemini composition adapter: dynamic catalog + persistent quota-aware fallback."""

import os
import re
import time
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .availability import AvailabilityCache, default_database_path
from .catalog import POLICY_VERSION, rank_models
from .gemini import GeminiProvider
from .gemini_format import provider_identity
from .models import ModelLimits, ProviderError, QuotaError, TranslationError, TranslationPaused, TranslationSegment
from .planning import digest, validate_translations
from .ports import ProviderIdentity, TranslationResponse


def cache_namespace(project_id: str | None, key: str) -> str:
    """No raw key/project identifier is persisted. Explicit project shares keys."""
    if project_id:
        if not re.fullmatch(r"[a-z][a-z0-9-]{3,62}|\d{6,24}", project_id):
            raise TranslationError("Некорректный --project-id (ID или номер проекта Google).")
        return digest("gemini-project:" + project_id)
    if not key:
        raise TranslationError("Не задан GEMINI_API_KEY.")
    return digest("gemini-credential-namespace:" + key)


def cooldown_for(error: ProviderError, now: float) -> tuple[str, float, str]:
    delay = error.retry_after if isinstance(error.retry_after, (int, float)) else 0
    if delay < 0 or not math.isfinite(delay):
        delay = 0
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
                error.retryable = False
                raise
        return self.executor.call(one_attempt, **kwargs)


class GeminiAutoProvider:
    def __init__(self, *, database_path=None, project_id=None, reset_cache=False,
                 ignore_cache=False, progress=None, clock=time.time):
        self.identity = ProviderIdentity("gemini-auto", digest({"policy": POLICY_VERSION,
                                                               "adapter": provider_identity().revision}))
        self.database_path = database_path if database_path is not None else default_database_path()
        self.project_id, self.reset_cache, self.ignore_cache = project_id, reset_cache, ignore_cache
        self.progress, self.clock = progress or (lambda _: None), clock
        self.adapter = GeminiProvider()
        self.catalog = None

    def prepare(self, options):
        if options.model != "auto":
            raise TranslationError("Автоматический адаптер требует model='auto'.")
        self.options = options
        self.namespace = cache_namespace(self.project_id or os.environ.get("GEMINI_PROJECT_ID"),
                                         os.environ.get("GEMINI_API_KEY", "").strip())
        self.adapter.connect(options)
        self.cache = AvailabilityCache(self.database_path, clock=self.clock)
        if self.reset_cache:
            self.cache.reset(self.namespace)
        self.catalog = None

    def close(self):
        self.adapter.close()

    def _load_catalog(self, executor):
        models, token, seen = [], None, set()
        for _ in range(100):
            page, next_token = executor.call(lambda: self.adapter.list_models_page(token), label="каталог моделей")
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
            until = datetime.fromtimestamp(project_block.retry_at, timezone.utc).isoformat(timespec="seconds")
            raise TranslationPaused(f"Общее ограничение проекта Gemini до {until}. Прогресс сохранён.")
        for candidate in self.catalog:
            blocked = None if self.ignore_cache else self.cache.blocked(self.namespace, candidate.name)
            if blocked:
                deadlines.append(blocked.retry_at)
                self.progress(f"Кэш: пропущена {candidate.name} ({blocked.reason}).")
                if self.cache.blocked(self.namespace, "*"):
                    break
                continue
            if candidate.output_tokens < self.options.max_output_tokens:
                self.progress(f"Пропущена {candidate.name}: недостаточный предел ответа.")
                continue
            started = self.clock()
            owner = self.cache.claim(self.namespace, candidate.name,
                                     seconds=60 + self.options.timeout_seconds * 3 * (self.options.max_retries + 1),
                                     ignore_cooldown=self.ignore_cache)
            if owner is None:
                # Another process may have just recorded a restriction or claimed it.
                blocked = self.cache.blocked(self.namespace, candidate.name)
                deadlines.append(blocked.retry_at if blocked else started + 60)
                continue
            try:
                self.adapter.options = replace(self.options, model=candidate.name)
                self.adapter._limits = ModelLimits(candidate.input_tokens, candidate.output_tokens)
                self.progress(f"Выбрана модель: {candidate.name}.")
                response = self.adapter.translate(request, executor=_RoutingExecutor(executor))
                # Do not clear negative state for an invalid/partial response.
                segments = tuple(TranslationSegment(item.id, item.id, item.text) for item in request.items)
                translated, _ = validate_translations(response.translations, segments)
                actual = response.model
                self.cache.remember_alias(self.namespace, candidate.name, actual)
                self.cache.succeeded(self.namespace, actual, started_at=started)
                return TranslationResponse(translated, actual, names)
            except QuotaError as error:
                reason, retry_at, basis = cooldown_for(error, self.clock())
                if error.scope == "project":
                    self.cache.defer(self.namespace, "*", reason=reason, retry_at=retry_at, basis=basis)
                    raise TranslationPaused("Общее ограничение проекта Gemini. Прогресс сохранён; повторите запуск позже.") from None
                if error.model:
                    self.cache.remember_alias(self.namespace, candidate.name, error.model)
                self.cache.defer(self.namespace, candidate.name, reason=reason, retry_at=retry_at, basis=basis)
                deadlines.append(retry_at)
                self.progress(f"Fallback: {candidate.name}, ограничение {reason}.")
            except ProviderError as error:
                if error.status_code != 404:
                    # Credentials, bad requests, filters and exhausted transport
                    # retries never trigger blind model hopping.
                    raise
                reason, retry_at, basis = cooldown_for(error, self.clock())
                self.cache.defer(self.namespace, candidate.name, reason=reason, retry_at=retry_at, basis=basis)
                deadlines.append(retry_at)
                self.progress(f"Fallback: {candidate.name} недоступна (404).")
            finally:
                self.cache.release(self.namespace, candidate.name, owner)
        if deadlines:
            earliest = datetime.fromtimestamp(min(deadlines), timezone.utc).isoformat(timespec="seconds")
            raise TranslationPaused(f"Доступные модели временно закончились. Ближайшая оценка повторной попытки: {earliest}.")
        raise TranslationError("Ни одна модель не подходит под max_output_tokens. Уменьшите предел ответа.")
