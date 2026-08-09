"""A persisted JSON dict -- used for ingestion cursors (last-seen filing
date / news timestamp per symbol) and any other flat key-value snapshot that
just needs to survive a restart. Writes atomically (tmp file + fsync +
rename) so a crash mid-write -- OR an unclean power-off, the characteristic
Home-Assistant-on-a-Pi/SD-card failure -- cannot leave a half-written or
truncated state file. A file that is nonetheless found unreadable is
QUARANTINED (renamed to <name>.corrupt-<timestamp>) and logged loudly before
starting fresh, so the data is recoverable by hand and the loss is never
silent -- a silent wipe of periodic_pass_state re-fires every daily pass, and
of accepted_candidates reverts the whole runtime universe."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


def quarantine_corrupt_file(path: Path, exc: Exception) -> None:
    """Rename an unreadable data file aside instead of letting the next save
    silently overwrite it, and log loudly. Shared by JsonState, DossierStore
    and RelationshipGraph -- all three previously degraded a corrupt file to
    'start fresh' and let the next write clobber the original with no trace,
    which for a dossier is the permanent loss of accumulated evidence. Never
    raises: quarantine is best-effort recovery, so a failure to rename must not
    take down the caller on top of the corruption it is already handling."""
    try:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        quarantined = path.with_name(f"{path.name}.corrupt-{stamp}")
        path.replace(quarantined)
        log.warning(
            "Data file %s was unreadable (%s) -- quarantined to %s and starting fresh. "
            "The original is recoverable by hand; it was NOT silently overwritten.",
            path, exc, quarantined,
        )
    except OSError as move_exc:
        log.warning(
            "Data file %s was unreadable (%s) and could not be quarantined (%s) -- starting "
            "fresh; the original may be overwritten on the next save.",
            path, exc, move_exc,
        )


def atomic_write_json(path: Path, payload, *, indent: int | None = None) -> None:
    """Write `payload` as JSON to `path` durably: write a tmp file, flush and
    fsync its bytes, rename it over the target, then fsync the directory so the
    rename itself survives a power loss. Rename is atomic against other
    readers, but rename atomicity does NOT imply the new file's DATA is durable
    -- on an unclean power-off the rename can persist while the blocks are
    still in the page cache, leaving a truncated file the next start would
    quarantine. The fsyncs close that window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # best-effort; not every filesystem/platform supports directory fsync


class JsonState:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                quarantine_corrupt_file(path, exc)
                self.data = {}

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        self.data[key] = value
        self._save()

    def update(self, mapping: dict) -> None:
        """Set several keys with a SINGLE durable write, rather than one fsync
        per key -- for callers that persist a small group of related fields
        together (see engine's retry-state persistence)."""
        self.data.update(mapping)
        self._save()

    def overwrite(self, data: dict) -> None:
        """Replace the whole dict with one durable write -- for a bulk prune
        that drops many keys at once (one fsync instead of one per delete)."""
        self.data = dict(data)
        self._save()

    def delete(self, key: str) -> None:
        if key in self.data:
            del self.data[key]
            self._save()

    def _save(self) -> None:
        atomic_write_json(self.path, self.data)
