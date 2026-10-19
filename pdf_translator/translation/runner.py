"""Resumable translation orchestration; persistence and request execution are ports."""

from pathlib import Path
from contextlib import contextmanager

from ..telemetry import emit, span
from .execution import ExecutionPolicy, RequestController
from .journal import RunJournal
from .models import Progress, TranslationError, TranslationOptions, TranslationResult
from .ports import TranslationProvider
from .planning import plan_translation
from .serialization import digest
from .storage import check_output_paths, JsonCheckpointStore, write_outputs
from .workflow import run_translation


def translate_file(input_path: str | Path, *, output_dir: str | Path, options: TranslationOptions,
                   provider: TranslationProvider,
                   allow_network: bool = False, glossary=None, limit_units: int | None = None,
                   resume: bool = False, overwrite: bool = False,
                   progress: Progress | None = None) -> TranslationResult:
    """Translate with explicit opt-in. Caller owns provider resources."""
    with span("translation.run") as outcome:
        result = _translate_file(input_path, output_dir=output_dir, options=options, provider=provider,
                                 allow_network=allow_network, glossary=glossary, limit_units=limit_units,
                                 resume=resume, overwrite=overwrite, progress=progress)
        if result.status != "complete":
            outcome["status"] = result.status
        return result


def _translate_file(input_path: str | Path, *, output_dir: str | Path, options: TranslationOptions,
                   provider: TranslationProvider,
                   allow_network: bool = False, glossary=None, limit_units: int | None = None,
                   resume: bool = False, overwrite: bool = False,
                   progress: Progress | None = None) -> TranslationResult:
    with open_run(input_path, output_dir=output_dir, options=options, provider=provider,
                  allow_network=allow_network, glossary=glossary, limit_units=limit_units,
                  resume=resume, overwrite=overwrite) as context:
        plan, journal, check_source, export, paths = context
        progress = progress or (lambda _: None)
        controller = RequestController(ExecutionPolicy.from_options(options), journal, progress)
        result = run_translation(plan, options, provider, journal, controller,
                                 resume=resume, check_source=check_source, export=export, progress=progress)
        return file_result(result, plan, controller, paths)


def file_result(result, plan, controller, paths):
    checkpoint, markdown_path, text_path = paths
    return TranslationResult(result.status, result.completed, len(plan.segments), controller.calls,
                             result.warnings, str(checkpoint.resolve()),
                             str(markdown_path.resolve()) if result.status == "complete" else None,
                             str(text_path.resolve()) if result.status == "complete" else None, result.pause_reason, result.failed_batches)


@contextmanager
def open_run(input_path, *, output_dir, options, provider, allow_network,
             glossary, limit_units, resume, overwrite):
    """Shared preflight and file lifecycle for sync and async execution.

    No await inside journal mutation; callers serialize it on their event loop.
    Provider resource lifetime belongs to the composition root, never this core.
    """
    if not allow_network:
        raise TranslationError("Перевод требует явного allow_network=True. Для локального плана используйте plan_translation().")
    plan = plan_translation(input_path, options, provider=provider.identity,
                            glossary=glossary, limit_units=limit_units)
    emit("translation.options", model=options.model, timeout_seconds=options.timeout_seconds, resume=resume, overwrite=overwrite,
         provider=provider.identity.name if provider.identity.name in ("gemini", "gemini-auto") else "external")
    source = Path(input_path)
    folder = Path(output_dir)
    stem = source.stem + "." + options.target_language.lower()
    markdown_path, text_path = folder / (stem + ".md"), folder / (stem + ".txt")
    checkpoint = folder / (stem + ".translation.json")
    check_output_paths([markdown_path, text_path], source, overwrite=overwrite)
    if checkpoint.is_symlink():
        raise TranslationError("Журнал перевода не может быть символической ссылкой.")
    if checkpoint.exists() and not resume:
        raise TranslationError("Журнал уже существует. Для продолжения укажите --resume; для нового перевода выберите другую папку.")
    if resume and not checkpoint.is_file():
        raise TranslationError("Журнал для --resume не найден.")

    def check_source():
        if digest(source.read_bytes()) != plan.manifest["source_sha256"]:
            raise TranslationError("Исходник изменился во время перевода. Экспорт остановлен; журнал сохраняет прежний снимок.")

    store = JsonCheckpointStore(checkpoint)
    with store.lock():
        # Recheck after locking; a competing run could finish before lock acquisition.
        if not resume and checkpoint.exists():
            raise TranslationError("Журнал появился в другом запуске; используйте --resume.")
        journal = RunJournal.open(plan, store, resume=resume)
        cached = journal.translations
        if resume:
            emit("translation.resumed", completed=len(cached), total=len(plan.segments))
        def export(markdown, text):
            check_output_paths([markdown_path, text_path], source, overwrite=overwrite)
            write_outputs({markdown_path: markdown, text_path: text}, overwrite=overwrite)

        yield plan, journal, check_source, export, (checkpoint, markdown_path, text_path)
