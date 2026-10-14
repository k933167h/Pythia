"""Tamper-evident audit trail (append-only, SHA-256 hash chain).

What gets recorded — the events an ISO/IEC 42001 auditor or a NIST AI RMF
GOVERN/MANAGE review asks for:
  * every oracle pass: model, prompt version, input hash, output hash, counts;
  * every privileged change: model switch, webhook add/remove, alert rules,
    loop on/off, human resolution overrides;
  * security events: rejected auth, refused webhook targets, budget exhaustion.

Each line carries `prev` (the previous line's hash) and `hash` (SHA-256 of its own
content), so deleting or editing a line breaks the chain at that point.
GET /governance/audit/verify re-walks it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

from .config import CONFIG
from .models import now_ms

log = logging.getLogger("pythia.audit")


class AuditLog:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or (CONFIG.runs_dir / "audit.jsonl")
        self._lock = threading.Lock()
        self._last = self._tail_hash()

    def _tail_hash(self) -> str:
        """Hash of the last complete record, reading backwards in growing windows so a
        long final line can't silently restart the chain."""
        try:
            with self.path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                window = 8192
                while True:
                    f.seek(max(0, size - window))
                    lines = [ln for ln in f.read().decode("utf-8", "replace").splitlines() if ln.strip()]
                    # the first line of a partial window may be truncated; we need ≥2 or the whole file
                    if not lines:
                        return ""
                    if len(lines) >= 2 or window >= size:
                        return json.loads(lines[-1]).get("hash", "")
                    window *= 4
        except (OSError, ValueError, IndexError):
            return ""

    @staticmethod
    def _digest(rec: dict) -> str:
        body = {k: v for k, v in rec.items() if k != "hash"}
        return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    def record(self, action: str, actor: str = "system", **detail: Any) -> dict:
        with self._lock:
            rec = {"ts": now_ms(), "action": action, "actor": actor, "detail": detail, "prev": self._last}
            rec["hash"] = self._digest(rec)
            try:
                with self.path.open("a") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self._last = rec["hash"]
            except OSError as e:
                log.warning("audit write failed: %s", e)
            return rec

    def verify(self) -> dict:
        prev, n, broken = "", 0, []
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return {"records": 0, "ok": True, "broken": []}
        for i, ln in enumerate(lines):
            if not ln.strip():
                continue
            n += 1
            try:
                rec = json.loads(ln)
            except ValueError:
                broken.append(i)
                prev = ""
                continue
            if rec.get("prev") != prev or rec.get("hash") != self._digest(rec):
                broken.append(i)
            prev = rec.get("hash", "")
        return {"records": n, "ok": not broken, "broken": broken[:50]}

    def tail(self, limit: int = 100, action: Optional[str] = None) -> list[dict]:
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if action and rec.get("action") != action:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out


AUDIT = AuditLog()
