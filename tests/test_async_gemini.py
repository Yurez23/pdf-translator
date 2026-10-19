"""Native async Gemini SDK, concurrent auto routing and recovery leases offline."""

import asyncio
from contextlib import closing
import json
import os
import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from pdf_translator.telemetry import log_run
from pdf_translator.translation import TranslationError
from pdf_translator.translation.availability import AvailabilityCache
from pdf_translator.translation.gemini import GeminiProvider
from pdf_translator.translation.gemini_gateway import CatalogEntry, GenerationResult
from pdf_translator.translation.models import ModelLimits, ProviderError, QuotaError
from pdf_translator.translation.routing import GeminiAutoProvider, cache_namespace
from tests.test_async_translation import AsyncLocalCase

try:
    from google import genai
except ImportError:
    genai = None


class Gateway:
    def __init__(self, owner):
        self.owner, self.closed = owner, False
        owner.gateways.append(self)

    async def aclose(self):
        self.closed = True

    async def list_models_page(self, token=None):
        self.owner.pages.append(token)
        await asyncio.sleep(0)
        if self.owner.catalog_error:
            raise self.owner.catalog_error
        return self.owner.entries, None

    async def model_limits(self, model):
        return ModelLimits(100000, 65536)

    async def count_tokens(self, model, prompt):
        await asyncio.sleep(0)
        count = len(json.loads(prompt.split("INPUT DATA:\n")[1])["segments"])
        return 20000 if count > self.owner.batch_limit else 100

    async def generate(self, model, prompt, max_output_tokens, *, response_schema=None):
        self.owner.sent.append(model)
        data = json.loads(prompt.split("INPUT DATA:\n")[1])
        self.owner.active += 1
        self.owner.peak = max(self.owner.active, self.owner.peak)
        try:
            if self.owner.operation:
                await self.owner.operation(model, data)
            else:
                await asyncio.sleep(0.01)
            if getattr(self.owner, "response", None):
                return GenerationResult(self.owner.response(model, data), model)
            return GenerationResult(json.dumps({"translations": {
                s["id"]: "Перевод: " + s["text"] for s in data["segments"]}}),
                self.owner.aliases.get(model, model))
        finally:
            self.owner.active -= 1


