"""Content-free event port and context propagation; importing never creates files."""

import math
import re
import sys
import time
import uuid
from asyncio import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from threading import RLock
from typing import Protocol

from .diagnostics import DiagnosticError


class EventSink(Protocol):
    def write(self, record: dict) -> None: ...


_current = ContextVar("pdf_translator_telemetry", default=None)
_scope = ContextVar("pdf_translator_event_scope", default={})
_NUMBERS = frozenset("duration_ms delay_seconds timeout_seconds page pages_total pages_selected units segments batches blocks warnings completed total calls attempt max_attempts token_reservation input_tokens output_tokens thinking_tokens total_tokens cached_tokens source_bytes characters rows cells catalog_size exit_code words tables max_concurrency response_attempt expected_count received_count missing_count unexpected_count".split())
_ENUMS = {
    "status": {"success", "error", "cancelled", "paused", "incomplete", "split", "cached"},
    "response_issue": {"invalid_json", "response_structure", "duplicate_id", "id_mismatch", "invalid_text", "protected_markers", "response_length"},
    "request_kind": {"catalog", "metadata", "count_tokens", "generation", "external"},
    "reason": {"minute", "daily", "zero", "unknown", "unavailable", "output_limit", "busy", "project", "input_limit"},
    "decision": {"join", "keep"},
    "finish_reason": {"STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "OTHER"},
    "provider": {"gemini", "gemini-auto", "external"},
    "command": {"conversion", "translation", "api"},
    "code": {"empty_page", "unmapped_glyph", "table_detection", "complex_table", "diagram_layout", "rotated_text", "ambiguous_hyphen", "heading_depth", "graphic_text", "numbers_changed", "unchanged_text", "container_split", "txt_structure", "literal_preserved", "preview", "unspecified"},
    "error_code": {"cancelled", "paused", "request_limit", "quota", "provider", "network_timeout", "network_transport", "sdk", "input_or_validation", "filesystem", "batch_too_large", "response_validation", "response_exhausted", "unexpected"},
}


def _safe(fields):
    """Allowlist, not recursive redaction: arbitrary strings never reach a sink."""
    result = {}
    for key, value in fields.items():
        if key in _NUMBERS and type(value) in (int, float) and 0 <= value <= 10**15 and math.isfinite(value):
            result[key] = value
        elif key == "status_code" and type(value) is int and 100 <= value <= 599:
            result[key] = value
        elif key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
            result[key] = value
        elif key in ("model", "actual_model") and isinstance(value, str) and re.fullmatch(r"gemini-[a-z0-9.-]{1,110}|auto", value):
            result[key] = value
        elif key == "batch_id" and isinstance(value, str) and len(value) <= 100 and re.fullmatch(r"b\d{6}(?:\.[12])*", value):
            result[key] = value
        elif key == "segment_id" and isinstance(value, str) and re.fullmatch(r"u\d{5}(?:\.s\d{4})?", value):
            result[key] = value
        elif key in ("span_id", "parent_span_id", "request_id") and isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value):
            result[key] = value
        elif key in ("selected", "resume", "overwrite", "retryable") and type(value) is bool:
            result[key] = value
        elif key == "pages" and isinstance(value, (list, tuple)) and all(type(n) is int and n > 0 for n in value):
            result[key] = list(value[:100])
    return result


def error_fields(error):
    code = ("cancelled" if isinstance(error, (KeyboardInterrupt, CancelledError, GeneratorExit)) else
            error.diagnostic_code if isinstance(error, DiagnosticError) else
            "filesystem" if isinstance(error, OSError) else "unexpected")
    if code == "provider" and getattr(error, "error_code", None) in ("network_timeout", "network_transport", "sdk"):
        code = error.error_code
    if code not in _ENUMS["error_code"]:
        code = "unexpected"
    # Never stringify an exception, its arguments, traceback, request or response.
    return _safe({"error_code": code, "status_code": getattr(error, "status_code", None),
                  "retryable": getattr(error, "retryable", None), "response_issue": getattr(error, "code", None),
                  "expected_count": getattr(error, "expected_count", None), "received_count": getattr(error, "received_count", None)})


class Observer:
    def __init__(self, sink: EventSink, *, monotonic=time.perf_counter, utcnow=lambda: datetime.now(timezone.utc)):
        self.sink, self.monotonic, self.utcnow = sink, monotonic, utcnow
        self.run_id = uuid.uuid4().hex
        self._lock, self._sequence, self._failed = RLock(), 0, False
        self._warnings = set()

    def emit(self, event, level="INFO", **fields):
        if self._failed:
            return
        if not re.fullmatch(r"[a-z][a-z_.]{0,63}", event) or level not in ("INFO", "WARNING", "ERROR"):
            return
        with self._lock:
            fields = _safe({**_scope.get(), **fields})
            if event == "translation.warning":
                key = (event, fields.get("code"), fields.get("segment_id"), tuple(fields.get("pages", ())))
                if key in self._warnings:
                    return
                self._warnings.add(key)
            self._sequence += 1
            record = {"schema_version": 1, "timestamp": self.utcnow().isoformat(timespec="milliseconds"),
                      "run_id": self.run_id, "sequence": self._sequence, "level": level, "event": event, **fields}
            try:
                self.sink.write(record)
            except Exception:
                self._failed = True
                try:
                    print("Warning [logging_unavailable]: structured logging stopped; operation continues.", file=sys.stderr)
                except Exception:
                    pass  # Even a closed stderr must not change application behavior.


def emit(event, level="INFO", **fields):
    observer = _current.get()
    if observer is not None:
        observer.emit(event, level, **fields)


def current_field(name, default=None):
    return _scope.get().get(name, default)


@contextmanager
def fields(**values):
    token = _scope.set({**_scope.get(), **_safe(values)})
    try:
        yield
    finally:
        _scope.reset(token)


@contextmanager
def span(event, **values):
    observer = _current.get()
    if observer is None:
        yield {}
        return
    outcome = {}
    parent = _scope.get().get("span_id")
    with fields(**values, span_id=uuid.uuid4().hex, **({"parent_span_id": parent} if parent else {})):
        start = observer.monotonic()
        emit(event + ".started")
        try:
            yield outcome
        except BaseException as error:
            info = error_fields(error)
            status = "cancelled" if info["error_code"] == "cancelled" else "paused" if info["error_code"] in ("paused", "request_limit") else "error"
            emit(event + ".finished", "ERROR" if status == "error" else "WARNING",
                 status=status, duration_ms=max(0, (observer.monotonic() - start) * 1000), **info)
            raise
        else:
            status = outcome.pop("status", "success")
            emit(event + ".finished", "ERROR" if status == "error" else "WARNING" if status in ("paused", "cancelled", "incomplete") else "INFO",
                 status=status, duration_ms=max(0, (observer.monotonic() - start) * 1000), **outcome)


@contextmanager
def log_run(sink: EventSink, *, command="api", monotonic=time.perf_counter, utcnow=lambda: datetime.now(timezone.utc)):
    """Explicit API opt-in. Caller owns the sink; no files or SDK configured here."""
    observer = Observer(sink, monotonic=monotonic, utcnow=utcnow)
    token, scope_token = _current.set(observer), _scope.set({})
    try:
        with span("run", command=command) as outcome:
            yield outcome
    finally:
        _scope.reset(scope_token)
        _current.reset(token)


def instrument(event):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with span(event):
                result = function(*args, **kwargs)
                return result
        return wrapped
    return decorate


def translation_warning(warning):
    """Only explicit diagnostic facts cross this boundary; never display prose."""
    emit("translation.warning", "WARNING", code=warning.code, segment_id=warning.segment_id)
