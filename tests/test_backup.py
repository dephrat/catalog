"""Snapshots of the catalog.

The database is the only copy of a personal archive and of tagging that cost
real money. These tests care about one thing above correctness of the happy
path: a snapshot that is silently bad is worse than no snapshot, because it
is only discovered at the moment it is needed.
"""
import gzip
import os
import sqlite3

import backup
import db
from conftest import make_thread


def _seed(user, n=5, tagged=3):
    db.upsert_user(user, "owner@example.com", "Owner", "2024-01-01T00:00:00Z")
    for i in range(n):
        db.upsert_thread(user, make_thread(
            f"t{i}", ai_tags=["tag"] if i < tagged else []))


class TestTakingASnapshot:
    def test_writes_a_verified_snapshot(self, user, tmp_path, capsys):
        _seed(user)
        assert backup.take(keep=5) == 0
        out = capsys.readouterr().out
        assert "verified" in out
        assert "5 threads" in out

    def test_the_snapshot_holds_the_same_data(self, user, tmp_path):
        _seed(user, n=7, tagged=4)
        backup.take(keep=5)
        snapshot = backup.existing()[0]

        plain = backup._unzip_to_temp(snapshot)
        try:
            info = backup._describe(plain)
        finally:
            os.unlink(plain)
        assert info == {"threads": 7, "users": 1, "tagged": 4}

    def test_snapshots_are_gzipped(self, user):
        _seed(user)
        backup.take(keep=5)
        with gzip.open(backup.existing()[0], "rb") as fh:
            assert fh.read(16).startswith(b"SQLite format 3")

    def test_lands_beside_the_database_by_default(self, user, fresh_db):
        _seed(user)
        backup.take(keep=5)
        assert os.path.dirname(backup.existing()[0]) == \
            os.path.dirname(os.path.abspath(fresh_db))

    def test_missing_database_is_reported_not_crashed(self, user, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "absent.db"))
        assert backup.take() == 1


class TestVerification:
    def test_a_corrupt_snapshot_is_rejected(self, user, tmp_path):
        """The point of the whole file: never keep an unreadable backup."""
        _seed(user)
        backup.take(keep=5)
        snapshot = backup.existing()[0]

        with gzip.open(snapshot, "wb") as fh:
            fh.write(b"this is not a database")
        assert backup.verify(snapshot) == 1

    def test_a_good_snapshot_verifies(self, user, capsys):
        _seed(user)
        backup.take(keep=5)
        assert backup.verify(backup.existing()[0]) == 0
        assert "ok" in capsys.readouterr().out

    def test_verifying_something_absent_is_reported(self, user, tmp_path):
        assert backup.verify(str(tmp_path / "nope.db.gz")) == 1

    def test_a_snapshot_short_of_the_live_row_count_is_refused(self, user, monkeypatch):
        """Guards the check itself: if the comparison stopped working, a
        truncated snapshot would be kept and nobody would know."""
        _seed(user, n=5)
        real_describe = backup._describe

        def lying_describe(path):
            info = real_describe(path)
            if info and path != db.DB_PATH:
                info["threads"] -= 1          # snapshot looks short
            return info

        monkeypatch.setattr(backup, "_describe", lying_describe)
        assert backup.take(keep=5) == 1
        assert backup.existing() == [], "a suspect snapshot must not be kept"


