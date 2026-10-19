"""Opt-in translation ports and core, independent of providers/PDF extraction."""

from .models import TranslationError, TranslationOptions, TranslationPlan, TranslationResult
from .planning import plan_translation
from .runner import translate_file
from .ports import (BatchTooLarge, ProviderIdentity, RequestExecutor, TranslationItem,
                    TranslationProvider, TranslationRequest, TranslationResponse)

__all__ = ["plan_translation", "translate_file", "TranslationOptions", "TranslationPlan", "TranslationResult", "TranslationError",
           "ProviderIdentity", "TranslationProvider", "TranslationRequest", "TranslationItem", "RequestExecutor", "BatchTooLarge",
           "TranslationResponse"]
