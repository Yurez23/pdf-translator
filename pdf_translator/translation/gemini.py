"""Official Google GenAI adapter of the provider-independent translation port."""

import json
import math
import os
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .models import ModelLimits, ProviderError, QuotaError, TranslationError, TranslationOptions
from .gemini_format import RESPONSE_SCHEMA, decode_response, make_prompt, provider_identity
from .ports import BatchTooLarge, RequestExecutor, TranslationRequest, TranslationResponse


def validate_model(model: str) -> None:
    if not re.fullmatch(r"gemini-[a-z0-9][a-z0-9.-]{0,100}", model) or "latest" in model:
        raise TranslationError("Укажите явный ID модели Gemini, без models/ и псевдонима latest.")


class GeminiProvider:
    def __init__(self):
        self.identity = provider_identity()
        self._client = None
        self._limits = None
        self.model_version = ""

    def prepare(self, options: TranslationOptions) -> None:
        """Local preflight only. Cached re-export never needs to prepare an adapter."""
        validate_model(options.model)
        self.connect(options)

    def connect(self, options: TranslationOptions) -> None:
        """Local SDK setup shared with the auto adapter; never sends a request."""
        # Read only this variable: do not silently prefer GOOGLE_API_KEY or
        # inherit Vertex AI/project routing from unrelated environment settings.
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise TranslationError("Не задан GEMINI_API_KEY. Сохраните ключ локально; не передавайте его в аргументах или чат.")
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise TranslationError("Установите официальный SDK: python -m pip install -e '.[translation]'.") from None
        self.options = options
        self._types = types
        self.close()
        self._limits = None
        self.model_version = ""
        self._client = genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(
            base_url="https://generativelanguage.googleapis.com",
            timeout=round(options.timeout_seconds * 1000), retry_options=types.HttpRetryOptions(attempts=1)))

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def translate(self, request: TranslationRequest, *, executor: RequestExecutor) -> TranslationResponse:
        if self._client is None:
            raise TranslationError("Адаптер Gemini не подготовлен: вызовите prepare(options).")
        if self._limits is None:
            limits = executor.call(self.model_limits, label="ограничения выбранной модели")
            if (not isinstance(limits, ModelLimits) or type(limits.input_tokens) is not int
                    or type(limits.output_tokens) is not int or min(limits.input_tokens, limits.output_tokens) < 1):
                raise TranslationError("Некорректные ограничения модели Gemini.")
            self._limits = limits
        if self.options.max_output_tokens > self._limits.output_tokens:
            raise TranslationError("max_output_tokens больше ограничения выбранной модели.")
        prompt = make_prompt(request)
        count = executor.call(lambda: self.count_tokens(prompt), label="подсчёт токенов")
        if type(count) is not int or count < 1:
            raise TranslationError("Некорректное число токенов от Gemini.")
        if count + 512 > min(self.options.max_input_tokens, self._limits.input_tokens):
            raise BatchTooLarge("Пакет с контекстом/глоссарием не помещается во входные токены Gemini.")
        raw = executor.call(lambda: self.generate(prompt, self.options.max_output_tokens),
                            tokens=count + 512 + self.options.max_output_tokens,
                            label=f"перевод {len(request.items)} сегментов")
        return TranslationResponse(decode_response(raw), self.model_version or self.options.model)

    def list_models_page(self, token: str | None = None):
        pager = self._call(lambda: self._client.models.list(config={"page_size": 100, "page_token": token}))
        # Never iterate a Pager: that would fetch later pages outside the budget.
        return pager.page, pager.config.get("page_token")

    def _call(self, operation):
        try:
            return operation()
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
                                     retry_after=delay, retryable=not daily and delay <= 60) from None
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
                raise ProviderError("Сетевая ошибка или тайм-аут Gemini; прогресс сохранён.", retryable=True) from None
            raise ProviderError("Неожиданная ошибка SDK Gemini; чувствительные подробности скрыты.") from None

    def model_limits(self) -> ModelLimits:
        model = self._call(lambda: self._client.models.get(model=self.options.model))
        values = (model.input_token_limit, model.output_token_limit)
        if not all(type(value) is int and value > 0 for value in values):
            raise TranslationError("Gemini не сообщил ограничения модели; отправка текста остановлена.")
        return ModelLimits(*values)

    def count_tokens(self, prompt: str) -> int:
        result = self._call(lambda: self._client.models.count_tokens(model=self.options.model, contents=prompt))
        if type(result.total_tokens) is not int or result.total_tokens < 1:
            raise TranslationError("Gemini не вернул корректное количество входных токенов.")
        return result.total_tokens

    def generate(self, prompt: str, max_output_tokens: int) -> str:
        self.model_version = ""
        result = self._call(lambda: self._client.models.generate_content(
            model=self.options.model, contents=prompt,
            config=self._types.GenerateContentConfig(
                response_mime_type="application/json", response_json_schema=RESPONSE_SCHEMA,
                max_output_tokens=max_output_tokens, temperature=0.1, candidate_count=1,
                # No tools, automatic function calls, search or file upload.
                automatic_function_calling=self._types.AutomaticFunctionCallingConfig(disable=True),
            )))
        if not result.candidates or len(result.candidates) != 1:
            raise TranslationError("Gemini не вернул единственный перевод; возможна блокировка содержимого.")
        candidate = result.candidates[0]
        reason = getattr(candidate.finish_reason, "value", candidate.finish_reason)
        if reason != "STOP":
            raise TranslationError("Gemini не завершил ответ нормально (обрезка или фильтр). Пакет не сохранён; уменьшите размер пакета.")
        if not candidate.content or not candidate.content.parts:
            raise TranslationError("Gemini вернул пустой перевод.")
        parts = [part.text for part in candidate.content.parts if part.text and not part.thought]
        if not parts:
            raise TranslationError("В ответе Gemini нет текста перевода.")
        model_version = getattr(result, "model_version", None)
        if isinstance(model_version, str) and re.fullmatch(r"gemini-[a-z0-9.-]{1,110}", model_version):
            self.model_version = model_version
        return "".join(parts)
