"""Local negative availability cache and short probe leases. No document/key data."""

import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .models import TranslationError


def default_database_path() -> Path:
    checkout = Path(__file__).resolve().parents[2]
    return (checkout if (checkout / "pyproject.toml").is_file() else Path.cwd()) / "model-availability.sqlite3"


@dataclass(frozen=True)
class Cooldown:
    reason: str
    retry_at: float
    basis: str


class AvailabilityCache:
    """Connections are short-lived; no transaction spans a network call.

    Cache deletion is safe when no process is using it. An unknown schema or
    damaged database is refused, never silently overwritten or recreated.
    """

    def __init__(self, path: str | Path, *, clock=time.time):
        self.path, self.clock = Path(path).absolute(), clock
        if (self.path.is_symlink() or not self.path.parent.is_dir()
                or self.path.exists() and not self.path.is_file()):
            raise TranslationError("Небезопасный путь SQLite-кэша или отсутствует его каталог.")
        for suffix in ("-journal", "-wal", "-shm"):
            if Path(str(self.path) + suffix).is_symlink():
                raise TranslationError("Служебный файл SQLite не может быть символической ссылкой.")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise TranslationError("Неизвестная версия SQLite-кэша. Укажите другой --availability-db.")
            if version == 0:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise TranslationError("SQLite-файл занят другой базой; перезапись запрещена.")
                db.execute("CREATE TABLE cooldowns (namespace TEXT NOT NULL, model TEXT NOT NULL, reason TEXT NOT NULL, observed_at REAL NOT NULL, retry_at REAL NOT NULL, basis TEXT NOT NULL, PRIMARY KEY(namespace, model))")
                db.execute("CREATE TABLE aliases (namespace TEXT NOT NULL, alias TEXT NOT NULL, canonical TEXT NOT NULL, PRIMARY KEY(namespace, alias))")
                db.execute("CREATE TABLE leases (namespace TEXT NOT NULL, model TEXT NOT NULL, owner TEXT NOT NULL, expires REAL NOT NULL, PRIMARY KEY(namespace, model))")
                db.execute("PRAGMA user_version=1")

    @contextmanager
    def _connection(self):
        db = None
        try:
            if self.path.is_symlink():
                raise TranslationError("SQLite-кэш не может быть символической ссылкой.")
            db = sqlite3.connect(str(self.path), timeout=5)
            db.execute("PRAGMA trusted_schema=OFF")
            yield db
            db.commit()
        except sqlite3.Error:
            if db is not None:
                db.rollback()
            raise TranslationError("SQLite-кэш повреждён, занят или недоступен. Проверьте --availability-db; прогресс перевода хранится отдельно.") from None
        finally:
            if db is not None:
                db.close()

    def canonical(self, namespace: str, model: str) -> str:
        with self._connection() as db:
            seen = {model}
            for _ in range(8):
                row = db.execute("SELECT canonical FROM aliases WHERE namespace=? AND alias=?", (namespace, model)).fetchone()
                if not row or row[0] in seen:
                    break
                model = row[0]
                seen.add(model)
            return model

    def remember_alias(self, namespace: str, alias: str, canonical: str) -> None:
        if alias != canonical:
            with self._connection() as db:
                db.execute("INSERT INTO aliases VALUES (?,?,?) ON CONFLICT(namespace,alias) DO UPDATE SET canonical=excluded.canonical", (namespace, alias, canonical))

    def blocked(self, namespace: str, model: str) -> Cooldown | None:
        model = self.canonical(namespace, model)
        with self._connection() as db:
            row = db.execute("SELECT reason,retry_at,basis FROM cooldowns WHERE namespace=? AND model IN (?, '*') AND retry_at>? ORDER BY retry_at DESC LIMIT 1", (namespace, model, self.clock())).fetchone()
        if row:
            if not math.isfinite(row[1]):
                raise TranslationError("Некорректный срок ограничения в SQLite-кэше.")
            return Cooldown(*row)
        return None

    def defer(self, namespace: str, model: str, *, reason: str, retry_at: float, basis: str) -> None:
        if not math.isfinite(retry_at) or retry_at <= self.clock():
            raise TranslationError("Некорректный срок повторной попытки.")
        model = self.canonical(namespace, model)
        with self._connection() as db:
            db.execute("INSERT INTO cooldowns VALUES (?,?,?,?,?,?) ON CONFLICT(namespace,model) DO UPDATE SET reason=excluded.reason, observed_at=excluded.observed_at, retry_at=MAX(cooldowns.retry_at,excluded.retry_at), basis=excluded.basis", (namespace, model, reason, self.clock(), retry_at, basis))

    def succeeded(self, namespace: str, model: str, *, started_at: float) -> None:
        model = self.canonical(namespace, model)
        with self._connection() as db:
            # A successful older in-flight request cannot erase a newer failure.
            db.execute("DELETE FROM cooldowns WHERE namespace=? AND model=? AND observed_at<=?", (namespace, model, started_at))

    def claim(self, namespace: str, model: str, *, seconds: float, ignore_cooldown=False) -> str | None:
        model = self.canonical(namespace, model)
        now, owner = self.clock(), uuid.uuid4().hex
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not ignore_cooldown and db.execute("SELECT 1 FROM cooldowns WHERE namespace=? AND model IN (?, '*') AND retry_at>?", (namespace, model, now)).fetchone():
                return None
            db.execute("DELETE FROM leases WHERE expires<=?", (now,))
            if db.execute("SELECT 1 FROM leases WHERE namespace=? AND model=?", (namespace, model)).fetchone():
                return None
            db.execute("INSERT INTO leases VALUES (?,?,?,?)", (namespace, model, owner, now + seconds))
        return owner

    def release(self, namespace: str, model: str, owner: str) -> None:
        # Release using the original name: alias discovery may happen mid-call.
        with self._connection() as db:
            db.execute("DELETE FROM leases WHERE namespace=? AND owner=?", (namespace, owner))

    def reset(self, namespace: str) -> None:
        with self._connection() as db:
            db.execute("DELETE FROM cooldowns WHERE namespace=?", (namespace,))
            db.execute("DELETE FROM aliases WHERE namespace=?", (namespace,))
