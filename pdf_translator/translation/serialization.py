"""Strict JSON and deterministic hashing, independent of application workflows."""

import hashlib
import json


class DuplicateJsonKeyError(ValueError):
    """No offending key in the exception: it may contain source text or secrets."""


def strict_json(raw: str) -> object:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateJsonKeyError("Duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError("Non-finite JSON number")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)


def digest(value: bytes | str | dict | list) -> str:
    if not isinstance(value, (str, bytes)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()
