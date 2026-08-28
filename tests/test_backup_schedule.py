"""The scheduler that takes snapshots without being asked.

backup.py already proves a snapshot is consistent and verified. What is left
to get wrong is *whether one ever happens*: a scheduler that quietly stops is
worse than no scheduler, because the operator believes they are covered. These
tests are mostly about the ways it could go silent.
"""
import os
import time

import pytest

import app
import backup
import db
from conftest import make_thread


def _seed(user):
    db.upsert_user(user, "owner@example.com", "Owner", "2024-01-01T00:00:00Z")
    db.upsert_thread(user, make_thread("t1", ai_tags=["tag"]))


def _snapshot_file(age_hours=0.0):
    """A plausible snapshot on disk, without paying to take a real one."""
    path = os.path.join(backup._backup_dir(),
                        f"{backup.PREFIX}20240101T000000Z{backup.SUFFIX}")
    open(path, "wb").close()
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))
    return path


@pytest.fixture
def scheduled(monkeypatch):
    """Backups on, every 24h — the deployed default."""
    monkeypatch.setattr(app, "BACKUP_INTERVAL_HOURS", 24.0)
    monkeypatch.setattr(app, "_backup_status", {"last_error": None, "last_run": None})
    monkeypatch.setattr(app, "sync_running", {})


class TestWhenABackupIsDue:
    def test_due_when_none_has_ever_been_taken(self, user, scheduled):
        assert app.newest_backup_age_seconds() is None
        assert app.backup_due() is True

    def test_not_due_while_one_is_fresh(self, user, scheduled):
        _snapshot_file(age_hours=1)
        assert app.backup_due() is False

    def test_due_once_the_newest_passes_the_interval(self, user, scheduled):
        _snapshot_file(age_hours=25)
        assert app.backup_due() is True

    def test_the_boundary_belongs_to_due(self, user, scheduled):
        _snapshot_file(age_hours=24.001)
        assert app.backup_due() is True

    def test_age_comes_from_the_disk_not_from_a_counter(self, user, scheduled):
        """A scheduler that died must show an ageing backup, not the last
        reassuring number it managed to record."""
        _snapshot_file(age_hours=10)
        app._backup_status["last_run"] = time.time()
        assert app.newest_backup_age_seconds() == pytest.approx(10 * 3600, abs=5)

    def test_never_due_when_the_interval_is_zero(self, user, monkeypatch):
        monkeypatch.setattr(app, "BACKUP_INTERVAL_HOURS", 0.0)
        assert app.backup_due() is False


class TestTakingOne:
    def test_takes_a_real_verified_snapshot_when_due(self, user, scheduled):
        _seed(user)
        assert app.run_scheduled_backup() is True
        assert len(backup.existing()) == 1
        assert backup.verify(backup.existing()[0]) == 0

    def test_does_nothing_when_not_due(self, user, scheduled, monkeypatch):
        _seed(user)
        _snapshot_file(age_hours=1)
        called = []
        monkeypatch.setattr(backup, "take", lambda **kw: called.append(1) or 0)
        assert app.run_scheduled_backup() is False
        assert called == []

    def test_honours_the_keep_setting(self, user, scheduled, monkeypatch):
        _seed(user)
        monkeypatch.setattr(app, "BACKUP_KEEP", 3)
        seen = {}
        monkeypatch.setattr(backup, "take", lambda keep: seen.setdefault("keep", keep) or 0)
        app.run_scheduled_backup()
        assert seen["keep"] == 3


class TestSyncInFlight:
    def test_skips_while_a_sync_is_running(self, user, scheduled):
        """Not for correctness — the online backup API is safe under writes —
        but a large import should not fight the snapshot for the disk."""
        app.set_running(app.sync_running, user, True)
        assert app.run_scheduled_backup() is False
        assert backup.existing() == []

    def test_resumes_once_the_sync_finishes(self, user, scheduled):
        _seed(user)
        app.set_running(app.sync_running, user, True)
        app.run_scheduled_backup()
        app.set_running(app.sync_running, user, False)
        assert app.run_scheduled_backup() is True

    def test_a_stale_claim_does_not_suppress_backups_forever(self, user, scheduled):
        """A sync that died without clearing its flag would otherwise disable
        every future backup — silently, and precisely the mechanism that
        recovers from whatever killed it."""
        _seed(user)
        app.sync_running[user] = time.time() - (app.BACKUP_SYNC_GRACE_SECONDS + 60)
        assert app.run_scheduled_backup() is True


class TestFailureIsVisibleAndSurvivable:
    def test_an_exception_does_not_escape(self, user, scheduled, monkeypatch):
        def explode(**kw):
            raise OSError("disk full")
        monkeypatch.setattr(backup, "take", explode)
        assert app.run_scheduled_backup() is False
        assert "disk full" in app._backup_status["last_error"]

    def test_a_refused_snapshot_is_recorded_as_an_error(self, user, scheduled, monkeypatch):
        monkeypatch.setattr(backup, "take", lambda **kw: 1)
        assert app.run_scheduled_backup() is False
        assert app._backup_status["last_error"] == "snapshot refused"

    def test_success_clears_a_previous_error(self, user, scheduled):
        _seed(user)
        app._backup_status["last_error"] = "disk full"
        assert app.run_scheduled_backup() is True
        assert app._backup_status["last_error"] is None


class TestStartup:
    def test_does_not_start_when_disabled(self, monkeypatch):
        monkeypatch.setattr(app, "BACKUP_INTERVAL_HOURS", 0.0)
        assert app.start_backup_scheduler() is None

    def test_does_not_start_in_demo_mode(self, monkeypatch):
        """The demo writes to a disposable database; snapshotting it is noise."""
        monkeypatch.setattr(app, "BACKUP_INTERVAL_HOURS", 24.0)
        monkeypatch.setattr(app, "DEMO_MODE", True)
        assert app.start_backup_scheduler() is None

    def test_starts_a_daemon_thread_when_enabled(self, monkeypatch):
        monkeypatch.setattr(app, "BACKUP_INTERVAL_HOURS", 24.0)
        monkeypatch.setattr(app, "DEMO_MODE", False)
        thread = app.start_backup_scheduler()
        assert thread is not None and thread.daemon, \
            "a non-daemon thread would hold the process open on shutdown"


class TestStatusForAdmin:
    def test_reports_nothing_taken_yet(self, user, scheduled):
        status = app.backup_status()
        assert status["age_hours"] is None
        assert status["count"] == 0
        assert status["overdue"] is True

    def test_reports_a_healthy_recent_snapshot(self, user, scheduled):
        _snapshot_file(age_hours=2)
        status = app.backup_status()
        assert status["age_hours"] == pytest.approx(2, abs=0.1)
        assert status["overdue"] is False
        assert status["last_error"] is None

    def test_reports_disabled(self, user, monkeypatch):
        monkeypatch.setattr(app, "BACKUP_INTERVAL_HOURS", 0.0)
        assert app.backup_status()["enabled"] is False
