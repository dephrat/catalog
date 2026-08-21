"""The sync pipeline end to end, against a fake provider.

Everything here drives run_sync through providers.MailProvider rather than
through Graph, which is the point of the abstraction: a Gmail implementation
that satisfies the same interface inherits this entire suite.

The fake deliberately behaves like a real delta feed — it stops reporting a
conversation once the cursor has moved past it — because that behaviour is
exactly what makes an early cursor advance unrecoverable.
"""
import pytest

import app
import db
import tagger

FOLDER = "folder-A"


class FakeProvider:
    """A one-folder mailbox with a delta feed that honours its own cursor."""

    label = "Fake"

    def __init__(self, conversations=("conv-1",), fail_at=None, cursor="CURSOR-v2"):
        self.conversations = list(conversations)
        self.fail_at = fail_at
        self.cursor = cursor
        self.calls = []

    # ── auth ──
    def refresh_token(self, token_cache):
        return "tok", token_cache

    # ── change detection ──
    def excluded_container_ids(self, token):
        return set()

    def list_change_sources(self, token, exclude_ids=frozenset()):
        return [{"id": FOLDER, "name": "Inbox"}]

    def changes_for_source(self, token, source_id, cursor):
        self.calls.append(("changes", cursor))
        if self.fail_at == "delta":
            raise RuntimeError("delta feed unavailable")
        if cursor == self.cursor:
            return ([], [], self.cursor, False)      # already seen everything
        return (list(self.conversations), [], self.cursor, cursor is None)

    # ── content ──
    def get_thread(self, token, thread_id):
        if self.fail_at == "rebuild":
            raise RuntimeError("thread rebuild failed")
        return [{
            "id": f"{thread_id}-m1", "thread_id": thread_id,
            "subject": f"Subject for {thread_id}", "from_addr": "s@example.com",
            "to_addrs": ["owner@example.com"], "date": "2024-03-01T10:00:00Z",
            "has_attachments": False, "body": "<p>body</p>",
            "web_link": "https://example.test/m1", "container_id": FOLDER,
        }]

    def get_attachment_metadata(self, token, message_ids):
        if self.fail_at == "attachments":
            raise RuntimeError("a deploy killed the worker here")
        return {}

    def get_attachment_content(self, token, pairs):
        return {}


@pytest.fixture(autouse=True)
def no_real_tagging(monkeypatch):
    """Tag locally so no test can reach the Anthropic API."""
    monkeypatch.setattr(tagger, "generate_tags_batch",
                        lambda threads, on_usage=None:
                        ([["tag"] for _ in threads], [False] * len(threads)))


class TestDeltaCursorSafety:
    """A cursor is a promise that everything before it has been handled.
    Advancing it before storage loses mail silently and permanently, because
    the next scan simply never mentions it again."""

    def test_clean_run_advances_the_cursor(self, user):
        db.set_delta_link(user, FOLDER, "CURSOR-v1")
        app.run_sync(user, FakeProvider(), "tok")
        assert db.get_delta_link(user, FOLDER) == "CURSOR-v2"
        assert db.count_threads(user) == 1

    def test_failure_before_storage_holds_the_cursor(self, user):
        db.set_delta_link(user, FOLDER, "CURSOR-v1")
        app.run_sync(user, FakeProvider(fail_at="attachments"), "tok")
        assert db.get_delta_link(user, FOLDER) == "CURSOR-v1"
        assert db.count_threads(user) == 0
        assert db.get_sync_progress(user)["status"] == "error"

    def test_the_held_cursor_means_the_next_sync_recovers_the_mail(self, user):
        """The whole point. Before this rule the thread was gone for good."""
        db.set_delta_link(user, FOLDER, "CURSOR-v1")
        app.run_sync(user, FakeProvider(fail_at="attachments"), "tok")
        assert db.count_threads(user) == 0

        app.run_sync(user, FakeProvider(), "tok")
        assert db.count_threads(user) == 1
        assert db.get_delta_link(user, FOLDER) == "CURSOR-v2"

    def test_a_rebuild_failure_also_holds_the_cursor(self, user):
        db.set_delta_link(user, FOLDER, "CURSOR-v1")
        app.run_sync(user, FakeProvider(fail_at="rebuild"), "tok")
        assert db.get_delta_link(user, FOLDER) == "CURSOR-v1"

    def test_nothing_changed_still_advances(self, user):
        """An empty delta means the database already matches the mailbox."""
        provider = FakeProvider(conversations=[])
        app.run_sync(user, provider, "tok")
        assert db.get_delta_link(user, FOLDER) == "CURSOR-v2"

    def test_a_failed_delta_leaves_that_folder_untouched(self, user):
        db.set_delta_link(user, FOLDER, "CURSOR-v1")
        app.run_sync(user, FakeProvider(fail_at="delta"), "tok")
        assert db.get_delta_link(user, FOLDER) == "CURSOR-v1"

    def test_cursor_is_not_written_during_the_scan_phase(self, user, monkeypatch):
        """Guards the mechanism, not just the outcome: if anything starts
        persisting cursors inside detect_changes again, this fails."""
        writes = []
        real = db.set_delta_link
        monkeypatch.setattr(db, "set_delta_link",
                            lambda u, f, l: (writes.append(l), real(u, f, l))[1])

        provider = FakeProvider()
        conv_ids, orphans, excluded, cursors = app.detect_changes(
            user, provider, lambda: "tok")
        assert cursors == {FOLDER: "CURSOR-v2"}
        assert writes == [], "detect_changes must not persist cursors itself"


