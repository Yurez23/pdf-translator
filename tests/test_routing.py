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
from pdf_translator.translation.routing import GeminiAutoProvider, cache_namespace, cooldown_for
from tests.test_translation import FakeProvider, LocalCase, OPTIONS


def model(name, *, methods=("generateContent",), input_tokens=100000, output_tokens=32000):
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

    def count_tokens(self, prompt):
        return self.tokens_by_model.get(self.options.model, 100)

    def generate(self, prompt, max_output_tokens):
        name = self.options.model
        self.attempted.append(name)
        if name in self.failures:
            raise self.failures[name]
        self.model_version = self.aliases.get(name, name)
        return super().generate(prompt, max_output_tokens)


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
        provider = GeminiAutoProvider(database_path=self.path, clock=lambda: self.now[0], **kwargs)
        provider.adapter = adapter or CatalogAdapter()
        return provider

    def run_auto(self, provider, **kwargs):
        options = kwargs.pop("options", self.options)
        folder = kwargs.pop("output_dir", self.out)
        return translate_file(self.source, output_dir=folder, options=options, provider=provider,
                              allow_network=True, **kwargs)

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
        for code in (400, 401, 403, 503, 0):
            provider = self.provider(CatalogAdapter(failures={"gemini-3.8-pro": ProviderError("sanitized", status_code=code)}))
            with self.subTest(code=code), self.assertRaises(ProviderError):
                self.run_auto(provider, output_dir=self.root / str(code))
            self.assertEqual(provider.adapter.attempted, ["gemini-3.8-pro"])
        adapter = CatalogAdapter()
        adapter.response = "not json"
        with self.assertRaises(TranslationError):
            self.run_auto(self.provider(adapter), output_dir=self.root / "json")
        self.assertEqual(adapter.attempted, ["gemini-3.8-pro"])

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
        adapter.count_tokens = lambda prompt: 1000 if len(json.loads(prompt.split("INPUT DATA:\n")[1])["segments"]) > 1 else original(prompt)
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
        with patch("pdf_translator.translation.cli.GeminiAutoProvider", side_effect=lambda **kwargs: self.provider()), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
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
        with patch("pdf_translator.translation.routing.ZoneInfo", side_effect=ZoneInfoNotFoundError):
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
