"""Architecture and pre-refactor compatibility contracts; offline only."""

import ast
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pdf_translator.telemetry import error_fields, instrument, log_run, translation_warning
from pdf_translator.translation import (ProviderIdentity, TranslationOptions, TranslationWarning,
                                       TranslationError, plan_translation, translate_file)
from pdf_translator.translation.execution import ExecutionPolicy, RequestController
from pdf_translator.translation.gemini import GeminiProvider
from pdf_translator.translation.gemini_gateway import CatalogEntry, GenerationResult
from pdf_translator.translation.journal import RunJournal, RunState, decode_state, encode_state
from pdf_translator.translation.models import ModelLimits, ProviderError, QuotaError, TranslationPaused
from pdf_translator.translation.model_routing import _RoutingExecutor
from pdf_translator.translation.routing import GeminiAutoProvider
from pdf_translator.translation.workflow import run_translation
from pdf_translator.file_outputs import write_utf8_outputs
from tests.support import MemoryStore
from tests.test_translation import LocalCase


class CompatibilityTests(LocalCase):
    def test_pre_refactor_plan_fingerprints_and_boundaries(self):
        cases = {
            "tiny.txt": ("An ordinary paragraph.\n\nAnother paragraph with 20 points.",
                         "857ec7cad13cacd641365ca524a3d4a43e2d8b373cacf455f880d7a85628f046", 2),
            "book.md": ("# Chapter\n\nA paragraph with **20 points**.\n\n## Section\n\n"
                        "- First item.\n- Second item.\n\n| Name | Value |\n| --- | --- |\n"
                        "| Alpha | 100 |\n\n> Sidebar.\n",
                        "973ba61100494b497334bb11abbc5ca96a7919b0b54b3989d74030dbeb2d5eec", 9),
        }
        for name, (text, fingerprint, count) in cases.items():
            with self.subTest(name=name):
                source = self.root / name
                source.write_text(text, encoding="utf-8", newline="\n")
                plan = plan_translation(source, TranslationOptions(model="offline", max_output_tokens=16384),
                                        provider=ProviderIdentity("offline", "v1"))
                self.assertEqual(plan.fingerprint, fingerprint)
                self.assertEqual(len(plan.segments), count)
                self.assertEqual(plan.batches, (plan.segments,))
                self.assertEqual(plan.manifest["contract_version"], 3)

    def test_pre_refactor_v3_journal_reexports_without_preparing_a_provider(self):
        self.source.write_text("An ordinary paragraph.\n\nAnother paragraph with 20 points.", encoding="utf-8", newline="\n")
        options = TranslationOptions(model="offline", max_output_tokens=16384)
        plan = plan_translation(self.source, options, provider=ProviderIdentity("offline", "v1"))
        self.assertEqual(plan.fingerprint, "857ec7cad13cacd641365ca524a3d4a43e2d8b373cacf455f880d7a85628f046")
        raw = {"version": 3, "fingerprint": plan.fingerprint, "manifest": plan.manifest,
               "translations": {s.id: "Перевод: " + s.text for s in plan.segments},
               "segment_models": {s.id: "offline" for s in plan.segments}, "catalog": [],
               "calls_total": 7, "recent_requests": [{"time": 100, "tokens": 123}]}
        self.assertEqual(encode_state(decode_state(raw, plan, now=100)), raw)
        self.out.mkdir()
        checkpoint = self.out / "book.ru.translation.json"
        checkpoint.write_text(json.dumps(raw), encoding="utf-8")
        provider = SimpleNamespace(identity=ProviderIdentity("offline", "v1"),
                                   prepare=lambda _: self.fail("Completed v3 cache prepared provider"))
        result = translate_file(self.source, output_dir=self.out, options=options, provider=provider,
                                allow_network=True, resume=True)
        self.assertEqual((result.status, result.requests_made), ("complete", 0))
        self.assertEqual(json.loads(checkpoint.read_text(encoding="utf-8")), raw)


