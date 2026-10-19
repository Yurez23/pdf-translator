"""Offline auto-selection, SQLite persistence, error classification and resume."""

import contextlib
import io
import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pdf_translator.translation import TranslationError, TranslationOptions, translate_file
from pdf_translator.translation.availability import AvailabilityCache, default_database_path
from pdf_translator.translation.catalog import rank_models
from pdf_translator.translation.cli import main
from pdf_translator.translation.models import ProviderError, QuotaError
from pdf_translator.translation.gemini_gateway import GenerationResult
from pdf_translator.translation.routing import GeminiAutoProvider, cache_namespace
from pdf_translator.translation.model_routing import cooldown_for
from pdf_translator.translation.execution import RequestController
from tests.test_translation import FakeProvider, LocalCase, OPTIONS


def model(name, *, methods=("generateContent",), input_tokens=100000, output_tokens=65536):
    return SimpleNamespace(name="models/" + name, supported_actions=methods,
                           input_token_limit=input_tokens, output_token_limit=output_tokens)


CATALOG = [model("gemini-3.5-flash-lite"), model("gemini-3.8-flash"), model("gemini-3.8-pro")]


class CatalogAdapter(FakeProvider):
    def __init__(self, *, failures=None, catalog=None, aliases=None):
        super().__init__()
        self.failures = failures or {}
        self.catalog = CATALOG if catalog is None else catalog
        self.aliases = aliases or {}
        self.pages = []
        self.attempted = []
        self.tokens_by_model = {}

    def connect(self, options):
        self.prepare(options)

    def list_models_page(self, token=None):
        self.pages.append(token)
        return self.catalog, None

    def count_tokens(self, model, prompt):
        return self.tokens_by_model.get(model, 100)

    def generate(self, name, prompt, max_output_tokens, *, response_schema=None):
        self.attempted.append(name)
        if name in self.failures:
            raise self.failures[name]
        raw = super().generate(prompt, max_output_tokens)
        if self.response is None and response_schema["properties"]["translations"]["type"] == "object":
            raw = json.dumps({"translations": {v["id"]: v["text"] for v in json.loads(raw)["translations"]}})
        return GenerationResult(raw, self.aliases.get(name, name))


class CatalogTests(unittest.TestCase):
    def test_generation_then_class_stability_revision_and_total_order(self):
        names = ["gemini-3.8-flash-lite", "gemini-3.8-flash", "gemini-3.8-pro-preview",
                 "gemini-3.8-pro", "gemini-3.8-pro-002", "gemini-3.8-pro-001", "gemini-3.6-pro",
                 "gemini-3.10-flash", "gemini-3-flash"]
        expected = ["gemini-3.10-flash", "gemini-3.8-pro-002", "gemini-3.8-pro-001", "gemini-3.8-pro",
                    "gemini-3.8-pro-preview", "gemini-3.8-flash", "gemini-3.8-flash-lite", "gemini-3.6-pro", "gemini-3-flash"]
        for source in (names, list(reversed(names)), names + names):
            self.assertEqual([item.name for item in rank_models(map(model, source))], expected)

    def test_specialized_unknown_aliases_and_missing_capabilities_are_excluded(self):
        names = ["gemini-3.8-live", "gemini-3.8-flash-tts", "gemini-3.8-flash-image", "gemini-3.8-robotics",
                 "gemini-embedding-2", "gemini-4-argon", "gemini-flash-latest", "gemma-4-31b", "gemini-3.8-pro-custom"]
        entries = [*map(model, names), model("gemini-3.8-flash", methods=("embedContent",)),
                   model("gemini-3.5-flash-lite", output_tokens=0), model("gemini-3.1-pro", input_tokens=None)]
        self.assertFalse(rank_models(entries))


