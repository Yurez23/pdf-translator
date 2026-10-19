"""Contracts for the opt-in translation stage; no SDK imports or network calls."""

import math
import re
from dataclasses import dataclass, field
from typing import Callable, Literal

MAX_CHUNK_CHARS = 16000


class TranslationError(ValueError):
    """An actionable translation/input/checkpoint failure, safe to show in a CLI."""


class TranslationPaused(TranslationError):
    """A resumable stop, not a failed or completed translation."""


class RequestLimitReached(TranslationPaused):
    """The explicit per-run request budget was reached; progress is resumable."""


class ProviderError(TranslationError):
    """Sanitized provider failure; never contains a key or submitted content."""

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0,
                 status_code: int = 0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.status_code = status_code


class QuotaError(ProviderError):
    """Sanitized quota facts; unknown scope must not be treated as a daily limit."""

    def __init__(self, *, kind="unknown", scope="unknown", model="", retry_after=0, retryable=False):
        super().__init__("Квота Gemini исчерпана или запросы идут слишком часто. Прогресс сохранён.",
                         retryable=retryable, retry_after=retry_after, status_code=429)
        self.kind, self.scope, self.model = kind, scope, model


@dataclass(frozen=True)
class TranslationOptions:
    model: str
    source_language: str = "EN"
    target_language: str = "RU"
    max_chunk_chars: int = MAX_CHUNK_CHARS
    max_input_tokens: int = 12000
    max_output_tokens: int = 16384
    context_chars: int = 1000
    requests_per_minute: int = 5
    tokens_per_minute: int = 30000
    max_requests: int = 10
    max_retries: int = 2
    timeout_seconds: float = 300.0

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
    warnings: tuple[str, ...]


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
    status: Literal["complete", "paused"]
    completed_segments: int
    total_segments: int
    requests_made: int
    warnings: tuple[str, ...]
    checkpoint: str
    markdown_path: str | None = None
    text_path: str | None = None
    pause_reason: str | None = None


@dataclass(frozen=True)
class ModelLimits:
    input_tokens: int
    output_tokens: int


type Progress = Callable[[str], None]
