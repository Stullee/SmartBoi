"""One atomic writer and one quarantining reader, shared by every JSON
store in the system.

Before this module there were five hand-rolled copies of tmp+rename and
five hand-rolled loaders, with divergent `except` clauses. Two problems
followed from that, and both are the kind that only show up on the day
you can least afford them.

**Writes were ordered but not durable.** `tmp.write_text(...)` +
`tmp.replace(path)` guarantees a reader never sees a half-written file,
which is what the original docstrings claimed and is true. It does NOT
guarantee the bytes reached the disk: on a power loss or a hard container
kill the rename can be journalled while the data blocks are still in the
page cache, and the file comes back existing, correctly named, and empty
or truncated. `atomic_write_json` fsyncs the file before the rename and
the directory after it, which closes that window.

**Reads treated "I cannot parse this" as "there was nothing here."**
Every loader logged a warning at most, returned an empty object, and then
the next `save()` overwrote the unreadable bytes with that empty object.
For `graph.json` or a dossier that is months of accumulated evidence
destroyed by a 2 KB truncation, with the only copy gone. `read_json`
instead renames the unreadable file to `<name>.corrupt-<timestamp>` and
records the event, so the bytes survive for a human to look at and the
system can say out loud that it lost something.

It also fixes a narrower crash class. The old `except` clauses caught
`(json.JSONDecodeError, OSError, TypeError)`, which is not enough:
a file whose top-level JSON is a list where a dict was expected raises
`AttributeError` on `.items()`, and a file with invalid UTF-8 raises
`UnicodeDecodeError` (a `ValueError`, but not a `JSONDecodeError`).
Neither was caught, so both propagated out of a constructor, past
`main._amain` -- which catches only `KeyboardInterrupt` -- and killed the
process. With `boot: manual` and no watchdog that is an outage that lasts
until someone notices. `read_json` takes the expected top-level type as an
argument and treats a mismatch as corruption like any other.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class QuarantineEvent:
    """A file that could not be read and was set aside rather than
    overwritten. Held in memory so the engine can alert on it and the
    dashboard can show it -- a silent recovery is how you find out about
    data loss six weeks later, from a hole in a chart."""

    path: str
    quarantined_to: str
    reason: str
    at: str
    bytes_preserved: int = 0


# Appended to by read_json, read by the engine's heartbeat/alert path and
# by status.py. Never cleared automatically: an operator acknowledging
# data loss is a manual act.
quarantine_events: list[QuarantineEvent] = []


def _fsync_dir(directory: Path) -> None:
    """Durably record the rename itself. Without this the file's contents
    are on disk but the directory entry pointing at them may not be, so a
    power loss can leave the new file unreachable under its final name."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # not all platforms/filesystems allow opening a directory
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: Path, obj: Any, *, indent: int | None = None) -> None:
    """Write `obj` to `path` so that a crash at any instant leaves either
    the complete old file or the complete new one -- never a truncation.

    The temp file is `<name>.tmp` appended to the FULL name rather than
    `Path.with_suffix('.tmp')`, which replaces the existing suffix: for
    `dossiers/AAPL.json` the old form produced `AAPL.tmp`, which is fine,
    but for any symbol containing a dot it silently truncated the name and
    two symbols could collide on one temp file. Neither form matches the
    `*.json` glob `DossierStore.all_symbols` uses, so a crash mid-write
    still cannot make a temp file look like a real dossier."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(obj, indent=indent)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def quarantine(path: Path, reason: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    # The stamp has one-second resolution, and a restart loop can hit the
    # same bad file several times a second. os.replace would silently
    # overwrite the earlier copy -- quarantining on top of a quarantine is
    # the same data loss this function exists to prevent, just slower.
    seq = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{seq}")
        seq += 1
    size = 0
    try:
        size = path.stat().st_size
    except OSError:
        pass
    try:
        os.replace(path, target)
    except OSError as exc:
        # Could not even move it. Do NOT fall through to a caller that
        # will overwrite it -- say so loudly and leave the file alone.
        log.error(
            "%s is unreadable (%s) and could not be quarantined (%s). "
            "The file has been left in place; nothing will be written to it "
            "until it is dealt with by hand.", path, reason, exc,
        )
        return
    log.error(
        "%s is unreadable (%s). Moved %d byte(s) to %s and continuing with empty state. "
        "THIS IS DATA LOSS: whatever that file held is no longer live. Inspect the "
        "quarantined copy before deleting it.", path, reason, size, target.name,
    )
    quarantine_events.append(QuarantineEvent(
        path=str(path), quarantined_to=str(target), reason=reason,
        at=datetime.now(timezone.utc).isoformat(), bytes_preserved=size,
    ))


def read_json(path: Path, *, expect: type | tuple[type, ...] = dict) -> Any | None:
    """Return the parsed contents of `path`, or None.

    None means one of two very different things, and the caller almost
    always wants to treat them the same way (start empty) while the
    operator does not:
      - the file does not exist yet -- normal, first run, no event recorded;
      - the file exists and could not be read -- quarantined, event recorded,
        ERROR logged.

    `expect` is the required top-level type. A JSON file that parses fine
    but holds a list where a dict belongs is corruption for our purposes,
    and catching it here is what stops it becoming an `AttributeError` out
    of a constructor."""
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        # An I/O error is not evidence the CONTENT is bad -- the disk could
        # be full or the file briefly locked. Never quarantine on this.
        log.error("Could not read %s (%s). Continuing with empty state; the file is untouched.", path, exc)
        return None
    # Decoded here rather than via read_text(encoding=...) on purpose:
    # UnicodeDecodeError is a ValueError, NOT an OSError, so decoding up
    # in the I/O block would let invalid UTF-8 escape the function
    # entirely -- the exact crash class this module exists to stop.
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError both
        quarantine(path, f"invalid JSON: {exc}")
        return None
    if not isinstance(parsed, expect):
        names = expect.__name__ if isinstance(expect, type) else "/".join(t.__name__ for t in expect)
        quarantine(path, f"top-level JSON is {type(parsed).__name__}, expected {names}")
        return None
    return parsed