class CacheTests(LocalCase):
    def setUp(self):
        super().setUp()
        self.now = [1700000000.0]
        self.path = self.root / "availability.sqlite3"
        self.cache = AvailabilityCache(self.path, clock=lambda: self.now[0])

    def test_persistence_expiry_namespaces_and_success(self):
        self.cache.defer("p1", "m", reason="minute", retry_at=self.now[0] + 60, basis="server")
        other = AvailabilityCache(self.path, clock=lambda: self.now[0])
        self.assertEqual(other.blocked("p1", "m").reason, "minute")
        self.assertIsNone(other.blocked("p2", "m"))
        other.succeeded("p1", "m", started_at=self.now[0] - 1)
        self.assertIsNotNone(other.blocked("p1", "m"))
        self.now[0] += 61
        self.assertIsNone(other.blocked("p1", "m"))
        other.succeeded("p1", "m", started_at=self.now[0])

    def test_shared_project_and_observed_alias(self):
        self.cache.remember_alias("p", "old", "new")
        self.cache.defer("p", "old", reason="daily", retry_at=self.now[0] + 3600, basis="server")
        self.assertIsNotNone(self.cache.blocked("p", "new"))
        self.cache.defer("p", "*", reason="minute", retry_at=self.now[0] + 60, basis="server")
        self.assertIsNotNone(self.cache.blocked("p", "unrelated"))
        self.cache.reset("p")
        self.assertIsNone(self.cache.blocked("p", "old"))

    def test_lease_exclusion_owner_and_expiration(self):
        first = self.cache.claim("p", "m", seconds=30)
        self.assertIsNotNone(first)
        self.assertIsNone(self.cache.claim("p", "m", seconds=30))
        self.cache.release("p", "m", "not-owner")
        self.assertIsNone(self.cache.claim("p", "m", seconds=30))
        self.now[0] += 31
        second = self.cache.claim("p", "m", seconds=30)
        self.assertIsNotNone(second)
        self.cache.release("p", "m", first)
        self.assertIsNone(self.cache.claim("p", "m", seconds=30))
        self.cache.release("p", "m", second)
        self.assertIsNotNone(self.cache.claim("p", "m", seconds=30))

    def test_parallel_claims_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.cache.claim("p", "m", seconds=60), range(4)))
        self.assertEqual(sum(value is not None for value in results), 1)

    def test_damaged_unknown_and_foreign_database_are_not_overwritten(self):
        bad = self.root / "bad.sqlite3"
        bad.write_bytes(b"not sqlite")
        with self.assertRaises(TranslationError):
            AvailabilityCache(bad)
        self.assertEqual(bad.read_bytes(), b"not sqlite")
        for name, sql in (("future", "PRAGMA user_version=7"), ("foreign", "CREATE TABLE unrelated(x)")):
            path = self.root / (name + ".sqlite3")
            with contextlib.closing(sqlite3.connect(path)) as db:
                db.execute(sql)
            with self.assertRaises(TranslationError):
                AvailabilityCache(path)

    def test_default_path_is_checkout_root_and_namespace_contains_no_credentials(self):
        self.assertEqual(default_database_path(), Path(__file__).resolve().parents[1] / "model-availability.sqlite3")
        a = cache_namespace(None, "test-secret-one")
        self.assertNotEqual(a, cache_namespace(None, "test-secret-two"))
        self.assertEqual(cache_namespace("test-project", "one"), cache_namespace("test-project", "two"))
        self.assertNotIn("test-secret", a)
        for project in ("https://wrong", "bad id"):
            with self.assertRaises(TranslationError):
                cache_namespace(project, "fake")


