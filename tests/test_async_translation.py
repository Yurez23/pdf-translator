"""Concurrent translation contracts. Synthetic data, mocked transports, no live API."""

import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pdf_translator.telemetry import log_run
from pdf_translator.translation import (BatchTooLarge, ProviderIdentity, TranslationError,
                                       TranslationOptions, TranslationResponse, plan_translation,
                                       translate_file, translate_file_async)
from pdf_translator.translation.async_execution import AsyncRequestController
from pdf_translator.translation.availability import AvailabilityCache
from pdf_translator.translation.cli import main
from pdf_translator.translation.execution import ExecutionPolicy
from pdf_translator.translation.gemini import GeminiProvider
from pdf_translator.translation.gemini_gateway import CatalogEntry, GenerationResult
from pdf_translator.translation.journal import RunJournal, RunState
from pdf_translator.translation.models import ProviderError, QuotaError, RequestLimitReached
from pdf_translator.translation.routing import GeminiAutoProvider
from tests.support import MemoryStore
from tests.test_translation import markdown_it


class AsyncProvider:
    identity = ProviderIdentity("async-fixture", "v1")

    def __init__(self):
        self.active, self.peak, self.sessions = 0, 0, []
        self.sent, self.finished = [], []
        self.batch_limit = 100
        self.operation = None

    def prepare_async(self, options):
        self.options = options
        return self

    def new_session(self):
        provider = self
        session = SimpleNamespace()
        self.sessions.append(session)

        async def translate(request, *, executor):
            if len(request.items) > provider.batch_limit:
                raise BatchTooLarge("synthetic input limit")

            async def generate():
                ids = tuple(item.id for item in request.items)
                provider.sent.extend(ids)
                provider.active += 1
                provider.peak = max(provider.peak, provider.active)
                try:
                    if provider.operation:
                        await provider.operation(request)
                    else:
                        # Reverse completion order in each group of five.
                        await asyncio.sleep(0.003 * (6 - int(ids[0][1:6]) % 5))
                    provider.finished.extend(ids)
                    return TranslationResponse({item.id: "Перевод: " + item.text for item in reversed(request.items)},
                                               "gemini-test-001")
                finally:
                    provider.active -= 1
            return await executor.call(generate, tokens=100, label="synthetic generation")
        session.translate = translate
        return session


