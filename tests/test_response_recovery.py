"""Whole-batch response retries, isolation and checkpoint recovery; no live API."""

import asyncio
import contextlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pdf_translator.telemetry import log_run
from pdf_translator.translation import (ResponseValidationError, TranslationError, TranslationOptions,
                                       translate_file, translate_file_async)
from pdf_translator.translation.gemini_format import make_prompt, provider_identity, response_schema
from pdf_translator.translation.models import ProviderError
from pdf_translator.translation.ports import ProviderIdentity
from pdf_translator.translation.validation import validate_translations
from tests.test_translation import LocalCase, OPTIONS
from tests.test_async_translation import AsyncLocalCase


class ScriptedProvider:
    identity = ProviderIdentity("scripted", "v1")

    def __init__(self, behavior=None):
        self.behavior = behavior
        self.calls = []
        self.counts = {}

    def prepare(self, options):
        return self

    def prepare_async(self, options):
        return self

    def answer(self, request):
        ids = tuple(i.id for i in request.items)
        self.calls.append(request)
        self.counts[ids] = self.counts.get(ids, 0) + 1
        if self.behavior:
            value = self.behavior(request, self.counts[ids])
            if value is not None:
                return value
        return {i.id: "Перевод: " + i.text for i in request.items}

    def translate(self, request, *, executor):
        return executor.call(lambda: self.answer(request), label="synthetic response")

    def new_session(self):
        async def translate(request, *, executor):
            async def operation():
                await asyncio.sleep(0)
                return self.answer(request)
            return await executor.call(operation, label="synthetic response")
        return SimpleNamespace(translate=translate)

    async def aclose(self):
        pass


def bad_first(request, attempt):
    if request.items[0].id.startswith("u00000"):
        return {}


