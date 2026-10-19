"""Single-attempt Google SDK gateway; settings and model attribution are call-local."""

import json
import math
import os
import re
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..telemetry import emit
from .models import ModelLimits, ProviderError, QuotaError, ResponseValidationError, TranslationError
from .gemini_format import RESPONSE_SCHEMA


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    supported_actions: tuple[str, ...]
    input_token_limit: int | None
    output_token_limit: int | None


def close_gateways(gateways):
    """Release every owned gateway even when one close fails; preserve the first error."""
    pending = tuple(gateways)
    gateways.clear()
    failure = None
    for gateway in pending:
        try:
            gateway.close()
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


class GeminiGateway:
    def __init__(self, timeout_seconds):
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise TranslationError("Не задан GEMINI_API_KEY. Сохраните ключ локально; не передавайте его в аргументах или чат.")
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise TranslationError("Установите официальный SDK: python -m pip install -e '.[translation]'.") from None
        self._types = types
        self._client = genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(
            base_url="https://generativelanguage.googleapis.com",
            timeout=round(timeout_seconds * 1000), retry_options=types.HttpRetryOptions(attempts=1)))

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def list_models_page(self, token: str | None = None):
        pager = self._call(lambda: self._client.models.list(config={"page_size": 100, "page_token": token}))
        # Never iterate a Pager: that would fetch later pages outside the budget.
        return tuple(CatalogEntry(m.name or "", tuple(m.supported_actions or ()), m.input_token_limit, m.output_token_limit)
                     for m in pager.page), pager.config.get("page_token")

    def _call(self, operation):
        with self._errors():
            return operation()

    @contextmanager
    def _errors(self):
        try:
            yield
        except Exception as exc:
            from google.genai import errors
            import httpx
            if isinstance(exc, errors.APIError):
                code = exc.code if type(exc.code) is int else 0
                # Details may contain request data. Inspect only in memory;
                # never log the exception, URL, response body, or credentials.
                details = json.dumps(getattr(exc, "details", None), default=str).casefold()
                daily = any(word in details for word in ("perday", "per_day", "per day", "daily"))
                delay = 0.0
                match = re.search(r'"retrydelay"\s*:\s*"([\d.]+)s"', details)
                if match:
                    try:
                        delay = float(match[1])
                    except ValueError:
                        pass
                response = getattr(exc, "response", None)
                header = response.headers.get("Retry-After") if response is not None else None
                if header:
                    try:
                        header_delay = float(header)
                    except ValueError:
                        try:
                            until = parsedate_to_datetime(header)
                            header_delay = (until - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError, OverflowError):
                            header_delay = 0
                    if math.isfinite(header_delay):
                        delay = max(delay, header_delay)
                if code == 429:
                    kind, scope, quota_model = "unknown", "unknown", ""
                    payload = getattr(exc, "details", None)
                    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                        payload = payload["error"]
                    if isinstance(payload, dict):
                        payload = payload.get("details", [])
                    for detail in payload if isinstance(payload, list) else []:
                        for violation in detail.get("violations", []) if isinstance(detail, dict) else []:
                            if not isinstance(violation, dict):
                                continue
                            quota_id = str(violation.get("quotaId", "")).casefold()
                            dimensions = violation.get("quotaDimensions", {})
                            candidate = dimensions.get("model", "") if isinstance(dimensions, dict) else ""
                            if isinstance(candidate, str) and re.fullmatch(r"gemini-[a-z0-9.-]{1,110}", candidate):
                                quota_model = candidate
                            if candidate or "permodel" in quota_id:
                                if scope != "project":
                                    scope = "model"
                            elif "perproject" in quota_id:
                                scope = "project"
                            if "perday" in quota_id and kind != "zero":
                                kind = "daily"
                            elif "perminute" in quota_id and kind not in ("daily", "zero"):
                                kind = "minute"
                            # Never store raw violation payloads or descriptions.
                            value = violation.get("quotaValue")
                            if str(value) == "0":
                                kind = "zero"
                    if daily and kind != "zero":
                        kind = "daily"
                    raise QuotaError(kind=kind, scope=scope, model=quota_model,
                                     retry_after=delay, retryable=not daily and delay <= 60,
                                     message="Квота Gemini исчерпана или запросы идут слишком часто. Прогресс сохранён.") from None
                messages = {
                    400: "Gemini отклонил параметры запроса. Проверьте модель и её поддержку structured output.",
                    401: "Gemini не принял API-ключ.",
                    403: "Нет доступа к Gemini: проверьте ключ, проект, регион и доступность Free Tier.",
                    404: "Модель Gemini не найдена или недоступна этому проекту. Автозамены модели нет.",
                }
                raise ProviderError(messages.get(code, f"Ошибка Gemini HTTP {code}; данные ошибки скрыты."),
                                    retryable=500 <= code <= 599 and delay <= 60, retry_after=delay,
                                    status_code=code) from None
            if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
                raise ProviderError("Сетевая ошибка или тайм-аут Gemini; прогресс сохранён.", retryable=True,
                                    error_code="network_timeout" if isinstance(exc, httpx.TimeoutException) else "network_transport") from None
            raise ProviderError("Неожиданная ошибка SDK Gemini; чувствительные подробности скрыты.", error_code="sdk") from None

    def model_limits(self, model: str) -> ModelLimits:
        model = self._call(lambda: self._client.models.get(model=model))
        values = (model.input_token_limit, model.output_token_limit)
        if not all(type(value) is int and value > 0 for value in values):
            raise TranslationError("Gemini не сообщил ограничения модели; отправка текста остановлена.")
        return ModelLimits(*values)

    def count_tokens(self, model: str, prompt: str) -> int:
        result = self._call(lambda: self._client.models.count_tokens(model=model, contents=prompt))
        if type(result.total_tokens) is not int or result.total_tokens < 1:
            raise TranslationError("Gemini не вернул корректное количество входных токенов.")
        return result.total_tokens

    def generate(self, model: str, prompt: str, max_output_tokens: int, *, response_schema=None) -> GenerationResult:
        result = self._call(lambda: self._client.models.generate_content(
            model=model, contents=prompt,
            config=self._generation_config(max_output_tokens, response_schema)))
        return self._generation_result(result, model)

    def _generation_config(self, max_output_tokens, response_schema=None):
        return self._types.GenerateContentConfig(
                response_mime_type="application/json", response_json_schema=response_schema if response_schema is not None else RESPONSE_SCHEMA,
                max_output_tokens=max_output_tokens, temperature=0.1, candidate_count=1,
                # No tools, automatic function calls, search or file upload.
                automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
            )

    def _generation_result(self, result, model):
        usage = getattr(result, "usage_metadata", None)
        if usage is not None:
            emit("provider.usage", model=model,
                 input_tokens=getattr(usage, "prompt_token_count", None),
                 output_tokens=getattr(usage, "candidates_token_count", None),
                 thinking_tokens=getattr(usage, "thoughts_token_count", None),
                 total_tokens=getattr(usage, "total_token_count", None),
                 cached_tokens=getattr(usage, "cached_content_token_count", None))
        if not result.candidates or len(result.candidates) != 1:
            raise TranslationError("Gemini не вернул единственный перевод; возможна блокировка содержимого.")
        candidate = result.candidates[0]
        reason = getattr(candidate.finish_reason, "value", candidate.finish_reason)
        emit("provider.finish_reason", finish_reason=reason if reason in ("STOP", "MAX_TOKENS", "SAFETY", "RECITATION") else "OTHER")
        if reason != "STOP":
            raise TranslationError("Gemini не завершил ответ нормально (обрезка или фильтр). Пакет не сохранён; уменьшите размер пакета.")
        if not candidate.content or not candidate.content.parts:
            raise ResponseValidationError("invalid_text")
        parts = [part.text for part in candidate.content.parts if part.text and not part.thought]
        if not parts:
            raise ResponseValidationError("invalid_text")
        actual_model = model
        model_version = getattr(result, "model_version", None)
        if isinstance(model_version, str) and re.fullmatch(r"gemini-[a-z0-9.-]{1,110}", model_version):
            actual_model = model_version
        return GenerationResult("".join(parts), actual_model)


