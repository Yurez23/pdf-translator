"""Exclusive checkpoint locks and recoverable local output writes."""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..telemetry import emit, instrument
from ..file_outputs import write_utf8_outputs
from .models import TranslationError
from .serialization import strict_json


class JsonCheckpointStore:
    """Filesystem adapter for the checkpoint port; no run policy or model selection."""

    def __init__(self, path: Path):
        self.path = path

    def lock(self):
        return checkpoint_lock(self.path)

    def load(self):
        return load_checkpoint(self.path)

    def save(self, value):
        save_checkpoint(self.path, value)


@contextmanager
def checkpoint_lock(checkpoint: Path):
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    lock = checkpoint.with_suffix(checkpoint.suffix + ".lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        emit("storage.lock_acquired")
    except FileExistsError:
        raise TranslationError("Журнал перевода заблокирован другим запуском. Если процесс завершился аварийно, проверьте и удалите его .lock вручную.") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)
        emit("storage.lock_released")


@instrument("storage.checkpoint_write")
def save_checkpoint(path: Path, state: dict) -> None:
    stage = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".translation-", suffix=".tmp", dir=path.parent)
        stage = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


@instrument("storage.checkpoint_read")
def load_checkpoint(path: Path) -> dict:
    try:
        if path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
            raise TranslationError("Небезопасный или слишком большой журнал перевода.")
        value = strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, TranslationError):
            raise
        raise TranslationError("Не удалось прочитать журнал перевода; он повреждён или недоступен.") from None
    if not isinstance(value, dict):
        raise TranslationError("Некорректный журнал перевода.")
    return value


def check_output_paths(paths: list[Path], input_path: Path, *, overwrite: bool) -> None:
    source = input_path.resolve()
    for target in paths:
        if target.resolve() == source:
            raise TranslationError("Путь результата совпадает с исходным файлом.")
        if target.exists() and not target.is_file():
            raise TranslationError(f"Путь результата занят не обычным файлом: {target}.")
        if target.is_symlink() or target.exists() and not overwrite:
            raise TranslationError(f"Результат уже существует или является ссылкой: {target}. Выберите другую папку или --overwrite.")


@instrument("storage.export")
def write_outputs(outputs: dict[Path, str], *, overwrite: bool) -> None:
    def refuse_link(target):
        if target.is_symlink():
            raise TranslationError("Отказ от записи перевода через символическую ссылку.")
    write_utf8_outputs(outputs, overwrite=overwrite, prefix=".translation-", before_publish=refuse_link)
