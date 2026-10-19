"""Offline, content-free observability contracts for CLI and both pipelines."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pdf_translator.cli import main as convert_main
from pdf_translator.converter import convert_pdf
from pdf_translator.logging_setup import JsonlSink
from pdf_translator.telemetry import emit, fields, log_run, span
from pdf_translator.translation import TranslationOptions, plan_translation, translate_file
from pdf_translator.translation.cli import main as translate_main
from pdf_translator.translation.models import ProviderError, TranslationPaused
from pdf_translator.translation.ports import BatchTooLarge, ProviderIdentity
from tests.support import controller_for, allow_asyncio_self_pipe


class MemorySink:
    def __init__(self):
        self.events = []

    def write(self, event):
        self.events.append(event)


class LoggingCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.logs = self.root / "logs"
        self.source = self.root / "private-document.txt"
        self.source.write_text("A private paragraph about 20 points.\n\nA second private paragraph.", encoding="utf-8")
        self.options = TranslationOptions(model="offline", requests_per_minute=1000,
                                          tokens_per_minute=10000000, max_requests=100, max_retries=0)
        for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("Live network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch("pdf_translator.logging_setup.default_log_directory", return_value=self.logs)
        guard.start()
        self.addCleanup(guard.stop)

        self.enterContext(allow_asyncio_self_pipe())

    def read_logs(self):
        return [json.loads(line) for path in sorted(self.logs.glob("*.jsonl"))
                for line in path.read_text(encoding="utf-8").splitlines()]

    def invoke(self, function, args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = function(args)
        return code, output.getvalue(), errors.getvalue()


class EventTests(LoggingCase):
    def test_timings_use_monotonic_clock_and_utc_wall_clock_is_not_duration(self):
        sink, mono = MemorySink(), [100.0]
        with log_run(sink, monotonic=lambda: mono[0], utcnow=lambda: datetime(2026, 10, 19, tzinfo=timezone.utc)):
            with span("test.operation", batch_id="b000001"):
                mono[0] += 0.25
        event = next(e for e in sink.events if e["event"] == "test.operation.finished")
        self.assertEqual(event["duration_ms"], 250)
        self.assertEqual(event["status"], "success")
        self.assertEqual(event["batch_id"], "b000001")
        self.assertTrue(all(e["timestamp"].endswith("+00:00") for e in sink.events))

    def test_allowlist_never_logs_content_exception_messages_labels_or_credentials(self):
        sink = MemorySink()
        secret = "AIza" + "X" * 35
        with log_run(sink):
            emit("test.fields", text=secret, key=secret, url="https://private?key=" + secret,
                 model=secret, source_language=secret, label=secret, status_code=504,
                 input_tokens=0, thinking_tokens=None, total_tokens=float("nan"))
            with self.assertRaises(ValueError):
                with span("test.failure"):
                    raise ValueError(secret + " " + self.source.read_text(encoding="utf-8"))
        serialized = json.dumps(sink.events)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("private paragraph", serialized)
        self.assertNotIn("https://private", serialized)
        event = next(e for e in sink.events if e["event"] == "test.fields")
        self.assertEqual(event["input_tokens"], 0)
        self.assertNotIn("thinking_tokens", event)
        self.assertNotIn("total_tokens", event)
        self.assertEqual(sink.events[-2]["level"], "ERROR")

    def test_cancellation_and_pauses_are_distinct_and_rethrown(self):
        for error, status in ((KeyboardInterrupt(), "cancelled"), (TranslationPaused("private reason"), "paused")):
            sink = MemorySink()
            with self.subTest(status=status), self.assertRaises(type(error)):
                with log_run(sink):
                    raise error
            self.assertEqual(sink.events[-1]["status"], status)
            self.assertEqual(sink.events[-1]["level"], "WARNING")
            self.assertNotIn("private reason", json.dumps(sink.events))

    def test_provider_subclasses_keep_safe_error_classification(self):
        class CustomProviderError(ProviderError):
            pass
        sink = MemorySink()
        with self.assertRaises(CustomProviderError), log_run(sink):
            raise CustomProviderError("private details", status_code=503)
        self.assertEqual(sink.events[-1]["error_code"], "provider")
        self.assertEqual(sink.events[-1]["status_code"], 503)

    def test_sink_failure_is_visible_once_and_does_not_repeat_operation(self):
        sink = SimpleNamespace(write=lambda _: (_ for _ in ()).throw(OSError("sensitive path")))
        errors = io.StringIO()
        performed = []
        with contextlib.redirect_stderr(errors), log_run(sink):
            performed.append(1)
            emit("test.next")
        self.assertEqual(performed, [1])
        self.assertEqual(errors.getvalue().count("logging_unavailable"), 1)
        self.assertNotIn("sensitive path", errors.getvalue())

    def test_nested_and_sequential_runs_do_not_leak_context(self):
        first, second = MemorySink(), MemorySink()
        with log_run(first):
            with fields(batch_id="b000001"):
                with log_run(second):
                    emit("test.nested")
                emit("test.outer")
        emit("test.outside")
        self.assertNotEqual(first.events[0]["run_id"], second.events[0]["run_id"])
        self.assertNotIn("batch_id", second.events[1])
        self.assertEqual(first.events[-2]["batch_id"], "b000001")
        self.assertEqual(first.events[-1]["event"], "run.finished")

    def test_concurrent_scopes_and_json_lines_do_not_mix(self):
        with JsonlSink(self.logs) as sink:
            with log_run(sink):
                contexts = [copy_context() for _ in range(5)]
                def worker(index):
                    with span("test.worker", batch_id=f"b{index + 1:06d}"):
                        for _ in range(40):
                            emit("test.progress", page=index + 1)
                with ThreadPoolExecutor(max_workers=5) as pool:
                    list(pool.map(lambda pair: pair[1].run(worker, pair[0]), enumerate(contexts)))
        events = self.read_logs()
        self.assertEqual(len(events), 212)
        self.assertEqual([e["sequence"] for e in events], list(range(1, 213)))
        self.assertEqual(len({e["run_id"] for e in events}), 1)
        for event in events:
            if event["event"] == "test.progress":
                self.assertEqual(event["batch_id"], f"b{event['page']:06d}")

    def test_no_root_handlers_or_third_party_messages_are_captured(self):
        import logging
        before = list(logging.getLogger().handlers)
        sink = MemorySink()
        with contextlib.redirect_stderr(io.StringIO()), log_run(sink):
            logging.getLogger("httpx").warning("private-provider-message")
            emit("test.safe")
        self.assertEqual(before, logging.getLogger().handlers)
        self.assertNotIn("private-provider-message", json.dumps(sink.events))


class FileTests(LoggingCase):
    def test_active_log_remains_readable_and_lock_does_not_extend_file(self):
        with JsonlSink(self.logs) as sink:
            sink.write({"event": "active"})
            self.assertEqual(json.loads(sink.path.read_text(encoding="utf-8")), {"event": "active"})
            self.assertLess(sink.path.stat().st_size, 100)

    def test_directory_link_guard_is_checked_without_symlink_privilege(self):
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == self.logs or original(path)):
            with self.assertRaises(OSError):
                JsonlSink(self.logs)
        self.assertFalse(self.logs.exists())

    def test_exclusive_names_never_overwrite_an_existing_file(self):
        frozen = SimpleNamespace(now=lambda _: datetime(2026, 10, 19, tzinfo=timezone.utc))
        with patch("pdf_translator.jsonl.datetime", frozen), patch("pdf_translator.jsonl.uuid.uuid4", return_value=SimpleNamespace(hex="a" * 32)):
            with JsonlSink(self.logs) as first:
                first.write({"event": "first"})
                path = first.path
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                JsonlSink(self.logs)
            self.assertEqual(path.read_bytes(), before)

    def test_retention_does_not_delete_an_active_other_process_log(self):
        script = ("import sys; from pdf_translator.logging_setup import JsonlSink; "
                  "sink=JsonlSink(sys.argv[1]); sink.write({'event':'child'}); "
                  "print(str(sink.path),flush=True); input(); sink.close()")
        child = subprocess.Popen([sys.executable, "-c", script, str(self.logs)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            name = child.stdout.readline().strip()
            self.assertTrue(name)
            path = Path(name)
            self.assertEqual(path.parent.resolve(), self.logs.resolve())
            os.utime(path, (1, 1))
            with JsonlSink(self.logs, max_files=1, retention_days=1):
                self.assertTrue(path.exists())
            _, errors = child.communicate("\n", timeout=10)
            self.assertEqual(child.returncode, 0, errors)
            with JsonlSink(self.logs, max_files=1, retention_days=1):
                self.assertFalse(path.exists())
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()

    def test_rotation_is_bounded_and_each_line_remains_valid_json(self):
        with JsonlSink(self.logs, max_bytes=800, parts=3) as sink, log_run(sink):
            for i in range(100):
                emit("test.event", page=i)
        paths = list(self.logs.glob("*.jsonl"))
        self.assertLessEqual(len(paths), 3)
        self.assertTrue(all(p.stat().st_size <= 800 for p in paths))
        self.assertEqual(self.read_logs()[-1]["event"], "run.finished")

    def test_retention_skips_active_writers_and_unrelated_files(self):
        self.logs.mkdir()
        unrelated = self.logs / "notes.txt"
        unrelated.write_text("keep me", encoding="utf-8")
        first = JsonlSink(self.logs)
        self.addCleanup(first.close)
        first.write({"event": "synthetic"})
        old = first.path
        os.utime(old, (1, 1))
        with JsonlSink(self.logs, max_files=1, retention_days=1):
            self.assertTrue(old.exists())
        first.close()
        with JsonlSink(self.logs, max_files=1, retention_days=1):
            self.assertFalse(old.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me")

    def test_foreign_symlink_is_not_followed_or_pruned(self):
        self.logs.mkdir()
        target = self.root / "important.txt"
        target.write_text("keep", encoding="utf-8")
        link = self.logs / ("run-20200101T000000Z-" + "a" * 32 + "-000001.jsonl")
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Symlink privilege unavailable")
        with JsonlSink(self.logs, max_files=1):
            self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        with self.assertRaises(OSError):
            JsonlSink(link)


class EchoProvider:
    identity = ProviderIdentity("offline", "v1")

    def prepare(self, options):
        return self

    def translate(self, request, *, executor):
        return executor.call(lambda: {i.id: "Перевод " + i.text for i in request.items}, label="private label")


class PipelineTests(LoggingCase):
    def test_api_has_no_implicit_files_and_logging_does_not_change_plan(self):
        before = plan_translation(self.source, self.options, provider=EchoProvider.identity)
        sink = MemorySink()
        with log_run(sink):
            after = plan_translation(self.source, self.options, provider=EchoProvider.identity)
        self.assertEqual(before, after)
        self.assertFalse(self.logs.exists())
        self.assertTrue(any(e["event"] == "translation.plan.finished" for e in sink.events))

    def test_translation_batch_validation_storage_warnings_and_resume(self):
        class NumberProvider(EchoProvider):
            def translate(self, request, *, executor):
                return executor.call(lambda: {i.id: "Перевод " + i.text.replace("20", "21") for i in request.items}, label="private")
        sink = MemorySink()
        out = self.root / "translated"
        with log_run(sink):
            result = translate_file(self.source, output_dir=out, options=self.options, provider=NumberProvider(), allow_network=True)
        self.assertEqual(result.status, "complete")
        names = [e["event"] for e in sink.events]
        for name in ("translation.plan.finished", "translation.batch.finished", "api.request.finished",
                     "storage.checkpoint_write.finished", "translation.validate.finished", "storage.export.finished"):
            self.assertIn(name, names)
        warning = next(e for e in sink.events if e["event"] == "translation.warning" and e.get("code") == "numbers_changed")
        self.assertEqual(warning["level"], "WARNING")
        self.assertIn("segment_id", warning)
        self.assertNotIn("private paragraph", json.dumps(sink.events))
        sink = MemorySink()
        with log_run(sink):
            again = translate_file(self.source, output_dir=out, options=self.options, provider=NumberProvider(),
                                   allow_network=True, resume=True, overwrite=True)
        self.assertEqual(again.requests_made, 0)
        self.assertTrue(any(e["event"] == "translation.resumed" for e in sink.events))
        self.assertFalse(any(e["event"] == "api.request.started" for e in sink.events))

    def test_retry_and_wait_timings_have_attempt_ids_and_no_error_prose(self):
        now = [100.0]
        state = {"calls_total": 0, "recent_requests": []}
        options = replace(self.options, max_retries=1)
        controller = controller_for(options, state, self.root / "state.json", lambda _: None,
                                       clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        attempts, sink = [], MemorySink()
        def operation():
            attempts.append(1)
            now[0] += 0.25
            if len(attempts) == 1:
                raise ProviderError("private raw server message", retryable=True, status_code=504)
            return 42
        with log_run(sink, monotonic=lambda: now[0]), fields(batch_id="b000001", request_kind="generation", model="gemini-test"):
            self.assertEqual(controller.call(operation, tokens=123, label="private label"), 42)
        ended = [e for e in sink.events if e["event"] == "api.request.finished"]
        self.assertEqual([e["attempt"] for e in ended], [1, 2])
        self.assertEqual([e["status"] for e in ended], ["error", "success"])
        self.assertEqual([e["duration_ms"] for e in ended], [250, 250])
        self.assertEqual(ended[0]["status_code"], 504)
        self.assertEqual(len({e["request_id"] for e in ended}), 2)
        self.assertTrue(all(e["timeout_seconds"] == 300 and e["token_reservation"] == 123 for e in ended))
        self.assertTrue(any(e["event"] == "api.retry_wait.finished" and e["duration_ms"] == 2000 for e in sink.events))
        self.assertNotIn("private", json.dumps(sink.events))

    def test_minute_wait_is_not_counted_as_request_duration(self):
        now = [100.0]
        state = {"calls_total": 1, "recent_requests": [{"time": now[0], "tokens": 1}]}
        options = replace(self.options, requests_per_minute=1)
        controller = controller_for(options, state, self.root / "state.json", lambda _: None,
                                       clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay))
        sink = MemorySink()
        with log_run(sink, monotonic=lambda: now[0]):
            controller.call(lambda: None, label="private")
        requests = [e for e in sink.events if e["event"] == "api.request.finished"]
        waits = [e for e in sink.events if e["event"] == "api.wait.finished"]
        self.assertEqual(requests[0]["duration_ms"], 0)
        self.assertGreaterEqual(sum(e["duration_ms"] for e in waits), 60000)

    def test_controller_interrupt_is_logged_and_not_retried(self):
        state = {"calls_total": 0, "recent_requests": []}
        controller = controller_for(self.options, state, self.root / "state.json", lambda _: None)
        sink = MemorySink()
        with self.assertRaises(KeyboardInterrupt), log_run(sink):
            controller.call(lambda: (_ for _ in ()).throw(KeyboardInterrupt()), label="private")
        events = [e for e in sink.events if e["event"] == "api.request.finished"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "cancelled")

    def test_budget_pause_creates_no_fake_api_attempt(self):
        state = {"calls_total": 0, "recent_requests": []}
        controller = controller_for(replace(self.options, max_requests=1), state, self.root / "state.json", lambda _: None)
        sink = MemorySink()
        with log_run(sink):
            controller.call(lambda: None, label="private")
            with self.assertRaises(TranslationPaused):
                with span("test.batch"):
                    controller.call(lambda: self.fail("Budget bypassed"), label="private")
        self.assertEqual(sum(e["event"] == "api.request.started" for e in sink.events), 1)
        paused = next(e for e in sink.events if e["event"] == "test.batch.finished")
        self.assertEqual(paused["status"], "paused")

    def test_batch_split_has_stable_child_ids(self):
        class Limited(EchoProvider):
            def translate(self, request, *, executor):
                if len(request.items) > 1:
                    raise BatchTooLarge("private")
                return super().translate(request, executor=executor)
        sink = MemorySink()
        with log_run(sink):
            translate_file(self.source, output_dir=self.root / "out", options=self.options, provider=Limited(), allow_network=True)
        ids = [e["batch_id"] for e in sink.events if e["event"] == "translation.batch.started"]
        self.assertEqual(ids, ["b000001", "b000001.1", "b000001.2"])

    def test_conversion_success_and_error_are_logged_without_source_text(self):
        from reportlab.pdfgen.canvas import Canvas
        source = self.root / "private.pdf"
        canvas = Canvas(str(source))
        canvas.drawString(72, 720, "Private content should never appear in a log.")
        canvas.save()
        sink = MemorySink()
        with log_run(sink):
            result = convert_pdf(source)
        self.assertIn("Private content", result.text)
        for name in ("conversion.page_extract.finished", "conversion.page_layout.finished", "conversion.render.finished", "conversion.summary"):
            self.assertTrue(any(e["event"] == name for e in sink.events))
        self.assertNotIn("Private content", json.dumps(sink.events))
        sink = MemorySink()
        with self.assertRaises(ValueError), log_run(sink):
            convert_pdf(self.root / "not-found.pdf")
        self.assertEqual(sink.events[-1]["status"], "error")

    def test_cli_default_logs_conversion_and_explicit_disable(self):
        code, _, errors = self.invoke(convert_main, [str(self.root / "not-found.pdf")])
        self.assertEqual(code, 1)
        self.assertIn("Технический лог", errors)
        self.assertEqual(self.read_logs()[-1]["status"], "error")
        before = len(list(self.logs.glob("*.jsonl")))
        self.invoke(convert_main, [str(self.root / "missing.pdf"), "--no-log"])
        self.assertEqual(len(list(self.logs.glob("*.jsonl"))), before)

    def test_translation_plan_default_read_only_but_explicit_log_dir_works(self):
        code, _, _ = self.invoke(translate_main, [str(self.source)])
        self.assertEqual(code, 0)
        self.assertFalse(self.logs.exists())
        code, output, _ = self.invoke(translate_main, [str(self.source), "--log-dir", str(self.logs)])
        self.assertEqual(code, 0)
        self.assertIn("только технический лог", output)
        self.assertEqual(self.read_logs()[-1]["status"], "success")

    def test_logging_setup_failure_stops_before_operation_and_preserves_file(self):
        code, _, errors = self.invoke(translate_main, [str(self.source), "--send", "--log-dir", str(self.source)])
        self.assertEqual(code, 1)
        self.assertIn("не удалось открыть", errors)
        self.assertIn("private paragraph", self.source.read_text(encoding="utf-8"))

    def test_send_cli_without_key_logs_sanitized_failure(self):
        with patch.dict("os.environ", {}, clear=True):
            code, _, _ = self.invoke(translate_main, [str(self.source), "--send"])
        self.assertEqual(code, 1)
        events = self.read_logs()
        self.assertEqual(events[-1]["status"], "error")
        self.assertFalse(any(e["event"] == "api.request.started" for e in events))
        self.assertNotIn("private paragraph", json.dumps(events))

    def test_repeated_converter_warning_codes_do_not_hide_distinct_warnings(self):
        sink = MemorySink()
        with log_run(sink):
            emit("conversion.warning", "WARNING", code="ambiguous_hyphen", pages=(1,))
            emit("conversion.warning", "WARNING", code="ambiguous_hyphen", pages=(1,))
        self.assertEqual(sum(e["event"] == "conversion.warning" for e in sink.events), 2)

    def test_cli_cancellation_and_paused_result_statuses(self):
        from pdf_translator.logging_setup import run_cli
        args = SimpleNamespace(no_log=False, log_dir=self.logs)
        for status in (3, 130):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(run_cli(args, lambda: status, command="translation"), status)
        self.assertEqual({e["status"] for e in self.read_logs() if e["event"] == "run.finished"}, {"paused", "cancelled"})
