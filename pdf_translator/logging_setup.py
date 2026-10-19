"""CLI logging setup; file lifecycle belongs to the JSONL sink adapter."""

import sys
from pathlib import Path

from .jsonl import JsonlSink
from .telemetry import log_run


def default_log_directory():
    checkout = Path(__file__).resolve().parents[1]
    return (checkout if (checkout / "pyproject.toml").is_file() else Path.cwd()) / "logs"


def add_logging_arguments(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--log-dir", type=Path, help="папка технических JSONL-логов (по умолчанию logs в проекте)")
    group.add_argument("--no-log", action="store_true", help="не создавать технический лог")


def run_cli(args, operation, *, command, enabled=True):
    # Ordinary translation dry-run remains strictly read-only. Explicit --log-dir
    # authorizes a log even for a plan; --help/argument parsing never creates files.
    if args.no_log or not (enabled or args.log_dir is not None):
        return operation()
    try:
        sink = JsonlSink(args.log_dir if args.log_dir is not None else default_log_directory())
    except (OSError, ValueError):
        print("Ошибка: не удалось открыть технический лог. Проверьте --log-dir или явно используйте --no-log.", file=sys.stderr)
        return 1
    print("Технический лог: " + str(sink.path), file=sys.stderr)
    try:
        with log_run(sink, command=command) as outcome:
            result = operation()
            outcome.update(exit_code=result, status={0: "success", 3: "paused", 4: "incomplete", 130: "cancelled"}.get(result, "error"))
            return result
    finally:
        try:
            sink.close()
        except OSError:
            print("Предупреждение [logging_unavailable]: не удалось закрыть технический лог.", file=sys.stderr)