class RecoverySyncTests(LocalCase):
    def setUp(self):
        super().setUp()
        self.options = replace(OPTIONS, model="scripted", max_chunk_chars=256)
        self.source.write_text("\n\n".join(f"Paragraph {i}: " + "An ordinary sentence. " * 7 for i in range(4)), encoding="utf-8")

    def run_file(self, provider, **kwargs):
        return translate_file(self.source, output_dir=self.out, options=kwargs.pop("options", self.options),
                              provider=provider, allow_network=True, **kwargs)

    def test_retry_same_whole_request_then_success_without_splitting(self):
        provider = ScriptedProvider(lambda request, attempt: {} if attempt == 1 else None)
        result = self.run_file(provider)
        self.assertEqual((result.status, result.requests_made, result.completed_segments), ("complete", 8, 4))
        for first, second in zip(provider.calls[::2], provider.calls[1::2]):
            self.assertEqual((first.items, first.context, first.glossary), (second.items, second.context, second.glossary))
            self.assertEqual(second.response_issue, "id_mismatch")

    def test_exhausted_response_does_not_stop_later_batches_and_resume_skips_saved(self):
        provider = ScriptedProvider(bad_first)
        result = self.run_file(provider)
        self.assertEqual((result.status, result.completed_segments, result.requests_made), ("incomplete", 3, 5))
        self.assertEqual((len(result.failed_batches), result.failed_batches[0].attempts), (1, 2))
        state = json.loads(Path(result.checkpoint).read_text(encoding="utf-8"))
        self.assertEqual(state["failed_batches"][0]["code"], "id_mismatch")
        self.assertFalse(list(self.out.glob("*.md")))
        next_provider = ScriptedProvider()
        resumed = self.run_file(next_provider, resume=True)
        self.assertEqual((resumed.status, resumed.completed_segments, resumed.requests_made), ("complete", 4, 1))
        self.assertTrue(set(state["translations"]).isdisjoint(i.id for r in next_provider.calls for i in r.items))
        self.assertFalse(json.loads(Path(result.checkpoint).read_text(encoding="utf-8")).get("failed_batches"))

    def test_global_budget_is_not_reset_by_response_retries(self):
        provider = ScriptedProvider(lambda *_: {})
        result = self.run_file(provider, options=replace(self.options, max_requests=1))
        self.assertEqual((result.status, result.requests_made), ("paused", 1))
        self.assertEqual(len(provider.calls), 1)

    def test_zero_response_retries_and_invalid_configuration(self):
        result = self.run_file(ScriptedProvider(bad_first), options=replace(self.options, max_response_retries=0))
        self.assertEqual((result.status, result.requests_made, result.failed_batches[0].attempts), ("incomplete", 4, 1))
        for invalid in (-1, 6, True, 0.5):
            with self.subTest(invalid=invalid), self.assertRaises(TranslationError):
                replace(self.options, max_response_retries=invalid)

    def test_unexpected_failure_is_global_not_silently_isolated(self):
        def fail(*_):
            raise RuntimeError("synthetic code defect")
        with self.assertRaises(RuntimeError):
            self.run_file(ScriptedProvider(fail))
        self.assertFalse(list(self.out.glob("*.lock")))

    def test_failure_persistence_error_is_global(self):
        from pdf_translator.translation.storage import JsonCheckpointStore
        original = JsonCheckpointStore.save
        def save(store, state):
            if state.get("failed_batches"):
                raise OSError("synthetic disk failure")
            return original(store, state)
        with patch.object(JsonCheckpointStore, "save", save), self.assertRaises(OSError):
            self.run_file(ScriptedProvider(bad_first))
        self.assertFalse(list(self.out.glob("*.md")))

    def test_corrupt_failure_metadata_rejected_before_network(self):
        result = self.run_file(ScriptedProvider(bad_first))
        path = Path(result.checkpoint)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["failed_batches"][0]["segment_ids"] = ["unknown"]
        path.write_text(json.dumps(state), encoding="utf-8")
        provider = ScriptedProvider()
        with self.assertRaises(TranslationError):
            self.run_file(provider, resume=True)
        self.assertEqual(provider.calls, [])

    def test_diagnostics_log_only_known_ids_and_counts_not_model_payload(self):
        provider = ScriptedProvider(lambda *_: {"PRIVATE-KEY-AND-SOURCE": "private translation"})
        events = []
        with log_run(SimpleNamespace(write=events.append)):
            result = self.run_file(provider)
        self.assertEqual(result.status, "incomplete")
        failures = [e for e in events if e["event"] == "translation.response_rejected"]
        self.assertEqual(len(failures), 8)
        self.assertTrue(all(e["expected_count"] == 1 and e["received_count"] == 1 and e["missing_count"] == 1 and e["unexpected_count"] == 1 for e in failures))
        self.assertNotIn("PRIVATE-KEY", json.dumps(events))
        self.assertNotIn("private translation", json.dumps(events))

    def test_typed_response_error_recovers_but_number_warnings_do_not_retry(self):
        for code in ("invalid_json", "duplicate_id", "protected_markers", "invalid_text", "response_length"):
            self.out = self.root / code
            def behave(request, attempt):
                if attempt == 1:
                    raise ResponseValidationError(code)
            result = self.run_file(ScriptedProvider(behave))
            self.assertEqual(result.requests_made, 8)
        self.source.write_text("A paragraph with 20 points.", encoding="utf-8")
        self.out = self.root / "warning"
        result = self.run_file(ScriptedProvider(lambda request, attempt: {i.id: "Абзац с 30 очками." for i in request.items}))
        self.assertEqual((result.status, result.requests_made), ("complete", 1))
        self.assertIn("numbers_changed", {w.code for w in result.warnings})

    def test_cli_incomplete_exit_and_resume_only_failed_segments(self):
        from pdf_translator.translation.cli import main
        provider = ScriptedProvider(bad_first)
        args = [str(self.source), "--send", "--output-dir", str(self.out), "--max-chunk-chars", "256",
                "--max-requests", "100", "--requests-per-minute", "1000", "--tokens-per-minute", "1000000"]
        with patch("pdf_translator.translation.cli.GeminiAutoProvider", return_value=provider), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            code = main(args)
        self.assertEqual(code, 4)
        self.assertIn("неполон", output.getvalue())
        events = [json.loads(line) for path in (self.root / "logs").glob("*.jsonl") for line in path.read_text(encoding="utf-8").splitlines()]
        end = next(e for e in events if e["event"] == "run.finished")
        self.assertEqual((end["status"], end["exit_code"]), ("incomplete", 4))
        next_provider = ScriptedProvider()
        with patch("pdf_translator.translation.cli.GeminiAutoProvider", return_value=next_provider), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args + ["--resume"]), 0)
        self.assertEqual(len(next_provider.calls), 1)

    def test_http_retry_and_response_retry_remain_bounded_under_one_budget(self):
        from pdf_translator.translation.execution import RequestController
        self.source.write_text("One ordinary paragraph.", encoding="utf-8")
        def behavior(request, attempt):
            if attempt == 1:
                raise ProviderError("safe temporary failure", status_code=503, retryable=True)
            if attempt == 2:
                return {}
        provider = ScriptedProvider(behavior)
        def controller(policy, ledger, progress):
            return RequestController(policy, ledger, progress, sleep=lambda _: None)
        with patch("pdf_translator.translation.runner.RequestController", side_effect=controller):
            result = self.run_file(provider, options=replace(self.options, max_retries=1))
        self.assertEqual((result.status, result.requests_made, len(provider.calls)), ("complete", 3, 3))

    def test_strict_schema_and_hint_never_inject_previous_output(self):
        from pdf_translator.translation import TranslationItem, TranslationRequest
        request = TranslationRequest((TranslationItem("a", "One", "paragraph"), TranslationItem("b", "Two", "paragraph")), "EN", "RU", {}, "")
        schema = response_schema(request)
        body = schema["properties"]["translations"]
        self.assertEqual(body["required"], ["a", "b"])
        self.assertEqual(set(body["properties"]), {"a", "b"})
        self.assertFalse(body["additionalProperties"])
        self.assertEqual(response_schema(replace(request, items=request.items[:1]))["properties"]["translations"]["required"], ["a"])
        unsafe = replace(request, response_issue="PRIVATE-SOURCE-OR-KEY")
        self.assertNotIn("PRIVATE-SOURCE-OR-KEY", make_prompt(unsafe))
        self.assertNotEqual(provider_identity("strict"), provider_identity("legacy"))


