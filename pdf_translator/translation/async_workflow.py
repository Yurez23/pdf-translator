"""Bounded async batch scheduling; journal mutations stay on one event loop."""

import asyncio

from ..telemetry import emit, span
from .chunking import split_batch
from .models import BatchResponseFailed, TranslationError, TranslationPaused
from .response_recovery import translate_checked_async
from .planning import make_request
from .ports import BatchTooLarge
from .workflow import accept_response, finish_translation, record_failed_batch


async def run_translation_async(plan, options, provider, journal, controller, *,
                                resume, check_source, export, progress):
    cached = journal.translations
    pending = asyncio.Queue()
    for i, batch in enumerate(plan.batches):
        batch = tuple(s for s in batch if s.id not in cached)
        if batch:
            pending.put_nowait((f"b{i + 1:06d}", batch))
    errors = []
    stopping = False
    if not resume:
        journal.save()
    if not pending.empty():
        with span("provider.prepare"):
            factory = provider.prepare_async(options)
            if not callable(getattr(factory, "new_session", None)):
                raise TranslationError("prepare_async(options) должен вернуть фабрику асинхронных сессий.")

        async def worker():
            nonlocal stopping
            session = None
            while True:
                batch_id, batch = await pending.get()
                try:
                    if stopping:
                        continue
                    if session is None:
                        session = factory.new_session()
                    check_source()
                    with span("translation.batch", batch_id=batch_id, segments=len(batch)) as outcome:
                        try:
                            response = await translate_checked_async(session, make_request(plan, batch, options), executor=controller, options=options)
                        except BatchTooLarge:
                            left, right = split_batch(plan.document, batch)
                            pending.put_nowait((batch_id + ".1", left))
                            pending.put_nowait((batch_id + ".2", right))
                            emit("translation.batch_split", reason="input_limit", segments=len(batch))
                            outcome["status"] = "split"
                            continue
                        except BatchResponseFailed as error:
                            check_source()
                            record_failed_batch(batch_id, batch, error, journal, progress)
                            outcome["status"] = "incomplete"
                            continue
                        check_source()
                        accept_response(response, batch, options, journal, len(plan.segments), progress)
                except Exception as error:
                    errors.append(error)
                    stopping = True
                    controller.stop()
                finally:
                    pending.task_done()

        tasks = [asyncio.create_task(worker()) for _ in range(options.max_concurrency)]
        joined = asyncio.create_task(pending.join())
        try:
            # On errors drain calls already sent and save their valid responses.
            # User/task cancellation instead cancels all workers immediately.
            done, _ = await asyncio.wait((joined, *tasks), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                # A worker normally waits for more work forever. Unexpected
                # cancellation/termination must not strand queue.join().
                task.result()
        finally:
            joined.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(joined, *tasks, return_exceptions=True)
    fatal = next((error for error in errors if not isinstance(error, TranslationPaused)), None)
    if fatal is not None:
        raise fatal
    status = "paused" if errors else "complete"
    pause_reason = str(errors[0]) if errors else None
    return finish_translation(plan, journal, controller, status, pause_reason, check_source, export)