@unittest.skipUnless(markdown_it, "Install translation/dev extra for Markdown tests")
class AsyncLocalCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "chapter.md"
        self.source.write_text("\n\n".join(f"Paragraph {i}: " + "A complete sentence. " * 8 for i in range(12)), encoding="utf-8")
        self.out = self.root / "output"
        self.options = TranslationOptions(model="offline", max_chunk_chars=256, max_requests=100,
                                          requests_per_minute=1000, tokens_per_minute=10000000, max_retries=0)
        self.provider = AsyncProvider()
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("Live network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)

    async def run_file(self, **kwargs):
        return await translate_file_async(self.source, output_dir=self.out,
                                          options=kwargs.pop("options", self.options),
                                          provider=kwargs.pop("provider", self.provider), allow_network=True, **kwargs)

    def state(self):
        return json.loads((self.out / "chapter.ru.translation.json").read_text(encoding="utf-8"))


class AsyncTranslationTests(AsyncLocalCase):
    async def test_five_active_out_of_order_but_exports_are_in_source_order(self):
        events = []
        with log_run(SimpleNamespace(write=events.append)):
            result = await self.run_file()
        self.assertEqual((result.status, result.completed_segments, self.provider.peak), ("complete", 12, 5))
        self.assertEqual(len(self.provider.sessions), 5)
        self.assertNotEqual(self.provider.finished, self.provider.sent)
        self.assertEqual(len(self.provider.sent), len(set(self.provider.sent)))
        text = Path(result.text_path).read_text(encoding="utf-8")
        self.assertEqual([text.index(f"Paragraph {i}:") for i in range(12)], sorted(text.index(f"Paragraph {i}:") for i in range(12)))
        state = self.state()
        self.assertEqual((state["version"], state["calls_total"]), (3, 12))
        self.assertEqual(set(state["segment_models"].values()), {"gemini-test-001"})
        starts = [e for e in events if e["event"] == "api.request.started"]
        self.assertEqual(len({e["request_id"] for e in starts}), 12)
        self.assertEqual(len({e["batch_id"] for e in starts}), 12)
        self.assertNotIn("Paragraph", json.dumps(events))
        self.assertFalse(list(self.out.glob("*.lock")))

    async def test_configurable_concurrency_and_validation(self):
        result = await self.run_file(options=replace(self.options, max_concurrency=2))
        self.assertEqual((result.status, self.provider.peak), ("complete", 2))
        for invalid in (0, 6, True, 1.2):
            with self.subTest(invalid=invalid), self.assertRaises(TranslationError):
                replace(self.options, max_concurrency=invalid)

    async def test_budget_is_global_and_resume_skips_every_saved_segment(self):
        first = await self.run_file(options=replace(self.options, max_requests=3))
        self.assertEqual((first.status, first.requests_made, first.completed_segments), ("paused", 3, 3))
        self.assertFalse(Path(self.out / "chapter.ru.md").exists())
        saved = set(self.state()["translations"])
        next_provider = AsyncProvider()
        result = await self.run_file(provider=next_provider, resume=True, options=replace(self.options, max_concurrency=1))
        self.assertEqual(result.status, "complete")
        self.assertTrue(saved.isdisjoint(next_provider.sent))
        self.assertEqual(self.state()["calls_total"], 12)

    async def test_error_drains_successful_inflight_without_starting_new_batches(self):
        gate = asyncio.Event()
        async def operate(request):
            if request.items[0].id.startswith("u00000"):
                await asyncio.sleep(0.01)
                gate.set()
                raise TranslationError("synthetic MAX_TOKENS; no fallback")
            await gate.wait()
            await asyncio.sleep(0.01)
        self.provider.operation = operate
        with self.assertRaisesRegex(TranslationError, "MAX_TOKENS"):
            await self.run_file()
        self.assertEqual(len(self.provider.sent), 5)
        self.assertEqual(len(self.state()["translations"]), 4)
        self.assertEqual(self.provider.active, 0)
        self.assertFalse(list(self.out.glob("*.md")))
        self.assertFalse(list(self.out.glob("*.lock")))
        next_provider = AsyncProvider()
        result = await self.run_file(provider=next_provider, resume=True)
        self.assertEqual(result.status, "complete")
        self.assertTrue(set(self.provider.finished).isdisjoint(next_provider.sent))

    async def test_cancel_cleans_workers_locks_and_preserves_accepted_progress(self):
        accepted = asyncio.Event()
        async def operate(request):
            if request.items[0].id.startswith("u00000"):
                return
            await accepted.wait()
            await asyncio.Event().wait()
        self.provider.operation = operate
        def progress(value):
            if value.startswith("Сохранён"):
                accepted.set()
        task = asyncio.create_task(self.run_file(progress=progress))
        await asyncio.wait_for(accepted.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((len(self.state()["translations"]), self.provider.active), (1, 0))
        self.assertFalse(list(self.out.glob("*.lock")))
        self.assertFalse(list(self.out.glob("*.md")))
        await self.run_file(provider=AsyncProvider(), resume=True)

    async def test_cancelled_provider_worker_does_not_strand_pending_queue(self):
        async def cancel(request):
            raise asyncio.CancelledError()
        self.provider.operation = cancel
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(self.run_file(), 2)
        self.assertEqual(self.provider.active, 0)
        self.assertFalse(list(self.out.glob("*.lock")))

    async def test_split_batches_have_unique_ids_and_no_orphan_heading(self):
        self.options = replace(self.options, max_chunk_chars=16000)
        self.provider.batch_limit = 1
        result = await self.run_file()
        self.assertEqual((result.status, len(set(self.provider.sent))), ("complete", 12))
        self.assertGreater(self.provider.peak, 1)

    async def test_no_optin_and_completed_resume_need_no_preparation(self):
        with self.assertRaises(TranslationError):
            await translate_file_async(self.source, output_dir=self.out, options=self.options, provider=self.provider)
        self.assertFalse(self.out.exists())
        await self.run_file()
        self.provider.prepare_async = lambda _: self.fail("complete cache prepared provider")
        again = await self.run_file(resume=True, overwrite=True)
        self.assertEqual(again.requests_made, 0)

    async def test_concurrency_is_runtime_only_and_old_sync_journal_is_compatible(self):
        from tests.test_translation_ports import DictionaryProvider
        provider = DictionaryProvider()
        options = replace(self.options, model="dictionary")
        old = translate_file(self.source, output_dir=self.out, options=options, provider=provider, allow_network=True)
        next_provider = AsyncProvider()
        next_provider.identity = provider.identity
        next_provider.prepare_async = lambda _: self.fail("cached sync journal prepared async provider")
        result = await self.run_file(provider=next_provider, options=replace(options, max_concurrency=1), resume=True, overwrite=True)
        self.assertEqual((old.status, result.status, result.requests_made), ("complete", "complete", 0))

    async def test_source_change_during_call_blocks_acceptance_and_export(self):
        async def change(request):
            self.source.write_text("Changed source.", encoding="utf-8")
        self.provider.operation = change
        with self.assertRaisesRegex(TranslationError, "Исходник изменился"):
            await self.run_file()
        self.assertEqual(self.state()["translations"], {})
        self.assertFalse(list(self.out.glob("*.md")))

    async def test_structured_markdown_preserves_containers_and_protected_content(self):
        from pdf_translator.translation.document import parse_document
        text = ("# Chapter\n\n" + "A complete sentence. " * 20 +
                "\n\n## Section\n\n- First **important** point.\n- Second `literal` point.\n\n"
                "| Name | Value |\n| --- | --- |\n| Alpha | 100 |\n\n"
                "> ### Sidebar\n>\n> First side paragraph.\n>\n> Another side paragraph.\n")
        self.source.write_text(text, encoding="utf-8")
        result = await self.run_file()
        rendered = Path(result.markdown_path).read_text(encoding="utf-8")
        source = parse_document(text, markdown=True)
        target = parse_document(rendered, markdown=True)
        def shape(nodes):
            return [(n.kind, n.level, len(n.rows), tuple(len(row) for row in n.rows), shape(n.children)) for n in nodes]
        self.assertEqual(shape(source.nodes), shape(target.nodes))
        self.assertEqual(len(source.units), len(target.units))
        self.assertIn("`literal`", rendered)
        self.assertIn("**", rendered)


class AsyncControllerTests(unittest.IsolatedAsyncioTestCase):
    def controller(self, **kwargs):
        self.store = MemoryStore()
        self.journal = RunJournal(RunState("f", {}), self.store)
        options = TranslationOptions(model="offline", requests_per_minute=1000, max_requests=100,
                                     tokens_per_minute=1000, max_retries=0)
        return AsyncRequestController(ExecutionPolicy.from_options(replace(options, **kwargs)), self.journal)

    async def test_simultaneous_reservations_never_exceed_request_budget(self):
        controller = self.controller(max_requests=3)
        sent = []
        async def operation():
            sent.append(controller.calls)
            self.assertEqual(self.store.value["calls_total"], controller.calls)
            await asyncio.sleep(0)
        results = await asyncio.gather(*(controller.call(operation, label="test") for _ in range(10)), return_exceptions=True)
        self.assertEqual((len(sent), controller.calls), (3, 3))
        self.assertEqual(sum(isinstance(r, RequestLimitReached) for r in results), 7)

    async def test_minute_budget_wait_does_not_block_other_tasks_and_can_cancel(self):
        controller = self.controller()
        async def immediate():
            return "ok"
        await controller.call(immediate, tokens=700, label="first")
        waiting = asyncio.create_task(controller.call(immediate, tokens=700, label="second"))
        await asyncio.sleep(0.01)
        self.assertFalse(waiting.done())
        self.assertEqual(controller.calls, 1)
        controller.stop()
        with self.assertRaises(RequestLimitReached):
            await asyncio.wait_for(waiting, 1)

    async def test_rpm_expiry_and_retries_use_one_ledger_with_virtual_clock(self):
        controller = self.controller(requests_per_minute=1, max_retries=1)
        now, waits, calls = [100.0], [], []
        controller.clock = lambda: now[0]
        async def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds
            await asyncio.sleep(0)
        controller.sleep = sleep
        async def operation():
            calls.append(now[0])
            if len(calls) == 1:
                raise ProviderError("safe", retryable=True, status_code=503)
            return "ok"
        self.assertEqual(await controller.call(operation, tokens=700, label="retry"), "ok")
        self.assertEqual(controller.calls, 2)
        self.assertGreaterEqual(calls[1] - calls[0], 60)
        self.assertEqual(waits[0], 2)

    async def test_failed_persistence_prevents_http_and_invalid_tokens_do_not_reserve(self):
        controller = self.controller()
        async def forbidden():
            self.fail("operation before durable reservation")
        for tokens in (-1, True, 2000):
            with self.assertRaises(TranslationError):
                await controller.call(forbidden, tokens=tokens, label="invalid")
        def fail(value):
            raise OSError("synthetic write failure")
        self.store.save = fail
        with self.assertRaises(OSError):
            await controller.call(forbidden, label="failure")
        self.assertEqual(controller.calls, 0)

    async def test_local_admission_guard_precedes_durable_reservation(self):
        controller = self.controller()
        def guard():
            raise TranslationError("local restriction")
        async def forbidden():
            self.fail("request sent despite local restriction")
        with self.assertRaises(TranslationError):
            await controller.call(forbidden, label="guard", before_attempt=guard)
        self.assertEqual((controller.calls, self.journal._state.calls_total), (0, 0))
