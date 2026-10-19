"""Opt-in translation ports and core, independent of providers/PDF extraction."""

from .models import TranslationError, TranslationOptions, TranslationPlan, TranslationResult, TranslationWarning
from .models import BatchFailure, ResponseValidationError
from .planning import plan_translation
from .runner import translate_file
from .async_runner import translate_file_async
from .ports import (BatchTooLarge, ProviderIdentity, RequestExecutor, TranslationItem,
                    TranslationProvider, TranslationRequest, TranslationResponse, TranslationSession)
from .ports import AsyncRequestExecutor, AsyncTranslationProvider, AsyncTranslationSession, AsyncSessionFactory

__all__ = ["plan_translation", "translate_file", "TranslationOptions", "TranslationPlan", "TranslationResult", "TranslationError",
           "ProviderIdentity", "TranslationProvider", "TranslationRequest", "TranslationItem", "RequestExecutor", "BatchTooLarge",
           "TranslationResponse", "TranslationSession", "TranslationWarning", "translate_file_async",
           "AsyncRequestExecutor", "AsyncTranslationProvider", "AsyncTranslationSession", "AsyncSessionFactory",
           "BatchFailure", "ResponseValidationError"]