class TestRunSyncBehaviour:
    def test_threads_are_stored_before_tagging(self, user, monkeypatch):
        """A large import must be browsable within minutes rather than
        showing nothing for the length of the run."""
        seen = {}

        def slow_tagger(threads, on_usage=None):
            seen["stored_when_tagging_began"] = db.count_threads(user)
            return ([["tag"] for _ in threads], [False] * len(threads))

        monkeypatch.setattr(tagger, "generate_tags_batch", slow_tagger)
        app.run_sync(user, FakeProvider(conversations=["c1", "c2"]), "tok")
        assert seen["stored_when_tagging_began"] == 2

    def test_a_stop_requested_mid_scan_halts_the_run(self, user):
        """/sync/stop sets the flag while a sync is in flight."""
        class StopsItself(FakeProvider):
            def changes_for_source(self, token, source_id, cursor):
                result = super().changes_for_source(token, source_id, cursor)
                db.set_sync_flag(user, "1")     # the user hits stop here
                return result

        db.set_delta_link(user, FOLDER, "CURSOR-v1")
        app.run_sync(user, StopsItself(), "tok")
        assert db.get_sync_progress(user)["status"] == "stopped"
        assert db.count_threads(user) == 0
        assert db.get_delta_link(user, FOLDER) == "CURSOR-v1", \
            "a stopped sync stored nothing, so its cursor must not move"

    def test_a_previous_stop_does_not_block_the_next_sync(self, user):
        """The flag means 'stop the run in flight', so run_sync clears it —
        otherwise one stop would wedge the catalog permanently."""
        db.set_sync_flag(user, "1")
        app.run_sync(user, FakeProvider(), "tok")
        assert db.get_sync_progress(user)["status"] == "done"
        assert db.count_threads(user) == 1

    def test_expired_auth_reports_itself_clearly(self, user):
        class Expired(FakeProvider):
            def refresh_token(self, token_cache):
                return None, None

        db.set_delta_link(user, FOLDER, "CURSOR-v1")
        app.run_sync(user, Expired(), "tok", token_cache="stale-cache")
        assert db.get_sync_progress(user)["status"] == "auth_expired"
        assert db.get_delta_link(user, FOLDER) == "CURSOR-v1"

    def test_a_second_sync_does_not_retag_unchanged_threads(self, user, monkeypatch):
        calls = []
        monkeypatch.setattr(tagger, "generate_tags_batch",
                            lambda threads, on_usage=None: (
                                calls.append(len(threads)),
                                ([["tag"] for _ in threads], [False] * len(threads)))[1])
        app.run_sync(user, FakeProvider(), "tok")
        assert calls == [1]

        # Same cursor, so the feed reports nothing; even forced through, the
        # date filter would drop it.
        app.run_sync(user, FakeProvider(), "tok")
        assert calls == [1], "unchanged threads must not be re-tagged"

    def test_tags_land_on_the_stored_thread(self, user):
        app.run_sync(user, FakeProvider(), "tok")
        import json
        rows = db.search_threads(user)
        assert json.loads(rows[0]["ai_tags"]) == ["tag"]


class TestStoreUntagged:
    def test_does_not_strip_extracted_text_from_the_live_threads(self, user):
        """store_untagged used to assign a cleaned copy back onto the live
        dicts, so every attachment silently contributed nothing to tags."""
        threads = [{
            **__import__("conftest").make_thread("conv-1"),
            "attachments": [{"name": "a.pdf", "content_type": "application/pdf",
                             "size": 100, "text": "EXTRACTED TEXT",
                             "actual_char_count": 14, "scan_status": "ok"}],
        }]
        app.store_untagged(user, threads)
        assert threads[0]["attachments"][0]["text"] == "EXTRACTED TEXT"

    def test_stored_copy_has_no_document_contents(self, user):
        import json
        threads = [{
            **__import__("conftest").make_thread("conv-1"),
            "attachments": [{"name": "a.pdf", "content_type": "application/pdf",
                             "size": 100, "text": "EXTRACTED TEXT",
                             "actual_char_count": 14, "scan_status": "ok"}],
        }]
        app.store_untagged(user, threads)
        stored = json.loads(db.search_threads(user)[0]["attachments"])
        assert "EXTRACTED TEXT" not in json.dumps(stored)
        assert stored[0]["name"] == "a.pdf"


