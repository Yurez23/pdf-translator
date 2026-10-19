"""Separate translation command: offline planning unless --send is explicit."""

import argparse
import asyncio
import sys
from pathlib import Path

from ..logging_setup import add_logging_arguments, run_cli
from .models import MAX_CHUNK_CHARS, TranslationError, TranslationOptions
from .planning import plan_translation
from .serialization import strict_json
from .async_runner import translate_file_async
from .gemini import GeminiProvider, validate_model
from .routing import GeminiAutoProvider
from .availability import default_database_path


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Перевод MD/TXT через Gemini. Без --send: только локальный план, без ключа и сети.")
    parser.add_argument("input", type=Path, help="исходный .md или .txt в UTF-8")
    parser.add_argument("--model", default="auto", help="auto (по умолчанию): каталог и fallback; явный ID отключает fallback")
    parser.add_argument("--availability-db", type=Path, default=default_database_path(), help="SQLite-кэш в корне проекта")
    parser.add_argument("--project-id", help="необязательный ID проекта для общего кэша разных API-ключей")
    parser.add_argument("--reset-availability", action="store_true", help="сбросить ограничения этого проекта/ключа перед --send")
    parser.add_argument("--ignore-availability-cache", action="store_true", help="игнорировать сохранённые ограничения, но записывать новые")
    parser.add_argument("--send", action="store_true", help="разрешить отправку текста и глоссария Google; проект должен быть Free Tier")
    parser.add_argument("--output-dir", type=Path, default=Path("output/translations"))
    parser.add_argument("--source-language", default="EN")
    parser.add_argument("--target-language", default="RU")
    parser.add_argument("--glossary", type=Path, help="JSON-объект пар исходный термин: перевод")
    parser.add_argument("--limit-units", type=int, help="начальная проба: не менее N текстовых элементов, без разрезания контейнеров")
    parser.add_argument("--max-chunk-chars", type=int, default=MAX_CHUNK_CHARS,
                        help="жёсткий предел текста пакета, включая пробелы: 256–16000, по умолчанию 16000")
    parser.add_argument("--max-input-tokens", type=int, default=12000)
    parser.add_argument("--max-output-tokens", type=int, default=32768)
    parser.add_argument("--context-chars", type=int, default=1000)
    parser.add_argument("--requests-per-minute", type=int, default=5)
    parser.add_argument("--tokens-per-minute", type=int, default=50000)
    parser.add_argument("--max-requests", type=int, default=10, help="предел ВСЕХ API-вызовов за запуск, включая подсчёт и повторы")
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-response-retries", type=int, default=1, help="0–5 повторов целого пакета при неверном JSON/ID/маркерах на той же модели; по умолчанию 1")
    parser.add_argument("--gemini-response-format", choices=("strict", "legacy"), default="strict",
                        help="strict: обязательные ID в схеме; legacy: прежний формат для явного продолжения старого журнала")
    parser.add_argument("--max-concurrency", type=int, default=5, help="одновременно обрабатывать 1–5 независимых пакетов, по умолчанию 5")
    parser.add_argument("--timeout-seconds", type=float, default=300,
                        help="тайм-аут одной API-попытки: 1–300 секунд, по умолчанию 300")
    parser.add_argument("--resume", action="store_true", help="продолжить совместимый журнал без повторного перевода сохранённых частей")
    parser.add_argument("--overwrite", action="store_true", help="разрешить замену только итоговых файлов; не сбрасывает журнал")
    add_logging_arguments(parser)
    args = parser.parse_args(argv)
    return run_cli(args, lambda: _execute(args), command="translation", enabled=args.send)


