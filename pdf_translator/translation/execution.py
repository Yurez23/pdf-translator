"""Request execution policies; independent of JSON, paths, SDK and translation state."""

import time
import uuid
from dataclasses import dataclass

from ..telemetry import current_field, emit, span
from .journal import AttemptLedger
from .models import ProviderError, RequestLimitReached, TranslationError


@dataclass(frozen=True)
class ExecutionPolicy:
    model: str
    requests_per_minute: int
    tokens_per_minute: int
    max_requests: int
    max_retries: int
    timeout_seconds: float

    @classmethod
    def from_options(cls, options):
        return cls(**{name: getattr(options, name) for name in cls.__dataclass_fields__})


def minute_wait(recent, now, tokens, policy):
    if len(recent) < policy.requests_per_minute and sum(r.tokens for r in recent) + tokens <= policy.tokens_per_minute:
        return 0
    return min(60.0, max(0.05, recent[0].time + 60.05 - now))


def retry_delay(error, attempt, policy):
    if not error.retryable or attempt >= policy.max_retries:
        return None
    delay = max(error.retry_after, min(30.0, 2.0 ** (attempt + 1)))
    return delay if delay <= 60 else None


class RequestController:
    """One budget for ALL external calls; ledger durably reserves before execution."""

    def __init__(self, policy: ExecutionPolicy, ledger: AttemptLedger, progress=None, *, clock=time.time, sleep=time.sleep):
        self.policy, self.ledger = policy, ledger
        self.progress, self.clock, self.sleep = progress or (lambda _: None), clock, sleep
        self.calls = 0

    def call(self, operation, *, tokens: int = 0, label: str):
        if type(tokens) is not int or tokens < 0:
            raise TranslationError("Резерв токенов должен быть неотрицательным целым числом.")
        if tokens > self.policy.tokens_per_minute:
            raise TranslationError("Один запрос превышает tokens_per_minute с резервом ответа. Уменьшите пакет/ответ или уточните квоту.")
        for attempt in range(self.policy.max_retries + 1):
            if self.calls >= self.policy.max_requests:
                emit("api.budget_exhausted", "WARNING", calls=self.calls)
                raise RequestLimitReached("Достигнут лимит API-запросов этого запуска.")
            while True:
                now = self.clock()
                wait = minute_wait(self.ledger.recent(now), now, tokens, self.policy)
                if not wait:
                    break
                self.progress(f"Ожидание минутной квоты: {wait:.1f} с.")
                with span("api.wait", reason="minute", delay_seconds=wait):
                    self.sleep(wait)
            self.ledger.reserve(self.clock(), tokens)
            self.calls += 1
            self.progress(f"API {self.calls}/{self.policy.max_requests}: {label}.")
            request_id = uuid.uuid4().hex
            try:
                with span("api.request", request_id=request_id,
                          request_kind=current_field("request_kind", "external"),
                          model=current_field("model", self.policy.model),
                          attempt=attempt + 1, max_attempts=self.policy.max_retries + 1,
                          calls=self.calls, token_reservation=tokens, timeout_seconds=self.policy.timeout_seconds):
                    return operation()
            except ProviderError as error:
                delay = retry_delay(error, attempt, self.policy)
                if delay is None:
                    raise
                self.progress(f"Временная ошибка; повтор через {delay:.1f} с.")
                emit("api.retry", "WARNING", request_id=request_id, attempt=attempt + 1,
                     delay_seconds=delay, status_code=error.status_code)
                with span("api.retry_wait", delay_seconds=delay):
                    self.sleep(delay)
        raise AssertionError("unreachable")
