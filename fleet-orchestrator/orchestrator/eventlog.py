"""JSONL event log: one line per event, assignment, plan change and job completion.

Owner: Person A. Records are also kept in memory so the server can show recent ones.
"""
from __future__ import annotations

import json
from pathlib import Path


class EventLog:
    def __init__(self, path: str | Path | None = None, dispatcher: str = ""):
        self.dispatcher = dispatcher
        self.records: list[dict] = []
        self._fh = None
        if path is not None:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._fh = p.open("w", encoding="utf-8")

    def write(self, t: int, kind: str, **data) -> dict:
        rec = {"t": t, "kind": kind, "dispatcher": self.dispatcher, **data}
        self.records.append(rec)
        if self._fh:
            self._fh.write(json.dumps(rec, default=str) + "\n")
            self._fh.flush()
        return rec

    def recent(self, n: int = 50) -> list[dict]:
        return self.records[-n:]

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
