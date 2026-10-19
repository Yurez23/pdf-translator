"""Gemini provider factory and run-bound translation session, independent of SDK state."""

import re
from typing import Protocol

from ..telemetry import emit, fields
from .models import ModelLimits, TranslationError, TranslationOptions
from .gemini_format import count_prompt, decode_response, make_prompt, provider_identity, response_schema
from .gemini_gateway import GeminiGateway, AsyncGeminiGateway, GenerationResult, close_gateways, aclose_gateways
from .ports import BatchTooLarge, RequestExecutor, TranslationRequest, TranslationResponse


def validate_model(model: str) -> None:
    if not re.fullmatch(r"gemini-[a-z0-9][a-z0-9.-]{0,100}", model) or "latest" in model:
        raise TranslationError("Укажите явный ID модели Gemini, без models/ и псевдонима latest.")


class ModelGateway(Protocol):
    def model_limits(self, model: str) -> ModelLimits: ...
    def count_tokens(self, model: str, prompt: str) -> int: ...
    def generate(self, model: str, prompt: str, max_output_tokens: int, *, response_schema: dict | None = None) -> GenerationResult: ...


class GeminiSession:
    def __init__(self, options: TranslationOptions, gateway: ModelGateway, *, limits: ModelLimits | None = None, response_contract="strict"):
        self._options, self.gateway, self._limits = options, gateway, limits
        self.response_contract = response_contract

    @property
    def options(self):
        return self._options

    def translate(self, request: TranslationRequest, *, executor: RequestExecutor) -> TranslationResponse:
        if self._limits is None:
            with fields(request_kind="metadata", model=self._options.model):
                limits = executor.call(lambda: self.gateway.model_limits(self._options.model), label="ограничения выбранной модели")
            if (not isinstance(limits, ModelLimits) or type(limits.input_tokens) is not int
                    or type(limits.output_tokens) is not int or min(limits.input_tokens, limits.output_tokens) < 1):
                raise TranslationError("Некорректные ограничения модели Gemini.")
            self._limits = limits
        if self._options.max_output_tokens > self._limits.output_tokens:
            raise TranslationError("max_output_tokens больше ограничения выбранной модели.")
        prompt = make_prompt(request, self.response_contract)
        schema = response_schema(request, self.response_contract)
        with fields(request_kind="count_tokens", model=self._options.model):
            count = executor.call(lambda: self.gateway.count_tokens(self._options.model, count_prompt(prompt, schema, self.response_contract)), label="подсчёт токенов")
        if type(count) is not int or count < 1:
            raise TranslationError("Некорректное число токенов от Gemini.")
        if count + 512 > min(self._options.max_input_tokens, self._limits.input_tokens):
            raise BatchTooLarge("Пакет с контекстом/глоссарием не помещается во входные токены Gemini.")
        emit("provider.input_count", model=self._options.model, input_tokens=count)
        with fields(request_kind="generation", model=self._options.model):
            result = executor.call(lambda: self.gateway.generate(self._options.model, prompt, self._options.max_output_tokens, response_schema=schema),
                                tokens=count + 512 + self._options.max_output_tokens,
                                label=f"перевод {len(request.items)} сегментов")
        return TranslationResponse(decode_response(result.text, self.response_contract), result.model)


class GeminiProvider:
    def __init__(self, *, gateway_factory=GeminiGateway, async_gateway_factory=AsyncGeminiGateway, response_contract="strict"):
        self.identity = provider_identity(response_contract)
        self.response_contract = response_contract
        self._gateway_factory = gateway_factory
        self._gateways = []
        self._async_gateway_factory, self._async_gateways = async_gateway_factory, []

    def prepare_async(self, options):
        from .async_gemini import AsyncGeminiRun
        validate_model(options.model)
        return AsyncGeminiRun(options, self._async_gateway_factory, self._async_gateways, response_contract=self.response_contract)

    async def aclose(self):
        try:
            await aclose_gateways(self._async_gateways)
        finally:
            self.close()

    def prepare(self, options: TranslationOptions) -> GeminiSession:
        """Local-only preparation; previously returned sessions keep their settings."""
        validate_model(options.model)
        gateway = self._gateway_factory(options.timeout_seconds)
        self._gateways.append(gateway)
        return GeminiSession(options, gateway, response_contract=self.response_contract)

    def close(self):
        close_gateways(self._gateways)
