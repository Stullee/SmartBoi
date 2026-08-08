"""Nightly backup: the archive itself, and the retention rule that deletes.

Retention gets the most attention here because it is the only part of a
backup system that destroys things, and a retention bug is invisible until
the day you reach for a tarball that was quietly pruned.
"""
from __future__ import annotations

import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from smartboi.backup import (
    BACKUP_DIR_NAME, KEEP_DAILY, KEEP_WEEKLY, MIN_KEPT,
    parse_stamp, run_backup, select_expired,
)

NOW = datetime(2026, 8, 8, 3, 0, tzinfo=timezone.utc)


def _touch(directory: Path, when: datetime) -> Path:
    path = directory / f"smartboi-{when.strftime('%Y%m%dT%H%M%SZ')}.tar.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


# --- retention --------------------------------------------------------

def test_parse_stamp_ignores_anything_that_is_not_ours(tmp_path):
    assert parse_stamp(tmp_path / "notes.txt") is None
    assert parse_stamp(tmp_path / "smartboi-nonsense.tar.gz") is None
    assert parse_stamp(tmp_path / "smartboi-20260808T030000Z.tar.gz") == NOW


def test_the_newest_dailies_are_always_kept(tmp_path):
    made = [_touch(tmp_path, NOW - timedelta(days=d)) for d in range(KEEP_DAILY)]
    assert select_expired(made, NOW) == []


def test_older_backups_thin_to_one_per_week(tmp_path):
    # One per day for 10 weeks: far more than KEEP_DAILY.
    made = [_touch(tmp_path, NOW - timedelta(days=d)) for d in range(70)]
    expired = set(select_expired(made, NOW))
    kept = [p for p in made if p not in expired]

    assert len(kept) < len(made), "something must be pruned"
    # Every backup inside the daily window survives...
    daily_cutoff = NOW - timedelta(days=KEEP_DAILY)
    for path in made:
        if parse_stamp(path) >= daily_cutoff:
            assert path not in expired
    # ...and outside it, at most one per ISO week is kept.
    weeks = [parse_stamp(p).isocalendar()[:2] for p in kept if parse_stamp(p) < daily_cutoff]
    assert len(weeks) == len(set(weeks)), "more than one backup kept for a single week"
    # ...and nothing older than the weekly window survives at all.
    assert all(parse_stamp(p) >= NOW - timedelta(weeks=KEEP_WEEKLY) for p in kept)


def test_backups_older_than_the_weekly_window_are_all_expired(tmp_path):
    ancient = _touch(tmp_path, NOW - timedelta(weeks=KEEP_WEEKLY + 4))
    recent = [_touch(tmp_path, NOW - timedelta(days=d)) for d in range(3)]
    expired = select_expired([ancient, *recent], NOW)
    assert expired == [ancient]


def test_retention_ignores_unrelated_files(tmp_path):
    stray = tmp_path / "README.txt"
    stray.write_text("not a backup")
    old = _touch(tmp_path, NOW - timedelta(weeks=KEEP_WEEKLY + 4))
    # Enough recent backups that `old` is not held back by the MIN_KEPT floor.
    recent = [_touch(tmp_path, NOW - timedelta(days=d)) for d in range(MIN_KEPT)]
    assert select_expired([stray, old, *recent], NOW) == [old]


def test_the_floor_refuses_to_delete_the_last_few_however_old(tmp_path):
    """A container that starts before NTP settles sees every backup as
    ancient. Deleting the lot in one pass is the one outcome a backup
    system must never have."""
    ancient = [_touch(tmp_path, NOW - timedelta(weeks=52 + w)) for w in range(MIN_KEPT)]
    assert select_expired(ancient, NOW) == []


# --- the archive ------------------------------------------------------

def test_backup_archives_both_trees(tmp_path):
    data, logs = tmp_path / "data", tmp_path / "logs"
    (data / "dossiers").mkdir(parents=True)
    (data / "dossiers" / "FORM.json").write_text('{"symbol": "FORM"}')
    (data / "graph.json").write_text("[]")
    logs.mkdir()
    (logs / "paper_trades.jsonl").write_text('{"symbol": "FORM"}\n')

    target = run_backup([data, logs], tmp_path / BACKUP_DIR_NAME, now=NOW)

    assert target is not None and target.exists()
    with tarfile.open(target) as tar:
        names = set(tar.getnames())
    assert "data/graph.json" in names
    assert "data/dossiers/FORM.json" in names
    assert "logs/paper_trades.jsonl" in names


def test_backup_excludes_temp_files_that_would_restore_as_truncated_stores(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "graph.json").write_text("[]")
    (data / "graph.json.tmp").write_text("[half-writ")

    target = run_backup([data], tmp_path / BACKUP_DIR_NAME, now=NOW)
    with tarfile.open(target) as tar:
        names = set(tar.getnames())
    assert "data/graph.json" in names
    assert "data/graph.json.tmp" not in names


def test_backup_never_archives_itself(tmp_path):
    """The difference between generational backups and a tarball that
    doubles in size every night."""
    data = tmp_path / "data"
    (data / BACKUP_DIR_NAME).mkdir(parents=True)
    (data / "graph.json").write_text("[]")
    (data / BACKUP_DIR_NAME / "smartboi-20260101T000000Z.tar.gz").write_bytes(b"old" * 1000)

    target = run_backup([data], data / BACKUP_DIR_NAME, now=NOW)
    with tarfile.open(target) as tar:
        names = tar.getnames()
    assert not any(BACKUP_DIR_NAME in n for n in names), names


def test_backup_prunes_while_it_writes(tmp_path):
    backups = tmp_path / BACKUP_DIR_NAME
    data = tmp_path / "data"
    data.mkdir()
    (data / "graph.json").write_text("[]")
    stale = _touch(backups, NOW - timedelta(weeks=KEEP_WEEKLY + 4))
    for d in range(1, MIN_KEPT + 1):  # clear the MIN_KEPT floor
        _touch(backups, NOW - timedelta(days=d))

    run_backup([data], backups, now=NOW)
    assert not stale.exists()


def test_nothing_to_back_up_is_not_a_failure(tmp_path):
    assert run_backup([tmp_path / "absent"], tmp_path / BACKUP_DIR_NAME, now=NOW) is None


def test_a_failed_write_leaves_no_partial_restore_point(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "graph.json").write_text("[]")

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(tarfile, "open", _boom)
    assert run_backup([data], tmp_path / BACKUP_DIR_NAME, now=NOW) is None
    assert list((tmp_path / BACKUP_DIR_NAME).glob("*")) == []
