"""A persisted JSON dict -- used for ingestion cursors (last-seen filing
date / news timestamp per symbol) and any other flat key-value snapshot
that just needs to survive a restart.

Durability and corrupt-file handling both live in persist.py now: writes
are fsynced rather than merely renamed, and an unreadable file is
quarantined instead of being silently replaced with {} on the next set().
The old inline loader also had a quieter bug -- it caught only
JSONDecodeError/OSError, so a state file holding a top-level LIST loaded
without complaint and then raised AttributeError on the first .get()."""
from __future__ import annotations

from pathlib import Path

from smartboi.persist import atomic_write_json, read_json


class JsonState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = read_json(path, expect=dict) or {}

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
        atomic_write_json(self.path, self.data)
