"""Vendor-independent in-memory adapters for execution and persistence tests."""

from contextlib import contextmanager, nullcontext
import socket
from unittest.mock import patch

from pdf_translator.translation.execution import ExecutionPolicy, RequestController
from pdf_translator.translation.journal import RequestRecord, RunJournal, RunState

_real_socketpair, _real_connect = socket.socketpair, socket.socket.connect


@contextmanager
def allow_asyncio_self_pipe():
    """Windows asyncio needs socketpair even with all external I/O mocked.

    Restore connect only inside stdlib's loopback socketpair construction.
    Provider traffic outside that construction remains forbidden by test guards.
    """
    def pair(*args, **kwargs):
        with patch.object(socket.socket, "connect", _real_connect):
            return _real_socketpair(*args, **kwargs)
    with patch.object(socket, "socketpair", pair):
        yield


class MemoryStore:
    def __init__(self, backing=None):
        self.value = backing if backing is not None else {}
        self.failure = None

    def lock(self):
        return nullcontext()

    def load(self):
        return self.value

    def save(self, value):
        if self.failure is not None:
            raise self.failure
        self.value.clear()
        self.value.update(value)


def controller_for(options, state, _checkpoint=None, progress=None, **kwargs):
    """Legacy test setup shape, now exercising the real controller without files."""
    initial = RunState("fixture", {}, calls_total=state.get("calls_total", 0),
                       recent_requests=tuple(RequestRecord(**r) for r in state.get("recent_requests", ())))
    journal = RunJournal(initial, MemoryStore(state))
    return RequestController(ExecutionPolicy.from_options(options), journal, progress, **kwargs)
