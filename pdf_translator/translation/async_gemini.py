"""Async Gemini sessions and run-local catalog; workers never share SDK state."""

import asyncio
from dataclasses import replace

from ..telemetry import emit, fields
from .catalog import rank_models
from .gemini_format import count_prompt, decode_response, make_prompt, response_schema
from .models import ModelLimits, TranslationError
from .ports import BatchTooLarge, TranslationResponse


class AsyncGeminiSession:
    def __init__(self, options, gateway, *, limits=None, response_contract="strict"):
        self.options, self.gateway, self.limits = options, gateway, limits
        self.response_contract = response_contract

    async def translate(self, request, *, executor):
        model = self.options.model
        if self.limits is None:
            with fields(request_kind="metadata", model=model):
                self.limits = await executor.call(lambda: self.gateway.model_limits(model), label="ограничения выбранной модели")
            if (not isinstance(self.limits, ModelLimits) or type(self.limits.input_tokens) is not int
                    or type(self.limits.output_tokens) is not int or min(self.limits.input_tokens, self.limits.output_tokens) < 1):
                raise TranslationError("Некорректные ограничения модели Gemini.")
        if self.options.max_output_tokens > self.limits.output_tokens:
            raise TranslationError("max_output_tokens больше ограничения выбранной модели.")
        prompt = make_prompt(request, self.response_contract)
        schema = response_schema(request, self.response_contract)
        with fields(request_kind="count_tokens", model=model):
            count = await executor.call(lambda: self.gateway.count_tokens(model, count_prompt(prompt, schema, self.response_contract)), label="подсчёт токенов")
        if type(count) is not int or count < 1:
            raise TranslationError("Некорректное число токенов от Gemini.")
        if count + 512 > min(self.options.max_input_tokens, self.limits.input_tokens):
            raise BatchTooLarge("Пакет с контекстом/глоссарием не помещается во входные токены Gemini.")
        emit("provider.input_count", model=model, input_tokens=count)
        with fields(request_kind="generation", model=model):
            result = await executor.call(lambda: self.gateway.generate(model, prompt, self.options.max_output_tokens, response_schema=schema),
                                         tokens=count + 512 + self.options.max_output_tokens,
                                         label=f"перевод {len(request.items)} сегментов")
        return TranslationResponse(decode_response(result.text, self.response_contract), result.model)


class AsyncCatalog:
    """Single-flight pagination per run; immutable ranked snapshot after loading."""

    def __init__(self, progress):
        self.progress = progress
        self._lock = asyncio.Lock()
        self._catalog = None

    async def get(self, gateway, executor):
        async with self._lock:
            if self._catalog is not None:
                return self._catalog
            models, token, seen = [], None, set()
            for _ in range(100):
                with fields(request_kind="catalog", model="auto"):
                    page, next_token = await executor.call(lambda: gateway.list_models_page(token), label="каталог моделей")
                models.extend(page)
                if not next_token:
                    break
                if next_token in seen:
                    raise TranslationError("Каталог моделей повторяет страницу; автоматический выбор остановлен.")
                seen.add(next_token)
                token = next_token
            else:
                raise TranslationError("Каталог моделей превысил безопасный предел страниц.")
            catalog = tuple(rank_models(models))
            if not catalog:
                raise TranslationError("В каталоге нет совместимых текстовых моделей Pro/Flash/Flash Lite.")
            self._catalog = catalog
            self.progress(f"Каталог: {len(catalog)} подходящих текстовых моделей.")
            emit("routing.catalog", catalog_size=len(catalog))
            return catalog


class AsyncGeminiRun:
    """Local session factory; SDK resources belong to the calling provider."""

    def __init__(self, options, gateway_factory, gateways, *, cache=None, namespace=None,
                 ignore_cache=False, progress=None, clock=None, response_contract="strict"):
        self.options, self.gateway_factory, self.gateways = options, gateway_factory, gateways
        self.cache, self.namespace, self.ignore_cache = cache, namespace, ignore_cache
        self.progress = progress or (lambda _: None)
        self.clock = clock
        self.catalog = AsyncCatalog(self.progress)
        self.response_contract = response_contract

    def new_session(self):
        gateway = self.gateway_factory(self.options.timeout_seconds)
        self.gateways.append(gateway)
        if self.cache is None:
            return AsyncGeminiSession(self.options, gateway, response_contract=self.response_contract)
        from .async_routing import AsyncAutoRoutingSession
        def session_factory(candidate):
            return AsyncGeminiSession(replace(self.options, model=candidate.name), gateway,
                                      limits=ModelLimits(candidate.input_tokens, candidate.output_tokens), response_contract=self.response_contract)
        return AsyncAutoRoutingSession(self.options, gateway=gateway, catalog=self.catalog,
                                       session_factory=session_factory, cache=self.cache, namespace=self.namespace,
                                       ignore_cache=self.ignore_cache, progress=self.progress, clock=self.clock)
