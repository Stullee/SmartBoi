"""A persisted JSON dict -- used for ingestion cursors (last-seen filing
date / news timestamp per symbol) and any other flat key-value snapshot
that just needs to survive a restart. Writes atomically (tmp file + rename)
so a crash mid-write can never leave a half-written, corrupt state file."""
from __future__ import annotations

import json
from pathlib import Path


class JsonState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self._save()

    def delete(self, key: str) -> None:
        if key in self.data:
            del self.data[key]
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)
