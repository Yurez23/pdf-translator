"""Contracts for the opt-in translation stage; no SDK imports or network calls."""

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Literal

from ..diagnostics import DiagnosticError

MAX_CHUNK_CHARS = 16000


class TranslationError(DiagnosticError):
    """An actionable translation/input/checkpoint failure, safe to show in a CLI."""


class TranslationPaused(TranslationError):
    """A resumable stop, not a failed or completed translation."""

    diagnostic_code = "paused"


RESPONSE_ISSUES = {
    "invalid_json": "Ответ не является корректным JSON.",
    "response_structure": "Неверная структура ответа модели.",
    "duplicate_id": "В ответе повторены ключи или ID.",
    "id_mismatch": "Набор ID ответа не совпадает с запросом.",
    "invalid_text": "Пустой или некорректный перевод сегмента.",
    "protected_markers": "Нарушены защищённые маркеры.",
    "response_length": "Подозрительная длина перевода сегмента.",
}


class ResponseValidationError(TranslationError):
    """Recoverable model output, never a configuration/storage/provider failure."""

    diagnostic_code = "response_validation"

    def __init__(self, code, *, expected_count=None, received_count=None, missing_ids=(), unexpected_count=0, segment_id=None):
        if code not in RESPONSE_ISSUES:
            raise ValueError("Unknown response validation category")
        self.code = code
        self.expected_count, self.received_count = expected_count, received_count
        self.missing_ids, self.unexpected_count = tuple(missing_ids), unexpected_count
        self.segment_id = segment_id
        detail = ""
        if expected_count is not None and received_count is not None:
            detail = f" Ожидалось {expected_count}, получено {received_count}; отсутствует {len(self.missing_ids)}, посторонних {unexpected_count}."
        super().__init__(RESPONSE_ISSUES[code] + detail + " Пакет не сохранён.")


class BatchResponseFailed(TranslationError):
    """Response recovery exhausted: isolate this batch, do not retry it again outside."""

    diagnostic_code = "response_exhausted"

    def __init__(self, error: ResponseValidationError, attempts: int, model: str):
        self.error, self.attempts, self.model = error, attempts, model
        super().__init__(f"Некорректный ответ после {attempts} попыток. {error}")


@dataclass(frozen=True)
class BatchFailure:
    batch_id: str
    segment_ids: tuple[str, ...]
    code: str
    attempts: int
    model: str


class RequestLimitReached(TranslationPaused):
    """The explicit per-run request budget was reached; progress is resumable."""

    diagnostic_code = "request_limit"


class ProviderError(TranslationError):
    """Sanitized provider failure; never contains a key or submitted content."""

    diagnostic_code = "provider"

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0,
                 status_code: int = 0, error_code: str = "provider"):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.status_code = status_code
        self.error_code = error_code


class QuotaError(ProviderError):
    """Sanitized quota facts; unknown scope must not be treated as a daily limit."""

    diagnostic_code = "quota"

    def __init__(self, *, kind="unknown", scope="unknown", model="", retry_after=0, retryable=False,
                 message="Квота провайдера исчерпана или запросы идут слишком часто. Прогресс сохранён."):
        super().__init__(message,
                         retryable=retryable, retry_after=retry_after, status_code=429)
        self.kind, self.scope, self.model = kind, scope, model


@dataclass(frozen=True)
class TranslationWarning:
    """Stable machine-readable category, separate from display language."""

    code: str
    message: str
    segment_id: str | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class TranslationOptions:
    model: str
    source_language: str = "EN"
    target_language: str = "RU"
    max_chunk_chars: int = MAX_CHUNK_CHARS
    max_input_tokens: int = 12000
    max_output_tokens: int = 32768
    context_chars: int = 1000
    requests_per_minute: int = 5
    tokens_per_minute: int = 50000
    max_requests: int = 10
    max_retries: int = 2
    timeout_seconds: float = 300.0
    max_concurrency: int = 5
    max_response_retries: int = 1

    def __post_init__(self):
        if (not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 200
                or self.model != self.model.strip() or any(c.isspace() or not c.isprintable() for c in self.model)):
            raise TranslationError("Укажите непустой ID модели/движка длиной не более 200 символов.")
        for value in (self.source_language, self.target_language):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?", value):
                raise TranslationError("Язык задаётся кодом, например EN или RU.")
        if self.source_language.casefold() == self.target_language.casefold():
            raise TranslationError("Исходный и целевой языки должны отличаться.")
        bounds = {
            "max_chunk_chars": (256, MAX_CHUNK_CHARS), "max_input_tokens": (512, 1000000),
            "max_output_tokens": (256, 65536), "context_chars": (0, 10000),
            "requests_per_minute": (1, 1000), "tokens_per_minute": (512, 10000000),
            "max_requests": (1, 1000000), "max_retries": (0, 5),
            "max_concurrency": (1, 5),
            "max_response_retries": (0, 5),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise TranslationError(f"{name} должен быть целым числом от {low} до {high}.")
        if (isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (float, int))
                or not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 300):
            raise TranslationError("timeout_seconds должен быть числом от 1 до 300.")


@dataclass(frozen=True)
class ProtectedToken:
    marker: str
    markdown: str
    text: str


@dataclass(frozen=True)
class TranslationUnit:
    id: str
    text: str
    kind: str
    protected: tuple[ProtectedToken, ...] = ()
    context: str = ""


@dataclass
class DocumentNode:
    kind: str
    unit_id: str = ""
    children: list["DocumentNode"] = field(default_factory=list)
    level: int = 0
    text: str = ""
    marker: str = "-"
    start: int = 1
    rows: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class TranslationDocument:
    nodes: tuple[DocumentNode, ...]
    units: tuple[TranslationUnit, ...]
    warnings: tuple[TranslationWarning, ...]


@dataclass(frozen=True)
class TranslationSegment:
    id: str
    unit_id: str
    text: str
    separator: str = ""


@dataclass(frozen=True)
class TranslationPlan:
    document: TranslationDocument
    segments: tuple[TranslationSegment, ...]
    batches: tuple[tuple[TranslationSegment, ...], ...]
    manifest: dict
    fingerprint: str
    glossary: dict[str, str]


@dataclass(frozen=True)
class TranslationResult:
    status: Literal["complete", "paused", "incomplete"]
    completed_segments: int
    total_segments: int
    requests_made: int
    warnings: tuple[TranslationWarning, ...]
    checkpoint: str
    markdown_path: str | None = None
    text_path: str | None = None
    pause_reason: str | None = None
    failed_batches: tuple[BatchFailure, ...] = ()


@dataclass(frozen=True)
class ModelLimits:
    input_tokens: int
    output_tokens: int


type Progress = Callable[[str], None]