class TestRotation:
    def test_keeps_only_the_requested_number(self, user, monkeypatch):
        _seed(user)
        stamps = iter([f"2024010{i}T000000Z" for i in range(1, 7)])

        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                value = next(stamps)
                import datetime as _dt
                return _dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ")

        monkeypatch.setattr(backup, "datetime", FakeDatetime)
        for _ in range(6):
            backup.take(keep=3)
        assert len(backup.existing()) == 3

    def test_pruning_removes_the_oldest(self, user, monkeypatch):
        _seed(user)
        directory = backup._backup_dir()
        for stamp in ["20240101T000000Z", "20240102T000000Z", "20240103T000000Z"]:
            open(os.path.join(directory,
                              f"{backup.PREFIX}{stamp}{backup.SUFFIX}"), "wb").close()
        backup.prune(1)
        remaining = [os.path.basename(p) for p in backup.existing()]
        assert remaining == [f"{backup.PREFIX}20240103T000000Z{backup.SUFFIX}"]

    def test_unrelated_files_are_never_pruned(self, user):
        directory = backup._backup_dir()
        bystander = os.path.join(directory, "something-else.db.gz")
        open(bystander, "wb").close()
        backup.prune(0)
        assert os.path.exists(bystander)


class TestRestore:
    def test_does_nothing_without_confirmation(self, user, monkeypatch, capsys):
        _seed(user, n=5)
        backup.take(keep=5)
        snapshot = backup.existing()[0]

        db.wipe_db(user)
        assert db.count_threads(user) == 0

        monkeypatch.setattr(backup.sys, "argv", ["backup.py", "--restore", snapshot])
        assert backup.restore(snapshot) == 0
        assert "Nothing has changed" in capsys.readouterr().out
        assert db.count_threads(user) == 0, "still not restored"

    def test_restores_with_confirmation(self, user, monkeypatch):
        _seed(user, n=5)
        backup.take(keep=5)
        snapshot = backup.existing()[0]

        db.wipe_db(user)
        assert db.count_threads(user) == 0

        monkeypatch.setattr(backup.sys, "argv",
                            ["backup.py", "--restore", snapshot, "--yes"])
        assert backup.restore(snapshot) == 0
        assert db.count_threads(user) == 5

    def test_the_replaced_database_is_kept(self, user, monkeypatch):
        """Restoring the wrong snapshot must itself be recoverable."""
        _seed(user, n=5)
        backup.take(keep=5)
        snapshot = backup.existing()[0]
        db.upsert_thread(user, make_thread("added-later"))

        monkeypatch.setattr(backup.sys, "argv",
                            ["backup.py", "--restore", snapshot, "--yes"])
        backup.restore(snapshot)

        directory = os.path.dirname(os.path.abspath(db.DB_PATH))
        replaced = [n for n in os.listdir(directory) if ".replaced-" in n]
        assert replaced, "the pre-restore database must be preserved"
        conn = sqlite3.connect(os.path.join(directory, replaced[0]))
        kept = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        conn.close()
        assert kept == 6, "including the row that only existed before the restore"

    def test_a_corrupt_snapshot_is_never_restored(self, user, monkeypatch):
        _seed(user, n=5)
        backup.take(keep=5)
        snapshot = backup.existing()[0]
        with gzip.open(snapshot, "wb") as fh:
            fh.write(b"garbage")

        monkeypatch.setattr(backup.sys, "argv",
                            ["backup.py", "--restore", snapshot, "--yes"])
        assert backup.restore(snapshot) == 1
        assert db.count_threads(user) == 5, "the live database is untouched"


class TestConsistencyUnderWrites:
    def test_snapshot_is_consistent_while_the_database_is_written(self, user):
        """cp on a live SQLite file can capture a half-applied write. The
        online backup API takes a read lock per page instead, so writes
        neither block it nor tear it."""
        import threading

        _seed(user, n=20)
        stop = threading.Event()

        def keep_writing():
            i = 0
            while not stop.is_set():
                db.upsert_thread(user, make_thread(f"churn-{i}"))
                i += 1

        writer = threading.Thread(target=keep_writing, daemon=True)
        writer.start()
        try:
            backup._snapshot_to(os.path.join(backup._backup_dir(), "under-load.db"))
        finally:
            stop.set()
            writer.join(timeout=5)

        info = backup._describe(os.path.join(backup._backup_dir(), "under-load.db"))
        assert info is not None, "snapshot must be readable"
        assert info["threads"] >= 20, "and internally consistent"
