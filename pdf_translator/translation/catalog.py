"""Deterministic capability filter/ranking, not a hardcoded list of model IDs."""

import re
from dataclasses import dataclass

POLICY_VERSION = "generation-class-stability-v1"
_TEXT_MODEL = re.compile(r"gemini-(\d+(?:\.\d+){0,2})-(pro|flash-lite|flash)(?:-(preview|exp)(?:-(\d{2})-(\d{2}))?|-(\d{3}))?$")


@dataclass(frozen=True)
class ModelCandidate:
    name: str
    input_tokens: int
    output_tokens: int


def rank_models(models) -> tuple[ModelCandidate, ...]:
    """Newest generation first, then Pro > Flash > Flash Lite, stable > preview.

    Unknown classes are deliberately excluded rather than guessed. Method support
    alone is insufficient: TTS/image models can also expose generateContent.
    """
    ranked = {}
    for model in models:
        name = getattr(model, "name", "") or ""
        if name.startswith("models/"):
            name = name[7:]
        match = _TEXT_MODEL.fullmatch(name)
        if not match or "generateContent" not in (getattr(model, "supported_actions", None) or ()):
            continue
        limits = (getattr(model, "input_token_limit", None), getattr(model, "output_token_limit", None))
        if not all(type(value) is int and value > 0 for value in limits):
            continue
        generation, family, preview, month, day, revision = match.groups()
        version = tuple(map(int, generation.split(".")))
        version += (0,) * (3 - len(version))
        order = (*version, {"pro": 3, "flash": 2, "flash-lite": 1}[family],
                 int(preview is None), int(revision or 0), int(month or 0), int(day or 0), name)
        ranked[name] = (order, ModelCandidate(name, *limits))
    return tuple(item[1] for item in sorted(ranked.values(), key=lambda item: item[0], reverse=True))