class GatewayStub:
    """Single-attempt port double; no inheritance from a production implementation."""

    def __init__(self):
        self.calls, self.closed = [], False

    def close(self):
        self.closed = True

    def list_models_page(self, token=None):
        self.calls.append(("catalog", token))
        return (CatalogEntry("gemini-test", ("generateContent",), 100000, 65536),
                CatalogEntry("gemini-3.8-pro", ("generateContent",), 100000, 65536)), None

    def model_limits(self, model):
        self.calls.append(("metadata", model))
        return ModelLimits(100000, 65536)

    def count_tokens(self, model, prompt):
        self.calls.append(("count", model))
        return 100

    def generate(self, model, prompt, max_output_tokens, *, response_schema=None):
        self.calls.append(("generate", model))
        data = json.loads(prompt.split("INPUT DATA:\n")[1])
        return GenerationResult(json.dumps({"translations": {x["id"]: "Перевод: " + x["text"]
                                                               for x in reversed(data["segments"])}}), model)


class SessionContractTests(LocalCase):
    def providers(self, suffix, *, gateway_type=GatewayStub, dictionary_type=None):
        from tests.test_translation_ports import DictionaryProvider
        gateways = [gateway_type(), gateway_type()]
        return [(dictionary_type or DictionaryProvider)(), GeminiProvider(gateway_factory=lambda _: gateways[0]),
                GeminiAutoProvider(database_path=self.root / (suffix + ".sqlite3"), project_id="test-project",
                                   gateway_factory=lambda _: gateways[1])]

    def test_shared_contract_for_independent_manual_and_auto_providers(self):
        from tests.test_translation_ports import DictionaryProvider
        gateways = [GatewayStub(), GatewayStub()]
        providers = [DictionaryProvider(), GeminiProvider(gateway_factory=lambda _: gateways[0]),
                     GeminiAutoProvider(database_path=self.root / "availability.sqlite3", project_id="test-project",
                                        gateway_factory=lambda _: gateways[1])]
        models = ["dictionary", "gemini-test", "auto"]
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-only"}):
            for index, (provider, model) in enumerate(zip(providers, models)):
                with self.subTest(model=model):
                    options = TranslationOptions(model=model, requests_per_minute=1000, tokens_per_minute=1000000)
                    before = [tuple(g.calls) for g in gateways]
                    session = provider.prepare(options)
                    self.assertTrue(callable(session.translate))
                    self.assertEqual([tuple(g.calls) for g in gateways], before)
                    out = self.root / str(index)
                    result = translate_file(self.source, output_dir=out, options=options, provider=provider, allow_network=True)
                    text = Path(result.text_path).read_text(encoding="utf-8")
                    self.assertLess(text.index("ordinary"), text.index("Another"))
                    self.assertTrue(all(isinstance(w, TranslationWarning) for w in result.warnings))
                    provider.prepare = lambda _: self.fail("Complete cache needs no preparation")
                    again = translate_file(self.source, output_dir=out, options=options, provider=provider,
                                           allow_network=True, resume=True, overwrite=True)
                    self.assertEqual(again.requests_made, 0)
        for provider in providers[1:]:
            provider.close()
        self.assertTrue(all(g.closed for g in gateways))

    def test_session_settings_and_model_attribution_are_isolated(self):
        gateway = GatewayStub()
        provider = GeminiProvider(gateway_factory=lambda _: gateway)
        a = provider.prepare(TranslationOptions(model="gemini-3.8-pro"))
        b = provider.prepare(TranslationOptions(model="gemini-3.7-flash"))
        from pdf_translator.translation import TranslationRequest, TranslationItem
        request = TranslationRequest((TranslationItem("a", "One", "paragraph"),), "EN", "RU", {}, "")
        executor = SimpleNamespace(call=lambda operation, **_: operation())
        self.assertEqual(a.translate(request, executor=executor).model, "gemini-3.8-pro")
        self.assertEqual(b.translate(request, executor=executor).model, "gemini-3.7-flash")
        self.assertEqual(a.translate(request, executor=executor).model, "gemini-3.8-pro")
        self.assertEqual([v for kind, v in gateway.calls if kind == "metadata"], ["gemini-3.8-pro", "gemini-3.7-flash"])
        with self.assertRaises(AttributeError):
            a.options = b.options
        provider.close()

    def test_all_provider_kinds_share_pause_resume_and_marker_contracts(self):
        from tests.test_translation_ports import DictionaryProvider
        class SingleItem(DictionaryProvider):
            def __init__(self):
                super().__init__(batch_limit=1)
        self.source = self.root / "formatted.md"
        self.source.write_text("First **important** paragraph.\n\nSecond paragraph with 20 points.", encoding="utf-8")
        providers = self.providers("budget", dictionary_type=SingleItem)
        next_providers = self.providers("budget", dictionary_type=SingleItem)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-only"}):
            for index, (provider, next_provider, model) in enumerate(zip(providers, next_providers, ("dictionary", "gemini-test", "auto"))):
                with self.subTest(model=model):
                    options = TranslationOptions(model=model, max_requests=1, requests_per_minute=1000,
                                                 tokens_per_minute=1000000)
                    out = self.root / ("budget-" + str(index))
                    first = translate_file(self.source, output_dir=out, options=options, provider=provider, allow_network=True)
                    self.assertEqual((first.status, first.requests_made), ("paused", 1))
                    self.assertFalse(list(out.glob("*.md")))
                    previous = json.loads(Path(first.checkpoint).read_text(encoding="utf-8"))["translations"]
                    result = translate_file(self.source, output_dir=out, options=replace(options, max_requests=100),
                                            provider=next_provider, allow_network=True, resume=True)
                    self.assertEqual((result.status, result.completed_segments), ("complete", 2))
                    self.assertIn("**important**", Path(result.markdown_path).read_text(encoding="utf-8"))
                    state = json.loads(Path(result.checkpoint).read_text(encoding="utf-8"))
                    self.assertTrue(all(state["translations"][key] == value for key, value in previous.items()))
                    if index == 0:
                        self.assertFalse(set(previous) & {item.id for request in next_provider.requests for item in request.items})
        for provider in [*providers[1:], *next_providers[1:]]:
            provider.close()

    def test_all_provider_kinds_reject_missing_ids_without_accepting_a_batch(self):
        from tests.test_translation_ports import DictionaryProvider
        class MissingDictionary(DictionaryProvider):
            def translate(self, request, *, executor):
                return executor.call(lambda: {}, label="test")
        class MissingGateway(GatewayStub):
            def generate(self, model, prompt, max_output_tokens, *, response_schema=None):
                return GenerationResult('{"translations":[]}', model)
        providers = self.providers("invalid", gateway_type=MissingGateway, dictionary_type=MissingDictionary)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-only"}):
            for index, (provider, model) in enumerate(zip(providers, ("dictionary", "gemini-test", "auto"))):
                with self.subTest(model=model):
                    out = self.root / ("invalid-" + str(index))
                    result = translate_file(self.source, output_dir=out,
                                            options=TranslationOptions(model=model, requests_per_minute=1000, tokens_per_minute=1000000),
                                            provider=provider, allow_network=True)
                    self.assertEqual((result.status, len(result.failed_batches)), ("incomplete", 1))
                    self.assertFalse(list(out.glob("*.lock")))
                    self.assertFalse(list(out.glob("*.md")))
                    state = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8"))
                    self.assertEqual(state["translations"], {})
        for provider in providers[1:]:
            provider.close()

    def test_all_provider_kinds_propagate_unhandled_failures_without_losing_checkpoint(self):
        from tests.test_translation_ports import DictionaryProvider
        error = ProviderError("sanitized", status_code=500)
        class FailedDictionary(DictionaryProvider):
            def translate(self, request, *, executor):
                return executor.call(lambda: (_ for _ in ()).throw(error), label="test")
        class FailedGateway(GatewayStub):
            def generate(self, model, prompt, max_output_tokens, *, response_schema=None):
                raise error
        providers = self.providers("failure", gateway_type=FailedGateway, dictionary_type=FailedDictionary)
        with patch.dict(os.environ, {"GEMINI_API_KEY": "fake-key-only"}):
            for index, (provider, model) in enumerate(zip(providers, ("dictionary", "gemini-test", "auto"))):
                with self.subTest(model=model):
                    out = self.root / ("failure-" + str(index))
                    with self.assertRaises(ProviderError) as caught:
                        translate_file(self.source, output_dir=out, options=TranslationOptions(model=model),
                                       provider=provider, allow_network=True)
                    self.assertIs(caught.exception, error)
                    self.assertFalse(list(out.glob("*.lock")))
                    state = json.loads(next(out.glob("*.json")).read_text(encoding="utf-8"))
                    self.assertEqual(state["translations"], {})
                    self.assertGreater(state["calls_total"], 0)
        for provider in providers[1:]:
            provider.close()

    def test_manual_service_failures_still_propagate_without_switching_models(self):
        for code in (503, 504):
            error = ProviderError("sanitized", status_code=code)
            class FailedGateway(GatewayStub):
                def generate(self, model, prompt, max_output_tokens, *, response_schema=None):
                    self.calls.append(("generate", model))
                    raise error
            gateway = FailedGateway()
            provider = GeminiProvider(gateway_factory=lambda _: gateway)
            try:
                with self.subTest(code=code), self.assertRaises(ProviderError) as caught:
                    translate_file(self.source, output_dir=self.root / str(code),
                                   options=TranslationOptions(model="gemini-test"), provider=provider,
                                   allow_network=True)
                self.assertIs(caught.exception, error)
                self.assertEqual([model for action, model in gateway.calls if action == "generate"], ["gemini-test"])
            finally:
                provider.close()

    def test_old_none_return_is_rejected_before_any_translation(self):
        provider = SimpleNamespace(identity=ProviderIdentity("old", "v1"), prepare=lambda _: None)
        with self.assertRaisesRegex(TranslationError, "TranslationSession"):
            translate_file(self.source, output_dir=self.out, options=TranslationOptions(model="old"),
                           provider=provider, allow_network=True)
        self.assertFalse(list(self.out.glob("*.lock")))
        self.assertFalse(list(self.out.glob("*.json")))

    def test_provider_releases_other_sessions_when_one_gateway_close_fails(self):
        error = OSError("close failed")
        closed = []
        first = SimpleNamespace(close=lambda: (_ for _ in ()).throw(error))
        second = SimpleNamespace(close=lambda: closed.append(True))
        gateways = iter((first, second))
        provider = GeminiProvider(gateway_factory=lambda _: next(gateways))
        provider.prepare(TranslationOptions(model="gemini-test"))
        provider.prepare(TranslationOptions(model="gemini-other"))
        with self.assertRaises(OSError) as caught:
            provider.close()
        self.assertIs(caught.exception, error)
        self.assertEqual(closed, [True])
        provider.close()

    def test_auto_quota_suppression_does_not_mutate_original_exception(self):
        original = QuotaError(kind="minute", scope="model", model="gemini-test", retry_after=1, retryable=True)
        executor = SimpleNamespace(call=lambda operation, **_: operation())
        with self.assertRaises(QuotaError) as raised:
            _RoutingExecutor(executor).call(lambda: (_ for _ in ()).throw(original), label="test")
        self.assertIsNot(raised.exception, original)
        self.assertTrue(original.retryable)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual((raised.exception.kind, raised.exception.model, raised.exception.retry_after),
                         (original.kind, original.model, original.retry_after))