class AutoRoutingTests(LocalCase):
    def setUp(self):
        super().setUp()
        self.options = replace(OPTIONS, model="auto", max_retries=2)
        self.path = self.root / "availability.sqlite3"
        self.now = [1700000000.0]
        environment = patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-only", "GEMINI_PROJECT_ID": "test-project"})
        environment.start()
        self.addCleanup(environment.stop)

    def provider(self, adapter=None, **kwargs):
        adapter = adapter or CatalogAdapter()
        provider = GeminiAutoProvider(database_path=self.path, clock=lambda: self.now[0],
                                      gateway_factory=lambda _: adapter, **kwargs)
        provider.adapter = adapter
        prepare = provider.prepare
        def prepared(options):
            session = prepare(options)
            provider.cache, provider.namespace = session.cache, session.namespace
            return session
        provider.prepare = prepared
        return provider

    def run_auto(self, provider, **kwargs):
        options = kwargs.pop("options", self.options)
        folder = kwargs.pop("output_dir", self.out)
        return translate_file(self.source, output_dir=folder, options=options, provider=provider,
                              allow_network=True, **kwargs)

    def simulated_requests(self):
        def controller(policy, ledger, progress):
            def sleep(delay):
                self.now[0] += delay
            return RequestController(policy, ledger, progress, clock=lambda: self.now[0], sleep=sleep)
        return patch("pdf_translator.translation.runner.RequestController", side_effect=controller)

    def test_select_best_dynamic_model_and_record_attribution(self):
        provider = self.provider()
        result = self.run_auto(provider)
        self.assertEqual((result.status, result.requests_made), ("complete", 3))
        self.assertEqual(provider.adapter.attempted, ["gemini-3.8-pro"])
        state = json.loads(Path(result.checkpoint).read_text(encoding="utf-8"))
        self.assertEqual(set(state["segment_models"].values()), {"gemini-3.8-pro"})
        self.assertEqual(state["catalog"], ["gemini-3.8-pro", "gemini-3.8-flash", "gemini-3.5-flash-lite"])
        self.assertNotIn(b"fake-key-only", self.path.read_bytes())
        self.assertNotIn(b"ordinary paragraph", self.path.read_bytes())

    def test_logging_correlates_catalog_fallback_and_generation_without_key(self):
        from pdf_translator.telemetry import log_run
        events = []
        adapter = CatalogAdapter(failures={"gemini-3.8-pro": QuotaError(kind="daily", scope="model", retry_after=60)})
        with log_run(SimpleNamespace(write=events.append)):
            self.run_auto(self.provider(adapter))
        self.assertTrue(any(e["event"] == "routing.fallback" and e["reason"] == "daily" for e in events))
        calls = [e for e in events if e["event"] == "api.request.finished"]
        self.assertEqual([e["request_kind"] for e in calls], ["catalog", "count_tokens", "generation", "count_tokens", "generation"])
        self.assertEqual(calls[2]["status_code"], 429)
        self.assertEqual(calls[-1]["model"], "gemini-3.8-flash")
        self.assertTrue(all(e["batch_id"] == "b000001" for e in calls))
        self.assertNotIn("fake-key-only", json.dumps(events))
        self.assertNotIn("ordinary paragraph", json.dumps(events))

    def test_quota_fallback_no_retry_then_skip_persisted_model_next_run(self):
        adapter = CatalogAdapter(failures={"gemini-3.8-pro": QuotaError(kind="daily", scope="model", retry_after=3600)})
        first = self.provider(adapter)
        result = self.run_auto(first)
        self.assertEqual(result.requests_made, 5)
        self.assertEqual(adapter.attempted, ["gemini-3.8-pro", "gemini-3.8-flash"])
        second = self.provider()
        self.run_auto(second, output_dir=self.root / "second")
        self.assertEqual(second.adapter.attempted, ["gemini-3.8-flash"])
        self.assertEqual(second.adapter.pages, [None])

    def test_minute_expiry_returns_to_top_without_request_accounting(self):
        adapter = CatalogAdapter(failures={"gemini-3.8-pro": QuotaError(kind="minute", scope="model", retry_after=10)})
        self.run_auto(self.provider(adapter))
        self.now[0] += 61
        next_provider = self.provider()
        self.run_auto(next_provider, output_dir=self.root / "next")
        self.assertEqual(next_provider.adapter.attempted, ["gemini-3.8-pro"])

    def test_every_model_blocked_pauses_and_keeps_checkpoint(self):
        failures = {entry.name[7:]: QuotaError(kind="daily", scope="model", retry_after=3600) for entry in CATALOG}
        provider = self.provider(CatalogAdapter(failures=failures))
        result = self.run_auto(provider)
        self.assertEqual(result.status, "paused")
        self.assertIn("Ближайшая", result.pause_reason)
        self.assertFalse(list(self.out.glob("*.md")))
        self.assertEqual(len(provider.adapter.attempted), 3)
        again = self.provider()
        result = self.run_auto(again, resume=True)
        self.assertEqual(result.requests_made, 1)  # Only the fresh catalog.
        self.assertFalse(again.adapter.attempted)

    def test_project_quota_stops_without_trying_other_models(self):
        adapter = CatalogAdapter(failures={"gemini-3.8-pro": QuotaError(kind="minute", scope="project", retry_after=60)})
        result = self.run_auto(self.provider(adapter))
        self.assertEqual(result.status, "paused")
        self.assertIn("Общее ограничение", result.pause_reason)
        self.assertEqual(adapter.attempted, ["gemini-3.8-pro"])
        other = self.provider()
        self.run_auto(other, resume=True)
        self.assertFalse(other.adapter.attempted)

    def test_auth_bad_parameters_network_and_invalid_json_do_not_fallback(self):
        for code in (400, 401, 403, 500, 502, 0):
            provider = self.provider(CatalogAdapter(failures={"gemini-3.8-pro": ProviderError("sanitized", status_code=code)}))
            with self.subTest(code=code), self.assertRaises(ProviderError):
                self.run_auto(provider, output_dir=self.root / str(code))
            self.assertEqual(provider.adapter.attempted, ["gemini-3.8-pro"])
        adapter = CatalogAdapter()
        adapter.response = "not json"
        result = self.run_auto(self.provider(adapter), output_dir=self.root / "json")
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(adapter.attempted, ["gemini-3.8-pro"] * 2)

    def test_service_failures_fallback_after_retries_with_status_and_attribution(self):
        from pdf_translator.telemetry import log_run
        for code in (503, 504):
            with self.subTest(code=code):
                self.path = self.root / f"service-{code}.sqlite3"
                error = ProviderError("private provider details", status_code=code, retryable=True)
                adapter = CatalogAdapter(failures={"gemini-3.8-pro": error})
                provider, events = self.provider(adapter), []
                with self.simulated_requests(), log_run(SimpleNamespace(write=events.append)):
                    result = self.run_auto(provider, output_dir=self.root / f"service-{code}")
                self.assertEqual((result.status, result.requests_made), ("complete", 7))
                self.assertEqual(adapter.attempted, ["gemini-3.8-pro"] * 3 + ["gemini-3.8-flash"])
                blocked = provider.cache.blocked(provider.namespace, "gemini-3.8-pro")
                self.assertEqual((blocked.reason, blocked.retry_at), ("unavailable", self.now[0] + 300))
                fallback = [e for e in events if e["event"] == "routing.fallback"]
                self.assertEqual(len(fallback), 1)
                self.assertEqual((fallback[0]["status_code"], fallback[0]["reason"], fallback[0]["delay_seconds"]),
                                 (code, "unavailable", 300))
                self.assertEqual(sum(e["event"] == "api.retry" for e in events), 2)
                self.assertNotIn("private provider details", json.dumps(events))
                state = json.loads(Path(result.checkpoint).read_text(encoding="utf-8"))
                self.assertEqual(set(state["segment_models"].values()), {"gemini-3.8-flash"})
                self.assertEqual(len(adapter.generated), len(set(adapter.generated)))
                owner = provider.cache.claim(provider.namespace, "gemini-3.8-pro", seconds=1, ignore_cooldown=True)
                self.assertIsNotNone(owner)
                provider.cache.release(provider.namespace, "gemini-3.8-pro", owner)

    def test_service_failures_descend_through_multiple_models(self):
        adapter = CatalogAdapter(failures={
            "gemini-3.8-pro": ProviderError("busy", status_code=503, retryable=True),
            "gemini-3.8-flash": ProviderError("deadline", status_code=504, retryable=True),
        })
        with self.simulated_requests():
            result = self.run_auto(self.provider(adapter))
        self.assertEqual((result.status, result.requests_made), ("complete", 11))
        self.assertEqual(adapter.attempted, ["gemini-3.8-pro"] * 3 + ["gemini-3.8-flash"] * 3 + ["gemini-3.5-flash-lite"])

    def test_service_cooldown_persists_across_runs_and_expires(self):
        first = self.provider(CatalogAdapter(failures={"gemini-3.8-pro": ProviderError("busy", status_code=503)}))
        self.assertEqual(self.run_auto(first).status, "complete")
        second = self.provider()
        self.run_auto(second, output_dir=self.root / "second")
        self.assertEqual(second.adapter.attempted, ["gemini-3.8-flash"])
        self.now[0] += 301
        third = self.provider()
        self.run_auto(third, output_dir=self.root / "third")
        self.assertEqual(third.adapter.attempted, ["gemini-3.8-pro"])

    def test_service_retry_after_defers_only_the_failed_model(self):
        adapter = CatalogAdapter(failures={"gemini-3.8-pro": ProviderError("busy", status_code=503, retry_after=900)})
        provider = self.provider(adapter)
        before = self.now[0]
        with self.simulated_requests():
            result = self.run_auto(provider)
        self.assertEqual((result.status, result.requests_made), ("complete", 5))
        self.assertEqual(self.now[0], before)
        blocked = provider.cache.blocked(provider.namespace, "gemini-3.8-pro")
        self.assertEqual((blocked.retry_at, blocked.basis), (before + 900, "server"))
        self.assertIsNone(provider.cache.blocked(provider.namespace, "*"))

    def test_all_services_unavailable_pause_and_resume_after_cooldown(self):
        failures = {entry.name[7:]: ProviderError("busy", status_code=503) for entry in CATALOG}
        first = self.provider(CatalogAdapter(failures=failures))
        result = self.run_auto(first)
        self.assertEqual((result.status, result.completed_segments), ("paused", 0))
        self.assertFalse(list(self.out.glob("*.md")))
        self.assertFalse(list(self.out.glob("*.lock")))
        second = self.provider()
        again = self.run_auto(second, resume=True)
        self.assertEqual((again.status, again.requests_made), ("paused", 1))
        self.assertFalse(second.adapter.attempted)
        self.now[0] += 301
        resumed = self.run_auto(self.provider(), resume=True)
        self.assertEqual(resumed.status, "complete")

    def test_recovery_within_retries_does_not_fallback_or_cache(self):
        adapter = CatalogAdapter(failures={"gemini-3.8-pro": ProviderError("busy", status_code=503, retryable=True)})
        original = adapter.generate
        def generate(*args, **kwargs):
            if len(adapter.attempted) == 2:
                adapter.failures.clear()
            return original(*args, **kwargs)
        adapter.generate = generate
        provider = self.provider(adapter)
        with self.simulated_requests():
            result = self.run_auto(provider)
        self.assertEqual((result.status, result.requests_made), ("complete", 5))
        self.assertEqual(adapter.attempted, ["gemini-3.8-pro"] * 3)
        self.assertIsNone(provider.cache.blocked(provider.namespace, "gemini-3.8-pro"))

    def test_service_failure_during_token_count_falls_back(self):
        adapter = CatalogAdapter()
        original = adapter.count_tokens
        def count(name, prompt):
            if name == "gemini-3.8-pro":
                raise ProviderError("busy", status_code=504)
            return original(name, prompt)
        adapter.count_tokens = count
        result = self.run_auto(self.provider(adapter))
        self.assertEqual((result.status, result.requests_made), ("complete", 4))
        self.assertEqual(adapter.attempted, ["gemini-3.8-flash"])

    def test_catalog_service_failure_does_not_block_individual_models(self):
        adapter = CatalogAdapter()
        error = ProviderError("catalog busy", status_code=503, retryable=True)
        def page(token):
            adapter.pages.append(token)
            raise error
        adapter.list_models_page = page
        provider = self.provider(adapter)
        with self.simulated_requests(), self.assertRaises(ProviderError) as caught:
            self.run_auto(provider)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(adapter.pages), 3)
        self.assertFalse(adapter.attempted)
        self.assertIsNone(provider.cache.blocked(provider.namespace, "gemini-3.8-pro"))

    def test_service_fallback_respects_budget_and_resumes_saved_segments(self):
        self.source.write_text("A paragraph.\n\n" + "Another sentence. " * 35, encoding="utf-8")
        adapter = CatalogAdapter()
        original = adapter.generate
        def generate(name, prompt, tokens, **kwargs):
            if adapter.generated:
                adapter.failures["gemini-3.8-pro"] = ProviderError("busy", status_code=503)
            return original(name, prompt, tokens, **kwargs)
        adapter.generate = generate
        options = replace(self.options, max_chunk_chars=256, max_requests=5)
        first = self.run_auto(self.provider(adapter), options=options)
        self.assertEqual((first.status, first.requests_made, first.completed_segments), ("paused", 5, 1))
        saved = set(adapter.generated)
        second = self.provider()
        result = self.run_auto(second, options=replace(options, max_requests=100), resume=True)
        self.assertEqual(result.status, "complete")
        self.assertEqual(set(second.adapter.attempted), {"gemini-3.8-flash"})
        self.assertTrue(saved.isdisjoint(second.adapter.generated))
        state = json.loads(Path(result.checkpoint).read_text(encoding="utf-8"))
        self.assertEqual(set(state["segment_models"].values()), {"gemini-3.8-pro", "gemini-3.8-flash"})

    def test_service_cooldown_is_short_and_respects_retry_after(self):
        for code in (503, 504):
            for delay, expected in ((0, 300), (90, 300), (900, 900), (float("nan"), 300), (-10, 300)):
                with self.subTest(code=code, delay=delay):
                    reason, until, basis = cooldown_for(ProviderError("busy", status_code=code, retry_after=delay), self.now[0])
                    self.assertEqual((reason, until), ("unavailable", self.now[0] + expected))
                    self.assertEqual(basis, "server" if delay > 0 else "estimated")
        self.assertEqual(cooldown_for(ProviderError("missing", status_code=404), self.now[0])[1], self.now[0] + 3600)

    def test_not_found_falls_back_and_unknown_quota_has_short_ttl(self):
        adapter = CatalogAdapter(failures={"gemini-3.8-pro": ProviderError("missing", status_code=404),
                                           "gemini-3.8-flash": QuotaError()})
        provider = self.provider(adapter)
        self.assertEqual(self.run_auto(provider).status, "complete")
        self.assertEqual(adapter.attempted[-1], "gemini-3.5-flash-lite")
        self.assertEqual(provider.cache.blocked(provider.namespace, "gemini-3.8-flash").retry_at, self.now[0] + 60)

    def test_budget_pause_resumes_with_fresh_catalog_without_resending_saved_segments(self):
        self.source.write_text("A paragraph.\n\n" + "Another sentence. " * 35, encoding="utf-8")
        options = replace(self.options, max_chunk_chars=256, max_requests=3)
        first = self.provider()
        result = self.run_auto(first, options=options)
        self.assertEqual((result.status, result.completed_segments), ("paused", 1))
        saved = set(first.adapter.generated)
        second = self.provider(CatalogAdapter(catalog=[model("gemini-4.2-flash"), *CATALOG]))
        result = self.run_auto(second, options=replace(options, max_requests=100), resume=True)
        self.assertEqual(result.status, "complete")
        self.assertTrue(saved.isdisjoint(second.adapter.generated))
        models = json.loads(Path(result.checkpoint).read_text(encoding="utf-8"))["segment_models"]
        self.assertEqual(set(models.values()), {"gemini-3.8-pro", "gemini-4.2-flash"})

    def test_complete_resume_needs_no_key_catalog_or_sqlite(self):
        self.run_auto(self.provider())
        with patch.dict(os.environ, {}, clear=True), patch("pdf_translator.translation.routing.AvailabilityCache", side_effect=AssertionError("Cache opened")):
            result = self.run_auto(GeminiAutoProvider(database_path=self.path), resume=True, overwrite=True)
        self.assertEqual(result.requests_made, 0)

    def test_reset_and_ignore_cache_restore_top_candidate(self):
        first = self.provider(CatalogAdapter(failures={"gemini-3.8-pro": QuotaError(kind="daily", scope="model", retry_after=3600)}))
        self.run_auto(first)
        for index, flags in enumerate(({"ignore_cache": True}, {"reset_cache": True})):
            provider = self.provider(**flags)
            self.run_auto(provider, output_dir=self.root / f"restored-{index}")
            self.assertEqual(provider.adapter.attempted, ["gemini-3.8-pro"])

    def test_observed_redirects_share_negative_cache(self):
        self.run_auto(self.provider(CatalogAdapter(aliases={"gemini-3.8-pro": "gemini-3.8-pro-001"})))
        second = self.provider(CatalogAdapter(failures={"gemini-3.8-pro": QuotaError(kind="daily", scope="model", model="gemini-3.8-pro-001", retry_after=3600)}))
        self.run_auto(second, output_dir=self.root / "second")
        self.assertIsNotNone(second.cache.blocked(second.namespace, "gemini-3.8-pro-001"))
        third = self.provider(CatalogAdapter(catalog=[model("gemini-3.8-pro-001"), *CATALOG]))
        self.run_auto(third, output_dir=self.root / "third")
        self.assertEqual(third.adapter.attempted, ["gemini-3.8-flash"])

    def test_pagination_is_budgeted_and_repeated_tokens_fail(self):
        adapter = CatalogAdapter()
        calls = []
        def pages(token):
            calls.append(token)
            return ([model("gemini-3.5-flash-lite")], "next") if token is None else ([model("gemini-3.8-pro")], None)
        adapter.list_models_page = pages
        result = self.run_auto(self.provider(adapter))
        self.assertEqual(result.requests_made, 4)
        self.assertEqual(calls, [None, "next"])
        adapter = CatalogAdapter()
        adapter.list_models_page = lambda token: ([], "same")
        with self.assertRaises(TranslationError):
            self.run_auto(self.provider(adapter), output_dir=self.root / "bad-pages")

    def test_oversized_input_splits_not_falls_back_to_a_weaker_model(self):
        adapter = CatalogAdapter()
        original = adapter.count_tokens
        adapter.count_tokens = lambda name, prompt: 1000 if len(json.loads(prompt.split("INPUT DATA:\n")[1])["segments"]) > 1 else original(name, prompt)
        result = self.run_auto(self.provider(adapter), options=replace(self.options, max_input_tokens=800))
        self.assertEqual(result.status, "complete")
        self.assertEqual(adapter.attempted, ["gemini-3.8-pro", "gemini-3.8-pro"])

    def test_empty_catalog_and_insufficient_output_limits_fail_actionably(self):
        for index, entries in enumerate(([], [model("gemini-3.8-pro", output_tokens=100)])):
            with self.subTest(entries=entries), self.assertRaises(TranslationError):
                self.run_auto(self.provider(CatalogAdapter(catalog=entries)), output_dir=self.root / str(index))

    def test_cli_default_auto_dry_run_does_not_open_sqlite_or_read_key(self):
        with patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main([str(self.source), "--availability-db", str(self.path)])
        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())

    def test_cli_auto_send_and_database_input_collision(self):
        # CLI async execution is exercised with native async gateways separately.
        with patch("pdf_translator.translation.cli.GeminiAutoProvider", side_effect=lambda **kwargs: FakeProvider()), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main([str(self.source), "--send", "--output-dir", str(self.out),
                         "--requests-per-minute", "1000", "--availability-db", str(self.path)])
        self.assertEqual(code, 0)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = main([str(self.source), "--send", "--availability-db", str(self.source)])
        self.assertEqual(code, 1)

    def test_server_delay_and_conservative_daily_reset_without_timezone_database(self):
        reason, until, basis = cooldown_for(QuotaError(kind="daily", retry_after=90), self.now[0])
        self.assertEqual((reason, until, basis), ("daily", self.now[0] + 90, "server"))
        from zoneinfo import ZoneInfoNotFoundError
        with patch("pdf_translator.translation.model_routing.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            _, until, basis = cooldown_for(QuotaError(kind="daily"), self.now[0])
        self.assertEqual(until, self.now[0] + 25 * 3600)
        self.assertIn("no-tzdata", basis)

    def test_daily_reset_tracks_pacific_daylight_saving_time(self):
        from datetime import datetime, timezone
        # Both inputs are 12:00 UTC. Pacific midnight changes UTC offset by season.
        for date, reset_hour in (((2026, 1, 15), 8), ((2026, 7, 15), 7)):
            now = datetime(*date, 12, tzinfo=timezone.utc).timestamp()
            _, until, basis = cooldown_for(QuotaError(kind="daily"), now)
            self.assertEqual(datetime.fromtimestamp(until, timezone.utc).hour, reset_hour)
            self.assertEqual(basis, "pacific-midnight")


if __name__ == "__main__":
    unittest.main()
