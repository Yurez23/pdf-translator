"""Gemini auto composition root: local credentials, SDK and SQLite wiring only."""

import os
import re
import time
from dataclasses import replace

from ..telemetry import emit, fields, instrument
from .availability import AvailabilityCache, default_database_path
from .catalog import POLICY_VERSION
from .gemini import GeminiSession
from .gemini_gateway import GeminiGateway, AsyncGeminiGateway, close_gateways, aclose_gateways
from .gemini_format import provider_identity
from .models import ModelLimits, TranslationError
from .model_routing import AutoRoutingSession
from .ports import ProviderIdentity
from .serialization import digest


def cache_namespace(project_id: str | None, key: str) -> str:
    """No raw key/project identifier is persisted. Explicit project shares keys."""
    if project_id:
        if not re.fullmatch(r"[a-z][a-z0-9-]{3,62}|\d{6,24}", project_id):
            raise TranslationError("Некорректный --project-id (ID или номер проекта Google).")
        return digest("gemini-project:" + project_id)
    if not key:
        raise TranslationError("Не задан GEMINI_API_KEY.")
    return digest("gemini-credential-namespace:" + key)


class GatewayCatalog:
    def __init__(self, gateway):
        self.gateway = gateway

    def page(self, token, *, executor):
        with fields(request_kind="catalog", model="auto"):
            return executor.call(lambda: self.gateway.list_models_page(token), label="каталог моделей")


class GeminiAutoProvider:
    def __init__(self, *, database_path=None, project_id=None, reset_cache=False,
                 ignore_cache=False, progress=None, clock=time.time,
                 gateway_factory=GeminiGateway, cache_factory=AvailabilityCache,
                 async_gateway_factory=AsyncGeminiGateway, response_contract="strict"):
        self.identity = ProviderIdentity("gemini-auto", digest({"policy": POLICY_VERSION,
                                                               "adapter": provider_identity(response_contract).revision}))
        self.response_contract = response_contract
        self.database_path = database_path if database_path is not None else default_database_path()
        self.project_id, self.reset_cache, self.ignore_cache = project_id, reset_cache, ignore_cache
        self.progress, self.clock = progress or (lambda _: None), clock
        self._gateway_factory, self._cache_factory = gateway_factory, cache_factory
        self._gateways = []
        self._async_gateway_factory, self._async_gateways = async_gateway_factory, []

    def prepare_async(self, options):
        from .async_gemini import AsyncGeminiRun
        if options.model != "auto":
            raise TranslationError("Автоматический адаптер требует model='auto'.")
        namespace = cache_namespace(self.project_id or os.environ.get("GEMINI_PROJECT_ID"),
                                    os.environ.get("GEMINI_API_KEY", "").strip())
        cache = self._cache_factory(self.database_path, clock=self.clock)
        emit("routing.cache_ready")
        if self.reset_cache:
            cache.reset(namespace)
            emit("routing.cache_reset")
        return AsyncGeminiRun(options, self._async_gateway_factory, self._async_gateways,
                              cache=cache, namespace=namespace, ignore_cache=self.ignore_cache,
                              progress=self.progress, clock=self.clock, response_contract=self.response_contract)

    async def aclose(self):
        try:
            await aclose_gateways(self._async_gateways)
        finally:
            self.close()

    @instrument("routing.prepare")
    def prepare(self, options):
        if options.model != "auto":
            raise TranslationError("Автоматический адаптер требует model='auto'.")
        namespace = cache_namespace(self.project_id or os.environ.get("GEMINI_PROJECT_ID"),
                                    os.environ.get("GEMINI_API_KEY", "").strip())
        gateway = self._gateway_factory(options.timeout_seconds)
        self._gateways.append(gateway)
        cache = self._cache_factory(self.database_path, clock=self.clock)
        emit("routing.cache_ready")
        if self.reset_cache:
            cache.reset(namespace)
            emit("routing.cache_reset")
        def session_factory(candidate):
            return GeminiSession(replace(options, model=candidate.name), gateway,
                                 limits=ModelLimits(candidate.input_tokens, candidate.output_tokens), response_contract=self.response_contract)
        return AutoRoutingSession(options, reader=GatewayCatalog(gateway), session_factory=session_factory,
                                  cache=cache, namespace=namespace, ignore_cache=self.ignore_cache,
                                  progress=self.progress, clock=self.clock)

    def close(self):
        close_gateways(self._gateways)