class RecoveryAsyncTests(AsyncLocalCase):
    async def test_async_exhaustion_continues_every_other_batch_and_resumes(self):
        provider = ScriptedProvider(bad_first)
        result = await self.run_file(provider=provider)
        self.assertEqual((result.status, result.completed_segments, result.requests_made), ("incomplete", 11, 13))
        self.assertEqual(len(result.failed_batches), 1)
        self.assertFalse(list(self.out.glob("*.lock")))
        state = self.state()
        next_provider = ScriptedProvider()
        resumed = await self.run_file(provider=next_provider, resume=True)
        self.assertEqual((resumed.status, resumed.requests_made), ("complete", 1))
        self.assertTrue(set(state["translations"]).isdisjoint(i.id for r in next_provider.calls for i in r.items))

    async def test_async_retry_never_splits_and_all_calls_share_one_budget(self):
        provider = ScriptedProvider(lambda request, attempt: {} if attempt == 1 else None)
        with patch("pdf_translator.translation.async_workflow.split_batch", side_effect=AssertionError("response recovery split a batch")):
            result = await self.run_file(provider=provider)
        self.assertEqual((result.status, result.requests_made), ("complete", 24))
        self.assertTrue(all(count == 2 for count in provider.counts.values()))

    async def test_cancel_during_response_retry_preserves_progress_and_cleans_tasks(self):
        retried = asyncio.Event()
        provider = ScriptedProvider()
        def session():
            async def translate(request, *, executor):
                async def operation():
                    if request.response_issue:
                        retried.set()
                        await asyncio.Event().wait()
                    return {}
                return await executor.call(operation, label="cancel retry")
            return SimpleNamespace(translate=translate)
        provider.new_session = session
        task = asyncio.create_task(self.run_file(provider=provider))
        await asyncio.wait_for(retried.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(list(self.out.glob("*.lock")))
        self.assertFalse(list(self.out.glob("*.md")))
