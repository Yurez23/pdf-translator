"""Async public API; same plan/checkpoint/export contracts as the sync runner."""

from pathlib import Path

from ..telemetry import emit, span
from .async_execution import AsyncRequestController
from .async_workflow import run_translation_async
from .execution import ExecutionPolicy
from .models import Progress, TranslationOptions, TranslationResult
from .ports import AsyncTranslationProvider
from .runner import open_run, file_result


async def translate_file_async(input_path: str | Path, *, output_dir: str | Path,
                               options: TranslationOptions, provider: AsyncTranslationProvider,
                               allow_network: bool = False, glossary=None, limit_units: int | None = None,
                               resume: bool = False, overwrite: bool = False,
                               progress: Progress | None = None) -> TranslationResult:
    """Opt-in concurrent translation. Caller must await provider cleanup.

    Local filesystem persistence is short synchronous work serialized on this
    loop. External requests and quota/backoff/probe waits are cooperative.
    """
    with span("translation.run") as outcome:
        emit("translation.concurrency", max_concurrency=options.max_concurrency)
        with open_run(input_path, output_dir=output_dir, options=options, provider=provider,
                      allow_network=allow_network, glossary=glossary, limit_units=limit_units,
                      resume=resume, overwrite=overwrite) as context:
            plan, journal, check_source, export, paths = context
            progress = progress or (lambda _: None)
            controller = AsyncRequestController(ExecutionPolicy.from_options(options), journal, progress)
            result = await run_translation_async(plan, options, provider, journal, controller,
                                                 resume=resume, check_source=check_source, export=export, progress=progress)
            if result.status != "complete":
                outcome["status"] = result.status
            return file_result(result, plan, controller, paths)
