"""Crash-safe file writes.

A plain write_text truncates the file first: a crash or a full disk mid-write leaves
it empty or half-written, and the next load treats it as "no data" — the next save then
permanently replaces every alert rule, webhook or watchlist entry. Write to a temp file
in the same directory, fsync, then os.replace (atomic on POSIX and Windows).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def atomic_write_text(path: Path, text: str, mode: Optional[int] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any, indent: int = 1, mode: Optional[int] = None) -> None:
    atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=False), mode=mode)
