"""Small local CLI; conversion itself never writes files."""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .converter import convert_pdf
from .models import ConversionError, HyphenationIssue


def _read_choice() -> str:
    """One key in a terminal, one answer per line when stdin is redirected."""
    if not sys.stdin.isatty():
        value = sys.stdin.readline()
        if not value:
            raise EOFError
        return value.strip().casefold()
    if os.name == "nt":
        import msvcrt
        value = msvcrt.getwch()
        if value in ("\x00", "\xe0"):
            msvcrt.getwch()
            return ""
    else:
        import termios
        import tty
        descriptor = sys.stdin.fileno()
        original = termios.tcgetattr(descriptor)
        try:
            tty.setcbreak(descriptor)
            value = sys.stdin.read(1)
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
        if not value:
            raise EOFError
    return value.casefold()


def _prompt_hyphen(issue: HyphenationIssue) -> bool:
    print(f"\nНеоднозначный перенос, страница PDF {issue.page}: {issue.hyphenated}", file=sys.stderr)
    print("Контекст:\n" + issue.context, file=sys.stderr)
    while True:
        print(f"[1] Слить: {issue.joined}   [2] Оставить дефис: {issue.hyphenated}   [Q] Отменить\n> ", end="", file=sys.stderr, flush=True)
        try:
            answer = _read_choice()
        except EOFError as exc:
            raise ConversionError("Ввод закончился до выбора для неоднозначного переноса. Запустите в терминале или явно укажите --hyphens keep / --hyphens join.") from exc
        print(answer, file=sys.stderr)
        if answer == "1":
            return True
        if answer == "2":
            return False
        if answer in ("q", "\x1b", "\x03"):
            raise KeyboardInterrupt
        print("Нажмите 1, 2 или Q.", file=sys.stderr)


def _write_outputs(outputs: dict[Path, str], overwrite: bool) -> None:
    # Check the whole set first: an existing TXT must not leave a new MD behind.
    if not overwrite:
        for target in outputs:
            if target.exists() or target.is_symlink():
                raise ConversionError(f"Файл уже существует: {target}. Для перезаписи укажите --overwrite.")
    staged: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        for target, content in outputs.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=target.parent, prefix=".pdf-translator-", suffix=".tmp", delete=False) as stream:
                stage = Path(stream.name)
                staged.append((stage, target))
                stream.write(content)
        for stage, target in staged:
            if overwrite:
                os.replace(stage, target)
            else:
                # Exclusive creation prevents overwriting even if another process
                # creates the destination after the initial existence check.
                with target.open("x", encoding="utf-8", newline="\n") as stream:
                    created.append(target)
                    stream.write(stage.read_text(encoding="utf-8"))
    except OSError:
        if not overwrite:
            for target in created:
                target.unlink(missing_ok=True)
        raise
    finally:
        for stage, _ in staged:
            stage.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    # Windows pipes may default to a Western code page even on a Russian UI.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Преобразовать PDF в связный Markdown и TXT для перевода (локально, без OCR).")
    parser.add_argument("input", type=Path, help="путь к исходному PDF")
    parser.add_argument("--pages", help="страницы PDF, начиная с 1: 10-25 или 1,3-5")
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="папка результата (по умолчанию: output)")
    parser.add_argument("--overwrite", action="store_true", help="разрешить замену существующих результатов")
    parser.add_argument("--hyphens", choices=("ask", "keep", "join"), default="ask", help="неоднозначные переносы: спросить (по умолчанию), сохранить дефисы или убрать их")
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")
    args = parser.parse_args(argv)
    try:
        targets = [args.output_dir / (args.input.stem + suffix) for suffix in (".md", ".txt")]
        if any(target.resolve() == args.input.resolve() for target in targets):
            raise ConversionError("Путь результата совпадает с исходным PDF. Выберите другую папку результата.")
        if not args.overwrite:
            for target in targets:
                if target.exists() or target.is_symlink():
                    raise ConversionError(f"Файл уже существует: {target}. Для перезаписи укажите --overwrite.")
        resolver = _prompt_hyphen if args.hyphens == "ask" else lambda issue: args.hyphens == "join"
        result = convert_pdf(args.input, pages=args.pages, hyphen_resolver=resolver)
        for warning in result.warnings:
            numbers = ", ".join(map(str, warning.pages))
            print(f"Предупреждение [{warning.code}], стр. {numbers}: {warning.message}", file=sys.stderr)
        _write_outputs(dict(zip(targets, (result.markdown, result.text))), args.overwrite)
        for target in targets:
            print(f"Сохранено: {target.resolve()}")
        return 0
    except KeyboardInterrupt:
        print("\nКонвертация отменена.", file=sys.stderr)
        return 130
    except (ConversionError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