class JournalAndExecutionTests(LocalCase):
    def plan(self):
        options = TranslationOptions(model="offline", requests_per_minute=1000, tokens_per_minute=1000000)
        return options, plan_translation(self.source, options, provider=ProviderIdentity("offline", "v1"))

    def test_failed_reservation_cannot_execute_or_consume_a_call(self):
        options, plan = self.plan()
        store = MemoryStore()
        journal = RunJournal.open(plan, store, resume=False)
        store.failure = OSError("disk full")
        controller = RequestController(ExecutionPolicy.from_options(options), journal)
        with self.assertRaises(OSError):
            controller.call(lambda: self.fail("Unpersisted attempt executed"), label="test")
        self.assertEqual(controller.calls, 0)
        store.failure = None
        journal.save()
        self.assertEqual(store.value["calls_total"], 0)

    def test_journal_validates_with_injected_clock_and_exposes_readonly_translations(self):
        options, plan = self.plan()
        raw = encode_state(RunState(plan.fingerprint, plan.manifest))
        raw["recent_requests"] = [{"time": 161, "tokens": 0}]
        with self.assertRaises(TranslationError):
            decode_state(raw, plan, now=100)
        journal = RunJournal.open(plan, MemoryStore(), resume=False)
        with self.assertRaises(TypeError):
            journal.translations["unknown"] = "value"

    def test_failed_batch_save_keeps_only_durable_attempt_and_never_exports(self):
        options, plan = self.plan()
        class FailingStore(MemoryStore):
            def save(self, value):
                if value["translations"]:
                    raise OSError("disk full")
                super().save(value)
        store = FailingStore()
        journal = RunJournal.open(plan, store, resume=False)
        controller = RequestController(ExecutionPolicy.from_options(options), journal)
        provider = SimpleNamespace(prepare=lambda _: SimpleNamespace(translate=lambda request, executor:
            executor.call(lambda: {i.id: "Перевод: " + i.text for i in request.items}, label="test")))
        with self.assertRaises(OSError):
            run_translation(plan, options, provider, journal, controller, resume=False,
                            check_source=lambda: None, export=lambda *_: self.fail("Failed batch exported"), progress=lambda _: None)
        self.assertEqual(dict(journal.translations), {})
        self.assertEqual(store.value["calls_total"], 1)
        self.assertEqual(store.value["translations"], {})

    def test_workflow_runs_entirely_with_memory_ports(self):
        options, plan = self.plan()
        journal = RunJournal.open(plan, MemoryStore(), resume=False)
        controller = RequestController(ExecutionPolicy.from_options(options), journal)
        from tests.test_translation_ports import DictionaryProvider
        exported = []
        result = run_translation(plan, options, DictionaryProvider(), journal, controller, resume=False,
                                 check_source=lambda: None, export=lambda *pair: exported.append(pair), progress=lambda _: None)
        self.assertEqual((result.status, result.completed, controller.calls), ("complete", 2, 1))
        self.assertEqual(len(exported), 1)