def _execute(args) -> int:
    try:
        options = TranslationOptions(**{name: getattr(args, name) for name in TranslationOptions.__dataclass_fields__})
        glossary = None
        if args.glossary:
            try:
                glossary = strict_json(args.glossary.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, ValueError, RecursionError):
                raise TranslationError("Не удалось прочитать UTF-8 JSON-глоссарий.") from None
        # Composition root: only this layer selects the concrete translator.
        # Construction/identity are local; prepare() runs only for uncached work.
        progress = lambda value: print(value, file=sys.stderr, flush=True)
        if options.model == "auto":
            provider = GeminiAutoProvider(database_path=args.availability_db, project_id=args.project_id,
                                         reset_cache=args.reset_availability, ignore_cache=args.ignore_availability_cache,
                                         progress=progress, response_contract=args.gemini_response_format)
        else:
            validate_model(options.model)
            if args.reset_availability or args.ignore_availability_cache or args.project_id:
                raise TranslationError("Параметры кэша доступны только с --model auto.")
            provider = GeminiProvider(response_contract=args.gemini_response_format)
        plan = plan_translation(args.input, options, provider=provider.identity,
                                glossary=glossary, limit_units=args.limit_units)
        print(f"План: {len(plan.document.units)} элементов, {len(plan.segments)} сегментов, {len(plan.batches)} пакетов.")
        print(f"Модель: {options.model}. Языки: {options.source_language} → {options.target_language}.")
        print(f"Асинхронный режим: до {options.max_concurrency} пакетов одновременно; ограничения запросов общие.")
        if options.model == "auto":
            print("Автовыбор: каталог запрашивается только с --send; SQLite: " + str(args.availability_db))
        print(f"Снимок: {plan.fingerprint}")
        if not args.send:
            if args.log_dir is None or args.no_log:
                print("Локальный план: запросов нет, ключ не нужен, файлы не записаны. Для перевода явно добавьте --send.")
            else:
                print("Локальный план: запросов нет, ключ не нужен. Файлы перевода не записаны; создан только технический лог. Для перевода явно добавьте --send.")
            for warning in plan.document.warnings:
                print("Предупреждение: " + str(warning), file=sys.stderr)
            return 0
        prefix = args.input.stem + "." + options.target_language.lower()
        protected = [args.input, *(args.output_dir / (prefix + suffix) for suffix in (".md", ".txt", ".translation.json", ".translation.json.lock"))]
        if args.glossary:
            protected.append(args.glossary)
        if options.model == "auto" and any(args.availability_db.resolve() == path.resolve() for path in protected):
            raise TranslationError("Путь SQLite-кэша совпадает с исходником, глоссарием или результатом.")
        if args.glossary:
            prefix = args.input.stem + "." + options.target_language.lower()
            candidates = [args.output_dir / (prefix + suffix) for suffix in (".md", ".txt", ".translation.json", ".translation.json.lock")]
            if args.glossary.resolve() in [item.resolve() for item in candidates]:
                raise TranslationError("Путь результата или журнала совпадает с глоссарием.")
        print("Отправка Google разрешена. Нулевая стоимость зависит от Free Tier вашего проекта; программа не проверяет биллинг.", file=sys.stderr)
        async def send():
            try:
                return await translate_file_async(args.input, output_dir=args.output_dir, options=options, provider=provider,
                                    allow_network=True, glossary=glossary, limit_units=args.limit_units,
                                    resume=args.resume, overwrite=args.overwrite,
                                    progress=progress)
            finally:
                await provider.aclose()
        result = asyncio.run(send())
        for warning in result.warnings:
            print("Предупреждение: " + str(warning), file=sys.stderr)
        for failure in result.failed_batches:
            print(f"Проблемный пакет {failure.batch_id}: {len(failure.segment_ids)} сегментов, {failure.code}, "
                  f"{failure.attempts} попыток, модель {failure.model}.", file=sys.stderr)
        if result.status == "incomplete":
            print(f"Перевод неполон: {result.completed_segments}/{result.total_segments} сегментов. Журнал: {result.checkpoint}")
            print("Готовые MD/TXT не созданы. Повторите совместимые параметры с --send --resume для незавершённых пакетов.")
            return 4
        if result.status == "paused":
            print(f"Остановлено: {result.pause_reason}. Прогресс {result.completed_segments}/{result.total_segments}. Журнал: {result.checkpoint}")
            print("Продолжение: повторите те же параметры с --send --resume. Итоговые файлы пока не созданы.")
            return 3
        print(f"Сохранено: {result.markdown_path}\nСохранено: {result.text_path}\nЖурнал: {result.checkpoint}")
        return 0
    except KeyboardInterrupt:
        print("Перевод отменён. Сохранённые пакеты остаются в журнале; продолжение через --resume.", file=sys.stderr)
        return 130
    except (TranslationError, OSError) as exc:
        # Only our sanitized errors and local filesystem errors reach this boundary.
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
