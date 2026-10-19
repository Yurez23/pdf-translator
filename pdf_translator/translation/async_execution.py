"""Cooperative request admission: one durable ledger and budget for all workers."""

import asyncio
import time
import uuid

from ..telemetry import current_field, emit, span
from .execution import minute_wait, retry_delay
from .models import ProviderError, RequestLimitReached, TranslationError


class AsyncRequestController:
    def __init__(self, policy, ledger, progress=None, *, clock=time.time, sleep=asyncio.sleep):
        self.policy, self.ledger = policy, ledger
        self.progress, self.clock, self.sleep = progress or (lambda _: None), clock, sleep
        self.calls = 0
        self._admission = asyncio.Lock()
        self._closed = False
        self._stopped = asyncio.Event()

    def stop(self):
        """Do not start new attempts; already in-flight calls may finish and save."""
        self._closed = True
        self._stopped.set()

    async def _wait(self, seconds):
        sleeper = asyncio.create_task(self.sleep(seconds))
        stopped = asyncio.create_task(self._stopped.wait())
        try:
            await asyncio.wait((sleeper, stopped), return_when=asyncio.FIRST_COMPLETED)
            if self._closed:
                raise RequestLimitReached("Новые запросы остановлены; сохранённый прогресс доступен через --resume.")
            await sleeper
        finally:
            sleeper.cancel()
            stopped.cancel()
            await asyncio.gather(sleeper, stopped, return_exceptions=True)

    async def call(self, operation, *, tokens=0, label, before_attempt=None):
        if type(tokens) is not int or tokens < 0:
            raise TranslationError("Резерв токенов должен быть неотрицательным целым числом.")
        if tokens > self.policy.tokens_per_minute:
            raise TranslationError("Один запрос превышает tokens_per_minute с резервом ответа. Уменьшите пакет/ответ или уточните квоту.")
        for attempt in range(self.policy.max_retries + 1):
            while True:
                async with self._admission:
                    if self._closed:
                        raise RequestLimitReached("Новые запросы остановлены; сохранённый прогресс доступен через --resume.")
                    if self.calls >= self.policy.max_requests:
                        emit("api.budget_exhausted", "WARNING", calls=self.calls)
                        raise RequestLimitReached("Достигнут лимит API-запросов этого запуска.")
                    now = self.clock()
                    wait = minute_wait(self.ledger.recent(now), now, tokens, self.policy)
                    if not wait:
                        if before_attempt is not None:
                            before_attempt()
                        # No await between checking, persisting and incrementing.
                        self.ledger.reserve(now, tokens)
                        self.calls += 1
                        call_number = self.calls
                        break
                self.progress(f"Ожидание минутной квоты: {wait:.1f} с.")
                with span("api.wait", reason="minute", delay_seconds=wait):
                    await self._wait(wait)
            self.progress(f"API {call_number}/{self.policy.max_requests}: {label}.")
            request_id = uuid.uuid4().hex
            try:
                with span("api.request", request_id=request_id,
                          request_kind=current_field("request_kind", "external"),
                          model=current_field("model", self.policy.model), attempt=attempt + 1,
                          max_attempts=self.policy.max_retries + 1, calls=call_number,
                          token_reservation=tokens, timeout_seconds=self.policy.timeout_seconds):
                    return await operation()
            except ProviderError as error:
                delay = retry_delay(error, attempt, self.policy)
                if delay is None:
                    raise
                self.progress(f"Временная ошибка; повтор через {delay:.1f} с.")
                emit("api.retry", "WARNING", request_id=request_id, attempt=attempt + 1,
                     delay_seconds=delay, status_code=error.status_code)
                with span("api.retry_wait", delay_seconds=delay):
                    await self._wait(delay)
        raise AssertionError("unreachable")