class TestApplyBatchResults:
    def test_returns_the_ids_actually_tagged_not_a_count(self, user, monkeypatch):
        """The caller treated any non-zero count as 'everything landed', so
        with 1 of 6 groups succeeding the other 50 threads were marked done
        and left permanently untagged."""
        for tid in ["c1", "c2", "c3"]:
            db.upsert_thread(user, __import__("conftest").make_thread(tid))

        mapping = {"g0": ["c1", "c2"], "g1": ["c3"]}
        monkeypatch.setattr(tagger, "collect_tag_batch",
                            lambda bid, counts, on_usage=None: {
                                "g0": ([["a"], ["b"]], [False, False])})  # g1 failed

        tagged = app.apply_batch_results(user, "batch-1", mapping)
        assert tagged == {"c1", "c2"}
        assert "c3" not in tagged

    def test_clears_the_pending_record_afterwards(self, user, monkeypatch):
        db.add_pending_batch(user, "batch-1", {"g0": ["c1"]}, "2024-01-01")
        monkeypatch.setattr(tagger, "collect_tag_batch",
                            lambda bid, counts, on_usage=None: {"g0": ([["a"]], [False])})
        app.apply_batch_results(user, "batch-1", {"g0": ["c1"]})
        assert db.get_pending_batches(user) == []


class TestBatchRecordExpiry:
    def test_a_fresh_record_is_kept(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        assert app._batch_record_expired(now) is False

    def test_a_record_past_the_retention_window_is_dropped(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc)
               - timedelta(days=app.BATCH_RECORD_TTL_DAYS + 1)).isoformat()
        assert app._batch_record_expired(old) is True

    def test_a_missing_or_unparseable_timestamp_is_kept(self):
        """Never discard paid work because a timestamp was malformed."""
        assert app._batch_record_expired(None) is False
        assert app._batch_record_expired("not a date") is False

    def test_a_naive_timestamp_does_not_crash(self):
        assert app._batch_record_expired("2024-01-01T00:00:00") is True


class TestResumePendingBatches:
    def test_an_unreadable_batch_is_kept_for_next_time(self, user, monkeypatch):
        """A transient 401 or rate limit used to delete a batch that was
        probably finished and already billed."""
        from datetime import datetime, timezone
        db.add_pending_batch(user, "b1", {"g0": ["c1"]},
                             datetime.now(timezone.utc).isoformat())
        monkeypatch.setattr(tagger, "batch_status", lambda bid: "unknown")
        app.resume_pending_batches(user)
        assert len(db.get_pending_batches(user)) == 1

    def test_an_in_progress_batch_is_left_alone(self, user, monkeypatch):
        db.add_pending_batch(user, "b1", {"g0": ["c1"]}, "2024-01-01T00:00:00+00:00")
        monkeypatch.setattr(tagger, "batch_status", lambda bid: "in_progress")
        app.resume_pending_batches(user)
        assert len(db.get_pending_batches(user)) == 1

    def test_an_unreadable_but_ancient_batch_is_finally_dropped(self, user, monkeypatch):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc)
               - timedelta(days=app.BATCH_RECORD_TTL_DAYS + 1)).isoformat()
        db.add_pending_batch(user, "b1", {"g0": ["c1"]}, old)
        monkeypatch.setattr(tagger, "batch_status", lambda bid: "unknown")
        app.resume_pending_batches(user)
        assert db.get_pending_batches(user) == []

    def test_an_ended_batch_is_collected(self, user, monkeypatch):
        db.upsert_thread(user, __import__("conftest").make_thread("c1"))
        db.add_pending_batch(user, "b1", {"g0": ["c1"]}, "2024-01-01T00:00:00+00:00")
        monkeypatch.setattr(tagger, "batch_status", lambda bid: "ended")
        monkeypatch.setattr(tagger, "collect_tag_batch",
                            lambda bid, counts, on_usage=None: {"g0": ([["x"]], [False])})
        assert app.resume_pending_batches(user) == 1
        assert db.get_pending_batches(user) == []


class TestRunParallel:
    def test_surfaces_worker_exceptions_instead_of_swallowing_them(self):
        """executor.map defers exceptions into an iterator nobody read, so a
        sync in which workers had thrown reported 'done'."""
        def boom(i):
            if i == 2:
                raise ValueError("worker died")

        errors = app.run_parallel(boom, [1, 2, 3], max_workers=2)
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)

    def test_returns_empty_when_everything_succeeds(self):
        assert app.run_parallel(lambda i: i, [1, 2, 3], max_workers=2) == []


class TestRetagEmptyWiring:
    def test_the_route_passes_a_provider_not_a_token(self):
        """/retag-empty passed three args to a four-parameter function, so
        the access token bound to `provider` and every invocation raised
        AttributeError into a bare 'error' status."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(app.retag_empty))
        thread_call = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "Thread")
        passed = next(kw.value for kw in thread_call.keywords if kw.arg == "args")

        params = list(inspect.signature(app.run_retag_empty).parameters)
        assert len(passed.elts) == len(params)
        assert "current_provider" in ast.unparse(passed.elts[1])

    def test_run_retag_empty_uses_the_provider_interface(self, user, monkeypatch):
        db.upsert_thread(user, __import__("conftest").make_thread("c1", ai_tags=[]))
        provider = FakeProvider()
        app.run_retag_empty(user, provider, "tok")
        import json
        assert json.loads(db.search_threads(user)[0]["ai_tags"]) == ["tag"]
        assert db.get_sync_progress(user)["status"] == "done"
