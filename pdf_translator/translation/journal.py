"""Typed run state, v3 checkpoint codec and narrow persistence/attempt ports."""

import math
import time
import re
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Protocol

from .models import BatchFailure, RESPONSE_ISSUES, TranslationError, TranslationPlan
from .validation import validate_text


def valid_model_id(value):
    return isinstance(value, str) and 0 < len(value) <= 200 and all(c.isprintable() and not c.isspace() for c in value)


def valid_catalog(value):
    return isinstance(value, (list, tuple)) and len(value) <= 10000 and all(valid_model_id(item) for item in value) and len(set(value)) == len(value)


@dataclass(frozen=True)
class RequestRecord:
    time: float
    tokens: int


@dataclass(frozen=True)
class RunState:
    fingerprint: str
    manifest: dict
    translations: dict[str, str] = field(default_factory=dict)
    segment_models: dict[str, str] = field(default_factory=dict)
    catalog: tuple[str, ...] = ()
    calls_total: int = 0
    recent_requests: tuple[RequestRecord, ...] = ()
    failed_batches: tuple[BatchFailure, ...] = ()


def encode_state(state: RunState) -> dict:
    """Preserve the established JSON schema exactly; no implicit migration."""
    result = {"version": 3, "fingerprint": state.fingerprint, "manifest": state.manifest,
            "translations": dict(state.translations), "segment_models": dict(state.segment_models),
            "catalog": list(state.catalog), "calls_total": state.calls_total,
            "recent_requests": [{"time": r.time, "tokens": r.tokens} for r in state.recent_requests]}
    if state.failed_batches:
        result["failed_batches"] = [dict(batch_id=f.batch_id, segment_ids=list(f.segment_ids), code=f.code,
                                         attempts=f.attempts, model=f.model) for f in state.failed_batches]
    return result


def decode_state(value: dict, plan: TranslationPlan, *, now: float) -> RunState:
    if value.get("version") != 3 or value.get("fingerprint") != plan.fingerprint or value.get("manifest") != plan.manifest:
        raise TranslationError("Журнал относится к другому исходнику, адаптеру/его ревизии, модели, глоссарию или плану. Используйте прежние настройки либо новую папку.")
    expected = {segment.id: segment for segment in plan.segments}
    translations = value.get("translations")
    if not isinstance(translations, dict) or not set(translations) <= set(expected):
        raise TranslationError("В журнале есть неизвестные или некорректные сегменты.")
    models = value.get("segment_models")
    if not isinstance(models, dict) or set(models) != set(translations) or any(not valid_model_id(v) for v in models.values()):
        raise TranslationError("В журнале отсутствует или повреждена информация о моделях сегментов.")
    if not valid_catalog(value.get("catalog")):
        raise TranslationError("Некорректный снимок каталога моделей в журнале.")
    for identifier, text in translations.items():
        normalized, _ = validate_text(expected[identifier].text, text, identifier)
        if normalized != text:
            raise TranslationError("Перевод в журнале изменён или имеет неверный формат.")
    if type(value.get("calls_total")) is not int or value["calls_total"] < 0:
        raise TranslationError("Некорректный счётчик запросов в журнале.")
    recent = value.get("recent_requests")
    if not isinstance(recent, list) or len(recent) > 10000:
        raise TranslationError("Некорректная история минутной квоты.")
    for item in recent:
        if (not isinstance(item, dict) or set(item) != {"time", "tokens"}
                or type(item["time"]) not in (int, float) or not math.isfinite(item["time"])
                or item["time"] > now + 60 or item["time"] < 0
                or type(item["tokens"]) is not int or item["tokens"] < 0):
            raise TranslationError("Некорректная запись минутной квоты в журнале.")
    failures, failed_ids = [], set()
    raw_failures = value.get("failed_batches", [])
    if not isinstance(raw_failures, list) or len(raw_failures) > len(expected):
        raise TranslationError("Некорректный список проблемных пакетов в журнале.")
    for item in raw_failures:
        if (not isinstance(item, dict) or set(item) != {"batch_id", "segment_ids", "code", "attempts", "model"}
                or not isinstance(item["batch_id"], str) or not re.fullmatch(r"b\d{6}(?:\.[12])*", item["batch_id"])
                or not isinstance(item["code"], str) or item["code"] not in RESPONSE_ISSUES
                or type(item["attempts"]) is not int or not 1 <= item["attempts"] <= 6
                or not valid_model_id(item["model"]) or not isinstance(item["segment_ids"], list)
                or not item["segment_ids"] or any(not isinstance(i, str) or i not in expected for i in item["segment_ids"])):
            raise TranslationError("Некорректная запись проблемного пакета в журнале.")
        ids = set(item["segment_ids"])
        if len(ids) != len(item["segment_ids"]) or ids & (set(translations) | failed_ids):
            raise TranslationError("Повторённые или уже принятые сегменты проблемного пакета.")
        failed_ids.update(ids)
        failures.append(BatchFailure(item["batch_id"], tuple(item["segment_ids"]), item["code"], item["attempts"], item["model"]))
    return RunState(plan.fingerprint, plan.manifest, dict(translations), dict(models),
                    tuple(value["catalog"]), value["calls_total"], tuple(RequestRecord(**r) for r in recent), tuple(failures))


class CheckpointStore(Protocol):
    def lock(self) -> AbstractContextManager: ...
    def load(self) -> dict: ...
    def save(self, value: dict) -> None: ...


class AttemptLedger(Protocol):
    def recent(self, now: float) -> tuple[RequestRecord, ...]: ...
    def reserve(self, now: float, tokens: int) -> None: ...


class RunJournal:
    """Owns mutable progress; publish accepted state only after persistence succeeds."""

    def __init__(self, state: RunState, store: CheckpointStore):
        self._state, self._store = state, store

    @classmethod
    def open(cls, plan, store, *, resume, clock=time.time):
        state = decode_state(store.load(), plan, now=clock()) if resume else RunState(plan.fingerprint, plan.manifest)
        return cls(state, store)

    @property
    def translations(self) -> Mapping[str, str]:
        return MappingProxyType(self._state.translations)

    @property
    def failed_batches(self):
        return self._state.failed_batches

    def fail_batch(self, failure):
        ids = set(failure.segment_ids)
        previous = tuple(replace(f, segment_ids=tuple(i for i in f.segment_ids if i not in ids))
                         for f in self._state.failed_batches if set(f.segment_ids) - ids)
        self._commit(replace(self._state, failed_batches=previous + (failure,)))

    def save(self):
        self._store.save(encode_state(self._state))

    def _commit(self, state):
        self._store.save(encode_state(state))
        self._state = state

    def recent(self, now):
        recent = tuple(r for r in self._state.recent_requests if r.time > now - 60)
        self._state = replace(self._state, recent_requests=recent)
        return recent

    def reserve(self, now, tokens):
        self._commit(replace(self._state, calls_total=self._state.calls_total + 1,
                             recent_requests=self._state.recent_requests + (RequestRecord(now, tokens),)))

    def accept(self, translations, model, catalog=()):
        if not valid_model_id(model) or not valid_catalog(catalog):
            raise TranslationError("Адаптер вернул некорректные сведения о модели/каталоге.")
        failures = tuple(replace(f, segment_ids=tuple(i for i in f.segment_ids if i not in translations))
                         for f in self._state.failed_batches if set(f.segment_ids) - set(translations))
        self._commit(replace(self._state, failed_batches=failures,
                             translations={**self._state.translations, **translations},
                             segment_models={**self._state.segment_models, **{i: model for i in translations}},
                             catalog=tuple(catalog) if catalog else self._state.catalog))
