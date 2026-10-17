"""Public, UI-independent conversion API."""

from .converter import convert_pdf
from .models import Block, ConversionError, ConversionResult, ConversionWarning, SourceRegion

__all__ = ["convert_pdf", "Block", "ConversionError", "ConversionResult", "ConversionWarning", "SourceRegion"]