class DiagnosticAndArchitectureTests(LocalCase):
    def test_types_not_names_determine_error_categories(self):
        impostor = type("TranslationPaused", (Exception,), {})
        renamed = type("NewName", (TranslationPaused,), {})
        self.assertEqual(error_fields(impostor())["error_code"], "unexpected")
        self.assertEqual(error_fields(renamed())["error_code"], "paused")
        self.assertEqual(error_fields(ProviderError("hidden", error_code="sdk"))["error_code"], "sdk")
        unknown = type("UnknownCode", (TranslationError,), {"diagnostic_code": "typo"})
        self.assertEqual(error_fields(unknown())["error_code"], "unexpected")

    def test_warning_category_is_independent_of_display_language(self):
        for message in ("Numbers changed", "Изменился набор чисел", "private source text"):
            events = []
            warning = TranslationWarning("numbers_changed", message, "u00001.s0000")
            with log_run(SimpleNamespace(write=events.append)):
                translation_warning(warning)
            record = next(e for e in events if e["event"] == "translation.warning")
            self.assertEqual((record["code"], record["segment_id"]), ("numbers_changed", "u00001.s0000"))
            self.assertNotIn(message, json.dumps(events))
            self.assertEqual(str(warning), message)

    def test_generic_instrumentation_does_not_interpret_business_results(self):
        @instrument("test.arbitrary")
        def operation():
            return SimpleNamespace(status="paused")
        events = []
        with log_run(SimpleNamespace(write=events.append)):
            operation()
        record = next(e for e in events if e["event"] == "test.arbitrary.finished")
        self.assertEqual(record["status"], "success")

    def test_dependency_boundaries(self):
        root = Path(__file__).resolve().parents[1] / "pdf_translator"
        rules = {
            "translation/response_recovery.py": {"pathlib", "os", "json", "storage", "planning", "chunking", "gemini", "routing", "availability", "google"},
            "translation/async_workflow.py": {"pathlib", "os", "json", "storage", "gemini", "routing", "availability"},
            "translation/async_execution.py": {"pathlib", "os", "json", "storage", "planning", "gemini"},
            "translation/async_routing.py": {"os", "sqlite3", "availability", "routing", "gemini", "gemini_gateway", "google"},
            "translation/workflow.py": {"pathlib", "os", "json", "storage", "gemini", "routing", "availability"},
            "translation/execution.py": {"pathlib", "os", "json", "storage", "planning", "gemini"},
            "translation/journal.py": {"pathlib", "os", "json", "storage", "planning", "gemini"},
            "translation/model_routing.py": {"os", "sqlite3", "availability", "routing", "gemini", "gemini_gateway", "google"},
            "translation/serialization.py": {"planning", "storage", "gemini"},
            "translation/validation.py": {"planning", "document", "storage", "gemini"},
            "translation/chunking.py": {"document", "gemini", "storage"},
            "models.py": {"layout", "pdfplumber", "pdf_reader"},
            "page_data.py": {"layout", "pdfplumber", "pdf_reader"},
            "captions.py": {"layout", "pdf_reader"},
            "telemetry.py": {"translation", "models", "storage", "gemini", "jsonl"},
        }
        for filename, forbidden in rules.items():
            tree = ast.parse((root / filename).read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(part for alias in node.names for part in alias.name.split("."))
                elif isinstance(node, ast.ImportFrom):
                    imports.update((node.module or "").split("."))
            with self.subTest(module=filename):
                self.assertFalse(imports & forbidden, imports & forbidden)


class OutputMechanismTests(LocalCase):
    def test_new_outputs_roll_back_without_deleting_a_competing_file(self):
        first, second = self.root / "a.md", self.root / "b.txt"
        def compete(target):
            if target == second:
                second.write_text("foreign", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            write_utf8_outputs({first: "one", second: "two"}, overwrite=False, prefix=".test-", before_publish=compete)
        self.assertFalse(first.exists())
        self.assertEqual(second.read_text(encoding="utf-8"), "foreign")
        self.assertFalse(list(self.root.glob("*.tmp")))
    def test_overwrite_remains_per_file_not_a_claimed_group_transaction(self):
        first, second = self.root / "a.md", self.root / "b.txt"
        first.write_text("old-a", encoding="utf-8")
        second.write_text("old-b", encoding="utf-8")
        def fail_second(target):
            if target == second:
                raise OSError("disk full")
        with self.assertRaises(OSError):
            write_utf8_outputs({first: "new-a", second: "new-b"}, overwrite=True, prefix=".test-", before_publish=fail_second)
        self.assertEqual(first.read_text(encoding="utf-8"), "new-a")
        self.assertEqual(second.read_text(encoding="utf-8"), "old-b")
        self.assertFalse(list(self.root.glob("*.tmp")))
