"""Domain-facing translation ports; no SDK, prompt, JSON or token-count API."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .models import TranslationError, TranslationOptions


@dataclass(frozen=True)
class ProviderIdentity:
    """Public cache namespace and revision of translation-affecting behavior.

    Never put credentials here. Change revision when adapter behavior changes
    (for example its prompt, schema, generation settings or API version).
    Model and language settings are fingerprinted separately by the core.
    """

    name: str
    revision: str

    def __post_init__(self):
        for value in (self.name, self.revision):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
                raise TranslationError("Идентификатор и ревизия адаптера должны быть публичными короткими ID.")


@dataclass(frozen=True)
class TranslationItem:
    id: str
    text: str
    kind: str


@dataclass(frozen=True)
class TranslationRequest:
    items: tuple[TranslationItem, ...]
    source_language: str
    target_language: str
    glossary: Mapping[str, str]
    context: str


@dataclass(frozen=True)
class TranslationResponse:
    """Optional attribution for a batch; plain mappings remain supported."""

    translations: Mapping[str, str]
    model: str
    catalog: tuple[str, ...] = ()


class BatchTooLarge(TranslationError):
    """The adapter cannot accept this batch; the core may split it and retry."""


class RequestExecutor(Protocol):
    """Route EVERY external attempt through the shared budget/retry controller.

    Tokens are optional for non-token providers. An operation must perform only
    one external attempt and raise sanitized ProviderError for retryable failures.
    No adapter-internal retries, unbudgeted requests or logging of source/keys.
    """

    def call[T](self, operation: Callable[[], T], *, tokens: int = 0, label: str) -> T: ...


class TranslationProvider(Protocol):
    @property
    def identity(self) -> ProviderIdentity: ...

    def prepare(self, options: TranslationOptions) -> None:
        """Check local settings/dependencies/credentials; MUST NOT use the network."""
        ...

    def translate(self, request: TranslationRequest, *, executor: RequestExecutor) -> Mapping[str, str] | TranslationResponse:
        """Return exactly one translated string per item ID, preserving markers.

        All external calls go through executor. Raise BatchTooLarge before
        translation if a batch exceeds provider limits. Resource lifecycle belongs
        to the caller/composition root, never to the conversion/translation core.
        """
        ...
