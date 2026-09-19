"""One Phase B job at a time: an exclusive lock the operating system itself releases (D-277).

The lock is a byte-range lock on a file (``msvcrt.locking`` on Windows,
``flock`` elsewhere), taken without waiting. The operating system drops it
when the process ends however it ends, so a crash never leaves a stale lock
behind - and nothing ever "takes over" a lock whose holder only looks idle,
which for a job that sends requests would be a second sender. A second job
that finds the lock taken sends nothing and says who holds it.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path


class JobLockHeld(RuntimeError):
    """Another process holds the lock."""


class JobLock:
    def __init__(self, path: Path):
        self.path = path
        self.holder_path = path.with_name(path.name + ".holder.json")
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> JobLock:
        """Take the lock now or raise ``JobLockHeld``; never wait for it."""

        if self._fd is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise JobLockHeld(f"{self.path} is held by another process: {self.holder()}") from exc
        self._fd = fd
        try:
            self.holder_path.write_text(json.dumps({
                "pid": os.getpid(), "host": socket.gethostname(), "since": datetime.now(UTC).isoformat()}),
                encoding="utf-8")
        except OSError:
            pass  # who holds it is information, not the lock
        return self

    def holder(self) -> dict | None:
        try:
            return json.loads(self.holder_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> JobLock:
        return self.acquire()

    def __exit__(self, *_exc) -> None:
        self.release()


__all__ = ["JobLock", "JobLockHeld"]