class AsyncRoutingTests(AsyncLocalCase):
    def setUp(self):
        super().setUp()
        self.path = self.root / "availability.sqlite3"
        self.now = [1700000000.0]
        self.owner = SimpleNamespace(gateways=[], pages=[], sent=[], active=0, peak=0, operation=None,
                                     aliases={}, catalog_error=None, batch_limit=100,
                                     entries=tuple(CatalogEntry(name, ("generateContent",), 100000, 65536)
                                                   for name in ("gemini-3.8-pro", "gemini-3.8-flash")))
        self.options = replace(self.options, model="auto")
        self.enterContext(patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-only", "GEMINI_PROJECT_ID": "test-project"}))
        self.provider = GeminiAutoProvider(database_path=self.path, clock=lambda: self.now[0],
                                          async_gateway_factory=lambda _: Gateway(self.owner))
        self.namespace = cache_namespace("test-project", "fake-key-only")
        self.addAsyncCleanup(self.provider.aclose)

    async def test_one_catalog_independent_clients_five_generations_same_best_model(self):
        five = asyncio.Event()
        async def operation(model, data):
            if self.owner.active == 5:
                five.set()
            await asyncio.wait_for(five.wait(), 2)
        self.owner.operation = operation
        result = await self.run_file()
        self.assertEqual((result.status, result.requests_made), ("complete", 25))
        self.assertEqual((len(self.owner.pages), len(self.owner.gateways), self.owner.peak), (1, 5, 5))
        self.assertEqual(set(self.owner.sent), {"gemini-3.8-pro"})
        self.assertEqual(set(self.state()["segment_models"].values()), {"gemini-3.8-pro"})
        await self.provider.aclose()
        self.assertTrue(all(g.closed for g in self.owner.gateways))

    async def test_model_quotas_fallback_without_duplicate_accepted_segments(self):
        async def operation(model, data):
            if model == "gemini-3.8-pro":
                raise QuotaError(kind="daily", scope="model", retry_after=100)
            await asyncio.sleep(0.01)
        self.owner.operation = operation
        result = await self.run_file()
        self.assertEqual(result.status, "complete")
        self.assertEqual(set(self.state()["segment_models"].values()), {"gemini-3.8-flash"})
        self.assertEqual(len(self.state()["translations"]), 12)
        cache = AvailabilityCache(self.path, clock=lambda: self.now[0])
        self.assertIsNotNone(cache.blocked(self.namespace, "gemini-3.8-pro"))

    async def test_service_error_retries_then_fallback_and_actual_model_saved(self):
        attempts = []
        async def operation(model, data):
            attempts.append(model)
            if model == "gemini-3.8-pro":
                raise ProviderError("private details", status_code=503, retryable=True)
        self.owner.operation = operation
        self.owner.aliases["gemini-3.8-flash"] = "gemini-3.8-flash-001"
        self.options = replace(self.options, max_concurrency=1, max_retries=1)
        from pdf_translator.translation.async_execution import AsyncRequestController
        async def instant(_):
            await asyncio.sleep(0)
        original = AsyncRequestController.__init__
        def controller(instance, *args, **kwargs):
            original(instance, *args, **kwargs, sleep=instant)
        events = []
        with patch.object(AsyncRequestController, "__init__", controller), log_run(SimpleNamespace(write=events.append)):
            result = await self.run_file()
        self.assertEqual(result.status, "complete")
        self.assertEqual(attempts[:3], ["gemini-3.8-pro", "gemini-3.8-pro", "gemini-3.8-flash"])
        self.assertEqual(set(self.state()["segment_models"].values()), {"gemini-3.8-flash-001"})
        self.assertTrue(any(e["event"] == "routing.fallback" and e["status_code"] == 503 for e in events))
        self.assertNotIn("private details", json.dumps(events))

    async def test_project_quota_pauses_every_worker_without_lower_model_hopping(self):
        async def operation(model, data):
            raise QuotaError(kind="minute", scope="project", retry_after=60)
        self.owner.operation = operation
        result = await self.run_file()
        self.assertEqual((result.status, result.completed_segments), ("paused", 0))
        self.assertEqual(set(self.owner.sent), {"gemini-3.8-pro"})
        self.assertFalse(list(self.out.glob("*.lock")))
        self.assertIsNotNone(AvailabilityCache(self.path, clock=lambda: self.now[0]).blocked(self.namespace, "*"))

    async def test_late_success_cannot_erase_newer_negative_cache(self):
        self.options = replace(self.options, max_concurrency=2)
        failed = asyncio.Event()
        async def operation(model, data):
            if model == "gemini-3.8-pro":
                if data["segments"][0]["id"].startswith("u00000"):
                    await asyncio.sleep(0.01)
                    self.now[0] += 1
                    failed.set()
                    raise QuotaError(kind="minute", scope="model", retry_after=60)
                await failed.wait()
                await asyncio.sleep(0.03)
            else:
                await asyncio.sleep(0.01)
        self.owner.operation = operation
        result = await self.run_file()
        self.assertEqual(result.status, "complete")
        self.assertEqual(self.owner.sent.count("gemini-3.8-pro"), 2)
        cache = AvailabilityCache(self.path, clock=lambda: self.now[0])
        self.assertIsNotNone(cache.blocked(self.namespace, "gemini-3.8-pro"))
        self.assertEqual(set(self.state()["segment_models"].values()), {"gemini-3.8-pro", "gemini-3.8-flash"})

    async def test_expired_cooldown_uses_one_probe_then_parallel_best_model(self):
        cache = AvailabilityCache(self.path, clock=lambda: self.now[0])
        cache.defer(self.namespace, "gemini-3.8-pro", reason="minute", retry_at=self.now[0] + 1, basis="server")
        self.now[0] += 2
        probe = asyncio.Event()
        async def operation(model, data):
            if not probe.is_set():
                self.assertEqual(self.owner.active, 1)
                probe.set()
                await asyncio.sleep(0.03)
            else:
                await asyncio.sleep(0.01)
        self.owner.operation = operation
        result = await self.run_file()
        self.assertEqual(result.status, "complete")
        self.assertEqual(set(self.owner.sent), {"gemini-3.8-pro"})
        self.assertGreater(self.owner.peak, 1)
        self.assertIsNone(cache.blocked(self.namespace, "gemini-3.8-pro"))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    async def test_cancel_recovery_probe_releases_lease(self):
        cache = AvailabilityCache(self.path, clock=lambda: self.now[0])
        cache.defer(self.namespace, "gemini-3.8-pro", reason="minute", retry_at=self.now[0] + 1, basis="server")
        self.now[0] += 2
        started = asyncio.Event()
        async def operation(model, data):
            started.set()
            await asyncio.Event().wait()
        self.owner.operation = operation
        task = asyncio.create_task(self.run_file())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)

    async def test_catalog_failure_is_single_run_failure_not_model_fallback(self):
        self.owner.catalog_error = ProviderError("safe catalog error", status_code=503)
        with self.assertRaises(ProviderError):
            await self.run_file()
        self.assertFalse(self.owner.sent)

    async def test_all_models_unavailable_pause_then_resume_after_expiry(self):
        async def operation(model, data):
            raise ProviderError("safe service error", status_code=504)
        self.owner.operation = operation
        result = await self.run_file()
        self.assertEqual((result.status, result.completed_segments), ("paused", 0))
        self.now[0] += 301
        self.owner.operation = None
        result = await self.run_file(resume=True)
        self.assertEqual((result.status, result.completed_segments), ("complete", 12))

    async def test_max_tokens_does_not_fallback_or_save_partial_batch(self):
        self.options = replace(self.options, max_concurrency=1)
        async def operation(model, data):
            raise TranslationError("MAX_TOKENS synthetic truncation")
        self.owner.operation = operation
        with self.assertRaisesRegex(TranslationError, "MAX_TOKENS"):
            await self.run_file()
        self.assertEqual(self.owner.sent, ["gemini-3.8-pro"])
        self.assertFalse(self.state()["translations"])

    async def test_invalid_ids_retry_same_model_once_without_fallback_or_cooldown(self):
        counts = {}
        def response(model, data):
            identifier = data["segments"][0]["id"]
            counts[identifier] = counts.get(identifier, 0) + 1
            value = {} if counts[identifier] == 1 else {s["id"]: "Перевод: " + s["text"] for s in data["segments"]}
            return json.dumps({"translations": value})
        self.owner.response = response
        result = await self.run_file()
        self.assertEqual((result.status, result.requests_made), ("complete", 49))
        self.assertEqual(set(self.owner.sent), {"gemini-3.8-pro"})
        self.assertEqual(len(self.owner.sent), 24)
        self.assertTrue(all(v == 2 for v in counts.values()))
        self.assertIsNone(AvailabilityCache(self.path, clock=lambda: self.now[0]).blocked(self.namespace, "gemini-3.8-pro"))

    async def test_invalid_output_exhaustion_does_not_double_retry_or_hop_models(self):
        self.owner.response = lambda model, data: '{"translations":{}}'
        result = await self.run_file()
        self.assertEqual((result.status, len(result.failed_batches), len(self.owner.sent)), ("incomplete", 12, 24))
        self.assertEqual(set(self.owner.sent), {"gemini-3.8-pro"})

    async def test_manual_async_provider_and_dynamic_split(self):
        self.provider = GeminiProvider(async_gateway_factory=lambda _: Gateway(self.owner))
        self.addAsyncCleanup(self.provider.aclose)
        self.owner.batch_limit = 1
        self.options = replace(self.options, model="gemini-test", max_chunk_chars=16000)
        result = await self.run_file()
        self.assertEqual((result.status, len(self.owner.sent)), ("complete", 12))
        self.assertEqual(set(self.state()["segment_models"].values()), {"gemini-test"})