class AsyncGeminiGateway(GeminiGateway):
    """Native SDK aio calls; no thread pool and no hidden SDK retries."""

    async def _acall(self, operation):
        with self._errors():
            return await operation()

    async def aclose(self):
        if self._client is not None:
            try:
                await self._client.aio.aclose()
            finally:
                self.close()

    async def list_models_page(self, token=None):
        pager = await self._acall(lambda: self._client.aio.models.list(config={"page_size": 100, "page_token": token}))
        return tuple(CatalogEntry(m.name or "", tuple(m.supported_actions or ()), m.input_token_limit, m.output_token_limit)
                     for m in pager.page), pager.config.get("page_token")

    async def model_limits(self, model):
        result = await self._acall(lambda: self._client.aio.models.get(model=model))
        values = (result.input_token_limit, result.output_token_limit)
        if not all(type(value) is int and value > 0 for value in values):
            raise TranslationError("Gemini не сообщил ограничения модели; отправка текста остановлена.")
        return ModelLimits(*values)

    async def count_tokens(self, model, prompt):
        result = await self._acall(lambda: self._client.aio.models.count_tokens(model=model, contents=prompt))
        if type(result.total_tokens) is not int or result.total_tokens < 1:
            raise TranslationError("Gemini не вернул корректное количество входных токенов.")
        return result.total_tokens

    async def generate(self, model, prompt, max_output_tokens, *, response_schema=None):
        result = await self._acall(lambda: self._client.aio.models.generate_content(
            model=model, contents=prompt, config=self._generation_config(max_output_tokens, response_schema)))
        return self._generation_result(result, model)


async def aclose_gateways(gateways):
    pending = tuple(gateways)
    gateways.clear()
    failure = None
    for gateway in pending:
        try:
            await gateway.aclose()
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise failure
