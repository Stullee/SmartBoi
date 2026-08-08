"""Nightly tarball of the irreplaceable state, with generational retention.

Everything this system is for lives in two directories -- `data/` (the
relationship graph, every dossier, the dedup index, accepted candidates,
open paper trades) and `logs/` (the append-only forward record: paper
trades, signals, decisions, dossier snapshots, price marks). None of it can
be regenerated. There is no replay path: no raw filing or article text is
ever cached, so a lost dossier cannot be rebuilt even with the same code
and the same API keys, and a lost snapshot row is a lost day of a multi-year
measurement that only ever runs forward.

Until this module existed there was exactly one copy, on one host, and the
loaders treated an unreadable file as an empty one. persist.py fixed the
second half of that. This is the first half.

Deliberately boring: a gzipped tar written next to the data it copies, on
the same mapped share, so it is visible over Samba/the File Editor and is
picked up by a Home Assistant partial backup of /config without any extra
configuration. It is NOT off-box. An operator still has to pull a copy
somewhere else, and the log line says so on the first run of each day.
"""
from __future__ import annotations

import logging
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

BACKUP_DIR_NAME = "backups"
_PREFIX = "smartboi-"
_SUFFIX = ".tar.gz"
# 14 dailies covers "I broke it last week and only noticed now"; 8 weeklies
# covers "this has been wrong since before the last release" without keeping
# a year of tarballs on a Home Assistant host's storage.
KEEP_DAILY = 14
KEEP_WEEKLY = 8
# Never prune below this many, whatever the clock says (see select_expired).
MIN_KEPT = 3


def _stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def parse_stamp(path: Path) -> datetime | None:
    name = path.name
    if not name.startswith(_PREFIX) or not name.endswith(_SUFFIX):
        return None
    try:
        return datetime.strptime(name[len(_PREFIX):-len(_SUFFIX)], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def select_expired(backups: list[Path], now: datetime) -> list[Path]:
    """Which tarballs to delete: everything except the last KEEP_DAILY
    days, one per ISO week for the last KEEP_WEEKLY weeks, and an absolute
    floor of the newest MIN_KEPT whatever their age.

    Retention is by AGE, not by count. "Keep the newest 14" reads the same
    in the normal case and is wrong in every abnormal one: after a month of
    downtime the newest 14 files span months and none of them are dailies,
    so a rule phrased in counts quietly stops meaning what it says exactly
    when the deployment is already in trouble.

    The MIN_KEPT floor is the guard in the other direction -- a clock that
    jumps forward (a container starting before NTP settles) would otherwise
    make every existing backup look ancient and delete the lot in one pass.

    Pure function over paths and a clock: retention is the only part of a
    backup system that destroys things, and it should be testable without
    writing a single tar."""
    dated = sorted(
        ((p, ts) for p in backups if (ts := parse_stamp(p)) is not None),
        key=lambda pair: pair[1], reverse=True,
    )
    daily_cutoff = now - timedelta(days=KEEP_DAILY)
    weekly_cutoff = now - timedelta(weeks=KEEP_WEEKLY)

    keep: set[Path] = {p for p, _ in dated[:MIN_KEPT]}
    seen_weeks: set[tuple[int, int]] = set()
    for path, ts in dated:  # newest first, so the first hit in a week wins
        if ts >= daily_cutoff:
            keep.add(path)
            continue
        if ts < weekly_cutoff:
            continue
        week = ts.isocalendar()[:2]
        if week not in seen_weeks:
            seen_weeks.add(week)
            keep.add(path)
    return [p for p, _ in dated if p not in keep]


def run_backup(
    sources: list[Path],
    backup_dir: Path,
    now: datetime | None = None,
) -> Path | None:
    """Write one tarball of `sources` into `backup_dir` and prune old ones.

    Returns the tarball path, or None if there was nothing to back up or
    the write failed. Failure is never fatal: a backup that cannot be
    written must not take down an engine that is otherwise healthy, and the
    ERROR log plus the missing file are the alarm.

    `backup_dir` is excluded from its own archive even when it sits inside
    a source tree, which is the difference between generational backups and
    a tarball that doubles in size every night."""
    now = now or datetime.now(timezone.utc)
    present = [s for s in sources if s.exists()]
    if not present:
        log.info("[BACKUP] Nothing to back up yet (no data/ or logs/ directory).")
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{_PREFIX}{_stamp(now)}{_SUFFIX}"
    resolved_backup_dir = backup_dir.resolve()

    def _keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # Never archive a half-written temp file from
        # persist.atomic_write_json -- restoring one would look like a real
        # store with truncated contents. Nor the backup directory itself,
        # for the case where an operator has pointed it inside data/.
        if info.name.endswith(".tmp") or info.name.endswith(".part"):
            return None
        if f"/{BACKUP_DIR_NAME}/" in f"/{info.name}/":
            return None
        return info

    # Written to a .part file and renamed, for the same reason every other
    # write in this system is: a crash mid-tar must not leave something that
    # looks like a valid restore point.
    partial = target.with_name(target.name + ".part")
    try:
        with tarfile.open(partial, "w:gz") as tar:
            for source in present:
                resolved = source.resolve()
                if resolved_backup_dir == resolved:
                    continue
                tar.add(source, arcname=source.name, filter=_keep)
        partial.replace(target)
    except (OSError, tarfile.TarError) as exc:
        log.error("[BACKUP] Could not write %s: %s. The engine continues, but there is "
                  "no fresh copy of the data tonight.", target.name, exc)
        partial.unlink(missing_ok=True)
        return None

    size_mb = target.stat().st_size / 1_048_576
    expired = select_expired(list(backup_dir.glob(f"{_PREFIX}*{_SUFFIX}")), now)
    for old in expired:
        try:
            old.unlink()
        except OSError:
            log.warning("[BACKUP] Could not remove expired backup %s.", old.name)
    log.info(
        "[BACKUP] Wrote %s (%.1f MB), pruned %d expired. Keeping %d daily + %d weekly. "
        "NOTE: this is on the same host as the original -- copy it somewhere else.",
        target.name, size_mb, len(expired), KEEP_DAILY, KEEP_WEEKLY,
    )
    return target