@unittest.skipUnless(genai, "Install translation/dev extra for SDK tests")
class NativeAsyncSdkTests(AsyncLocalCase):
    async def asyncSetUp(self):
        import httpx
        from google import genai
        from google.genai import types
        self.requests = []
        self.body_override = None
        self.catalog_mode = False
        self.status = 200
        async def response(request):
            self.requests.append(request)
            if request.method == "GET":
                if self.catalog_mode and request.url.path.endswith("/models"):
                    token = request.url.params.get("pageToken")
                    name = "gemini-3.8-flash" if token else "gemini-3.8-pro"
                    body = {"models": [{"name": "models/" + name, "supportedGenerationMethods": ["generateContent"],
                                        "inputTokenLimit": 100000, "outputTokenLimit": 65536}]}
                    if not token:
                        body["nextPageToken"] = "second"
                    return httpx.Response(200, json=body)
                return httpx.Response(200, json={"name": "models/gemini-test", "inputTokenLimit": 100000, "outputTokenLimit": 65536})
            if request.url.path.endswith(":countTokens"):
                return httpx.Response(200, json={"totalTokens": 100})
            data = json.loads(json.loads(request.content)["contents"][0]["parts"][0]["text"].split("INPUT DATA:\n")[1])
            body = self.body_override or {"modelVersion": "gemini-test-001", "candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"text": json.dumps({"translations": {s["id"]: "Перевод: " + s["text"] for s in data["segments"]}})}]}}]}
            return httpx.Response(self.status, json=body)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(response))
        self.client = genai.Client(api_key="fake-key-only", vertexai=False, http_options=types.HttpOptions(
            httpx_async_client=self.http, timeout=300000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.addCleanup(self.client.close)
        self.addAsyncCleanup(self.client.aio.aclose)
        self.addAsyncCleanup(self.http.aclose)
        self.enterContext(patch("google.genai.Client", return_value=self.client))
        self.enterContext(patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-only"}))
        self.provider = GeminiProvider()
        self.addAsyncCleanup(self.provider.aclose)
        self.options = replace(self.options, model="gemini-test", max_chunk_chars=16000)

    async def test_real_sdk_aio_wire_and_timeout_attribution_no_live_network(self):
        result = await self.run_file()
        self.assertEqual((result.status, result.requests_made), ("complete", 3))
        generation = self.requests[-1]
        self.assertEqual(generation.headers["X-Server-Timeout"], "300")
        self.assertEqual(json.loads(generation.content)["generationConfig"]["maxOutputTokens"], 32768)
        self.assertEqual(set(self.state()["segment_models"].values()), {"gemini-test-001"})

    async def test_native_truncation_is_error_and_no_partial_export(self):
        self.body_override = {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]}
        with self.assertRaises(TranslationError):
            await self.run_file()
        self.assertFalse(self.state()["translations"])
        self.assertFalse(list(self.out.glob("*.md")))

    async def test_native_async_catalog_pagination_is_budgeted(self):
        self.catalog_mode = True
        self.options = replace(self.options, model="auto")
        self.provider = GeminiAutoProvider(database_path=self.root / "catalog.sqlite3", project_id="test-project")
        self.addAsyncCleanup(self.provider.aclose)
        result = await self.run_file()
        self.assertEqual((result.status, result.requests_made), ("complete", 4))
        self.assertEqual([r.url.params.get("pageToken") for r in self.requests if r.method == "GET"], [None, "second"])

    async def test_native_async_error_is_sanitized(self):
        self.status = 503
        self.body_override = {"error": {"code": 503, "status": "UNAVAILABLE", "message": "private provider content"}}
        events = []
        with log_run(SimpleNamespace(write=events.append)), self.assertRaises(ProviderError) as error:
            await self.run_file()
        self.assertEqual(error.exception.status_code, 503)
        self.assertNotIn("private provider content", str(error.exception))
        self.assertNotIn("private provider content", json.dumps(events))
