"""storage.py — File locking and atomic JSON writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - non-POSIX platforms
    fcntl = None


class FileLock:
    """Simple exclusive file lock using fcntl on POSIX."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None

    def __enter__(self) -> "FileLock":
        # Ensure lock file exists in the same directory.
        self._fh = open(self._path, "a+")
        if fcntl:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh:
            if fcntl:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    """Write JSON atomically (tmp + fsync + replace)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    with open(path) as f:
        return json.load(f)
