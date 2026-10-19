"""CLI-owned JSONL files with bounded rotation/retention; no third-party handlers."""

import json
import os
import re
import stat
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock


_OWNED = re.compile(r"run-\d{8}T\d{6}Z-[a-f0-9]{32}-\d{6}\.jsonl")


def _lock_file(fd):
    if os.name == "nt":
        import msvcrt
        # Lock a reserved byte beyond bounded log data, not its first byte:
        # Windows byte-range locks must not prevent readers from tailing JSONL.
        position = os.lseek(fd, 0, os.SEEK_CUR)
        try:
            os.lseek(fd, 1 << 30, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        finally:
            os.lseek(fd, position, os.SEEK_SET)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class JsonlSink:
    """One writer per unique run, thread-safe; held file locks protect retention.

    Up to three 1 MiB parts per run. Startup/rotation prune inactive owned files
    older than 14 days, then oldest inactive files toward a 60-file directory cap.
    Active files, symlinks and unrelated filenames are never removed.
    """

    def __init__(self, directory, *, max_bytes=1024 * 1024, parts=3,
                 max_files=60, retention_days=14, clock=time.time):
        if min(max_bytes, parts, max_files, retention_days) < 1:
            raise ValueError("Invalid log limits")
        self.directory = Path(directory).absolute()
        self._check_directory()
        self.directory = self.directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._check_directory()
        self.max_bytes, self.parts, self.max_files = max_bytes, parts, max_files
        self.retention_seconds, self.clock = retention_days * 86400, clock
        self._lock, self._fd, self._size = RLock(), None, 0
        self._paths, self._part = deque(), 0
        self._prefix = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
        try:
            self._open_part()
        except BaseException:
            self.close()
            raise

    def _check_directory(self):
        if any(p.is_symlink() or p.is_junction() for p in (self.directory, *self.directory.parents)):
            raise OSError("Unsafe log directory")
        if self.directory.exists() and not self.directory.is_dir():
            raise OSError("Log directory is not a directory")

    @property
    def path(self):
        return self._paths[-1]

    def _open_part(self):
        self._check_directory()
        self._part += 1
        path = self.directory / f"{self._prefix}-{self._part:06d}.jsonl"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            _lock_file(fd)
        except BaseException:
            os.close(fd)
            raise
        if self._fd is not None:
            os.close(self._fd)
        self._fd, self._size = fd, 0
        self._paths.append(path)
        while len(self._paths) > self.parts:
            old = self._paths.popleft()
            if (old.parent == self.directory and not old.is_symlink() and old.is_file()
                    and old.stat().st_nlink == 1):
                old.unlink(missing_ok=True)
        self._prune()

    def _prune(self):
        self._check_directory()
        candidates = []
        for path in self.directory.iterdir():
            if _OWNED.fullmatch(path.name) and not path.is_symlink():
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    candidates.append((info.st_mtime, path, info))
        candidates.sort(key=lambda item: (item[0], item[1].name))
        remaining = len(candidates)
        for modified, path, before in candidates:
            if path == self.path or (modified >= self.clock() - self.retention_seconds and remaining <= self.max_files):
                continue
            fd = None
            try:
                fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                _lock_file(fd)  # Skip another active writer, including other processes.
                after = os.fstat(fd)
                if ((before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                        or path.is_symlink() or path.resolve().parent != self.directory):
                    continue
                os.close(fd)
                fd = None
                # Writers create filenames exclusively and never reopen old parts.
                path.unlink(missing_ok=True)
                remaining -= 1
            except OSError:
                pass
            finally:
                if fd is not None:
                    os.close(fd)

    def write(self, record):
        data = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            if self._fd is None:
                raise OSError("Log closed")
            if len(data) > self.max_bytes:
                raise OSError("Record exceeds log part size")
            if self._size and self._size + len(data) > self.max_bytes:
                self._open_part()
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                if written <= 0:
                    raise OSError("Log write failed")
                view = view[written:]
                self._size += written

    def close(self):
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
