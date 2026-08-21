"""Shared fixtures.

Two things have to happen before app.py is imported anywhere: DB_PATH must
point somewhere disposable, because importing the module runs init_db() and
recover_after_restart() as a side effect, and SECRET_KEY must exist or the
module refuses to load at all. Both are set here, at collection time, before
any test module gets a chance to import.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Import-time environment. _IMPORT_DB is only ever touched by app.py's own
# module-level init; every test that needs storage gets its own file.
_IMPORT_DB = os.path.join(tempfile.mkdtemp(prefix="catalog-import-"), "import.db")
os.environ["DB_PATH"] = _IMPORT_DB
os.environ.setdefault("SECRET_KEY", "test-key-not-a-real-secret")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("ANTHROPIC_API_KEY", "")

import pytest  # noqa: E402

import db  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A schema-initialised database of this test's own.

    get_db() reads the module global at call time rather than capturing it at
    import, so redirecting db.DB_PATH is enough to isolate a test.
    """
    path = tmp_path / "catalog.db"
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    return str(path)


@pytest.fixture
def user(fresh_db):
    return "user-under-test"


def make_message(mid, thread_id="conv-1", date="2024-03-01T10:00:00Z", **over):
    """A normalised message, the shape providers.MailProvider promises."""
    msg = {
        "id": mid,
        "thread_id": thread_id,
        "subject": "Bank statement",
        "from_addr": "sender@example.com",
        "to_addrs": ["owner@example.com"],
        "date": date,
        "has_attachments": False,
        "body": "<p>hello</p>",
        "web_link": f"https://example.test/{mid}",
        "container_id": "folder-A",
    }
    msg.update(over)
    return msg


def make_thread(thread_id="conv-1", **over):
    """A thread dict in the shape db.upsert_thread expects."""
    thread = {
        "thread_id": thread_id,
        "message_ids": [{"id": "m1", "web_link": "", "date": "2024-03-01",
                         "has_attachments": False}],
        "subject": "Bank statement",
        "participants": ["owner@example.com", "sender@example.com"],
        "date_first": "2024-03-01T10:00:00Z",
        "date_last": "2024-03-01T10:00:00Z",
        "has_attachments": 0,
        "attachments": [],
        "web_link": "",
        "ai_tags": [],
        "user_tags": [],
        "manually_reviewed": 0,
        "last_synced": "2024-03-01T10:00:00Z",
        "body_char_count": 12,
        "body_scan_status": "ok",
        "tags_truncated": 0,
    }
    thread.update(over)
    return thread
