"""Shared staging/publication mechanism; callers own path and overwrite policies."""

import os
import tempfile
from pathlib import Path

from .diagnostics import DiagnosticError


def write_utf8_outputs(outputs, *, overwrite, prefix, create_parents=False, before_publish=None):
    """Stage all files before publishing; rollback new files on ordinary failure.

    Overwrite is atomic per file, NOT transactional across the entire set.
    Policy callbacks run immediately before publication, including after staging.
    """
    staged, created = [], []
    try:
        for target, content in outputs.items():
            if create_parents:
                target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=target.parent)
            stage = Path(name)
            staged.append((stage, target))
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
        for stage, target in staged:
            if before_publish is not None:
                before_publish(target)
            if overwrite:
                os.replace(stage, target)
            else:
                with target.open("x", encoding="utf-8", newline="\n") as stream:
                    created.append(target)
                    stream.write(stage.read_text(encoding="utf-8"))
    except (OSError, DiagnosticError):
        if not overwrite:
            for target in created:
                target.unlink(missing_ok=True)
        raise
    finally:
        for stage, _ in staged:
            stage.unlink(missing_ok=True)
