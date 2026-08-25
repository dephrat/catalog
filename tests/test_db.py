"""Storage, search, and sync state.

Three production incidents live in this file: tags wiped by a re-sync that
stored before tagging, a frozen progress bar with no way to tell a dead sync
from a slow one, and a paid batch record deleted by a transient error.
"""
import json
import time

import db
from conftest import make_thread


class TestUpsertThread:
    def test_round_trips(self, user):
        db.upsert_thread(user, make_thread("conv-1", ai_tags=["honda", "car"]))
        rows = db.search_threads(user)
        assert len(rows) == 1
        assert json.loads(rows[0]["ai_tags"]) == ["honda", "car"]

    def test_never_replaces_real_tags_with_an_empty_list(self, user):
        """A re-sync stores the thread before tagging runs, so a tagging
        failure would otherwise wipe tags a previous sync had paid for."""
        db.upsert_thread(user, make_thread("conv-1", ai_tags=["honda", "car"]))
        db.upsert_thread(user, make_thread("conv-1", ai_tags=[]))
        rows = db.search_threads(user)
        assert json.loads(rows[0]["ai_tags"]) == ["honda", "car"]

    def test_does_replace_tags_with_better_tags(self, user):
        db.upsert_thread(user, make_thread("conv-1", ai_tags=["old"]))
        db.upsert_thread(user, make_thread("conv-1", ai_tags=["new", "tags"]))
        rows = db.search_threads(user)
        assert json.loads(rows[0]["ai_tags"]) == ["new", "tags"]

    def test_updates_metadata_on_conflict(self, user):
        db.upsert_thread(user, make_thread("conv-1", subject="original"))
        db.upsert_thread(user, make_thread("conv-1", date_last="2025-01-01T00:00:00Z",
                                           has_attachments=1))
        rows = db.search_threads(user)
        assert rows[0]["date_last"] == "2025-01-01T00:00:00Z"
        assert rows[0]["has_attachments"] == 1

    def test_catalogs_are_isolated_per_user(self, user):
        db.upsert_thread(user, make_thread("conv-1"))
        db.upsert_thread("other-user", make_thread("conv-2"))
        assert len(db.search_threads(user)) == 1
        assert len(db.search_threads("other-user")) == 1
        assert db.count_threads(user) == 1


class TestSearch:
    def _seed(self, user):
        db.upsert_thread(user, make_thread(
            "car", subject="Honda loan", ai_tags=["honda", "car", "loan", "2016"],
            date_first="2016-03-01T00:00:00Z", date_last="2016-03-01T00:00:00Z",
            has_attachments=1,
            message_ids=[{"id": "m1", "date": "2016-03-01", "has_attachments": True}],
            attachments=[{"name": "loan.pdf", "size": 500_000, "scan_status": "ok"}]))
        db.upsert_thread(user, make_thread(
            "dentist", subject="Cleaning", ai_tags=["dentist", "teeth", "health"],
            date_first="2020-06-01T00:00:00Z", date_last="2020-06-02T00:00:00Z",
            message_ids=[{"id": "m1", "date": "2020-06-01", "has_attachments": False},
                         {"id": "m2", "date": "2020-06-02", "has_attachments": False}]))

    def test_and_mode_requires_every_word(self, user):
        self._seed(user)
        assert len(db.search_threads(user, query="honda loan", search_mode="and")) == 1
        assert len(db.search_threads(user, query="honda dentist", search_mode="and")) == 0

    def test_or_mode_takes_any_word(self, user):
        self._seed(user)
        assert len(db.search_threads(user, query="honda dentist", search_mode="or")) == 2

    def test_searches_subject_as_well_as_tags(self, user):
        self._seed(user)
        assert len(db.search_threads(user, query="Cleaning")) == 1

    def test_has_attachments_filter(self, user):
        self._seed(user)
        assert len(db.search_threads(user, has_attachments=True)) == 1

    def test_has_multiple_filter_finds_threads_with_replies(self, user):
        self._seed(user)
        rows = db.search_threads(user, has_multiple=True)
        assert [r["thread_id"] for r in rows] == ["dentist"]

    def test_min_attachment_size_filter(self, user):
        self._seed(user)
        assert len(db.search_threads(user, min_attachment_size="100")) == 1
        assert len(db.search_threads(user, min_attachment_size="1000")) == 0

    def test_limit_caps_rows_while_total_stays_accurate(self, user):
        for i in range(10):
            db.upsert_thread(user, make_thread(f"t{i}", ai_tags=["common"]))
        rows, total = db.search_threads(user, query="common", limit=3, with_total=True)
        assert len(rows) == 3
        assert total == 10

    def test_unknown_sort_falls_back_instead_of_raising(self, user):
        self._seed(user)
        assert len(db.search_threads(user, sort_by="'; DROP TABLE threads--")) == 2

    def test_scoped_to_the_calling_user(self, user):
        self._seed(user)
        db.upsert_thread("other", make_thread("theirs", ai_tags=["honda"]))
        assert len(db.search_threads(user, query="honda")) == 1


class TestFullTextSearch:
    """LIKE '%term%' matched substrings. On the real corpus that was not a
    nuance: "car" returned 2,593 threads of which 41 carried the tag, and
    "man" returned 1,356 of which none did."""

    def _seed(self, user):
        db.upsert_thread(user, make_thread("car", ai_tags=["car", "loan"]))
        db.upsert_thread(user, make_thread("carpet", ai_tags=["carpet", "rug"]))
        db.upsert_thread(user, make_thread("scarf", ai_tags=["scarf"]))
        db.upsert_thread(user, make_thread("management",
                                           subject="Management update"))

    def test_a_word_does_not_match_words_containing_it(self, user):
        self._seed(user)
        found = {r["thread_id"] for r in db.search_threads(user, query="car")}
        assert found == {"car"}, "carpet and scarf are different words"

    def test_a_word_inside_a_subject_still_matches(self, user):
        self._seed(user)
        found = {r["thread_id"] for r in db.search_threads(user, query="management")}
        assert found == {"management"}

    def test_multi_word_tags_are_matched_by_either_word(self, user):
        db.upsert_thread(user, make_thread("t", ai_tags=["car loan", "meridian"]))
        assert len(db.search_threads(user, query="loan")) == 1
        assert len(db.search_threads(user, query="car")) == 1

    def test_and_mode_requires_every_word(self, user):
        self._seed(user)
        assert len(db.search_threads(user, query="car loan", search_mode="and")) == 1
        assert len(db.search_threads(user, query="car rug", search_mode="and")) == 0

    def test_or_mode_takes_any_word(self, user):
        self._seed(user)
        assert len(db.search_threads(user, query="car rug", search_mode="or")) == 2

    def test_query_syntax_in_user_input_is_treated_as_words(self, user):
        """Unquoted, a search for AND, OR, NOT or a hyphenated word would be
        read as FTS grammar and either error or mean something else."""
        db.upsert_thread(user, make_thread("t", ai_tags=["and", "or", "not"]))
        for term in ["and", "or", "not", "AND", "-car", '"quoted"', "NEAR"]:
            db.search_threads(user, query=term)   # must not raise

    def test_punctuation_only_input_is_harmless(self, user):
        self._seed(user)
        for junk in ["*", "()", '"', "^", ":"]:
            db.search_threads(user, query=junk)

    def test_the_index_follows_a_tag_update(self, user):
        """set_thread_tags writes ai_tags directly, so the index has to be
        maintained by triggers rather than inside upsert_thread."""
        db.upsert_thread(user, make_thread("t", ai_tags=["before"]))
        assert len(db.search_threads(user, query="before")) == 1
        db.set_thread_tags(user, "t", ["after"], False)
        assert len(db.search_threads(user, query="before")) == 0
        assert len(db.search_threads(user, query="after")) == 1

    def test_the_index_follows_a_delete(self, user):
        db.upsert_thread(user, make_thread("t", ai_tags=["gone"]))
        db.delete_thread(user, "t")
        assert db.search_threads(user, query="gone") == []

    def test_the_index_follows_a_wipe(self, user):
        db.upsert_thread(user, make_thread("t", ai_tags=["gone"]))
        db.wipe_db(user)
        assert db.search_threads(user, query="gone") == []

    def test_results_stay_scoped_to_the_user(self, user):
        db.upsert_thread(user, make_thread("mine", ai_tags=["shared"]))
        db.upsert_thread("other", make_thread("theirs", ai_tags=["shared"]))
        assert len(db.search_threads(user, query="shared")) == 1

    def test_filters_still_apply_alongside_a_query(self, user):
        db.upsert_thread(user, make_thread("a", ai_tags=["shared"],
                                           has_attachments=1))
        db.upsert_thread(user, make_thread("b", ai_tags=["shared"]))
        assert len(db.search_threads(user, query="shared",
                                     has_attachments=True)) == 1

    def test_total_is_accurate_alongside_a_limit(self, user):
        for i in range(10):
            db.upsert_thread(user, make_thread(f"t{i}", ai_tags=["common"]))
        rows, total = db.search_threads(user, query="common", limit=3,
                                        with_total=True)
        assert (len(rows), total) == (3, 10)

    def test_availability_is_read_from_the_database_not_a_global(self, user, tmp_path,
                                                                 monkeypatch):
        """A module flag set by init_db is wrong for any other database in
        the same process, and wrong in the silent direction: search would
        fall back to substring matching with nothing to indicate it."""
        conn = db.get_db()
        try:
            assert db.has_fts(conn) is True
        finally:
            conn.close()

        import sqlite3
        bare = tmp_path / "no-fts.db"
        c = sqlite3.connect(bare)
        c.execute("CREATE TABLE threads (user_id TEXT, thread_id TEXT)")
        c.commit()
        c.row_factory = sqlite3.Row
        try:
            assert db.has_fts(c) is False
        finally:
            c.close()


class TestSafeInt:
    def test_rejects_junk_and_negatives(self):
        assert db._safe_int("abc") is None
        assert db._safe_int("-5") is None
        assert db._safe_int("") is None
        assert db._safe_int(None) is None

    def test_accepts_positive_values(self):
        assert db._safe_int("100") == 100
        assert db._safe_int(100) == 100


class TestSyncProgress:
    def test_reports_stale_when_an_active_status_goes_quiet(self, user, monkeypatch):
        """Without a heartbeat a dead sync is indistinguishable from a live
        one, which left a progress bar that would never move again."""
        db.set_sync_progress(user, 5, 10, "tagging")
        assert db.get_sync_progress(user)["stale"] is False

        # Advance db's clock past the heartbeat window without sleeping.
        later = time.time() + db.STALE_AFTER_SECONDS + 60
        monkeypatch.setattr(db.time, "time", lambda: later)
        assert db.get_sync_progress(user)["stale"] is True

    def test_a_finished_sync_is_never_stale(self, user, monkeypatch):
        """Only the statuses that imply a live thread can go stale, or a
        catalog left idle overnight would report itself broken."""
        db.set_sync_progress(user, 10, 10, "done")
        later = time.time() + db.STALE_AFTER_SECONDS * 100
        monkeypatch.setattr(db.time, "time", lambda: later)
        progress = db.get_sync_progress(user)
        assert progress["status"] == "done"
        assert progress["stale"] is False

    def test_every_active_status_is_covered(self, user, monkeypatch):
        """A phase missing from ACTIVE_STATUSES can never be detected as
        dead, which is how the frozen bar survived its first fix."""
        clock = {"now": time.time()}
        monkeypatch.setattr(db.time, "time", lambda: clock["now"])
        for status in db.ACTIVE_STATUSES:
            db.set_sync_progress(user, 1, 2, status)      # heartbeat at now
            assert db.get_sync_progress(user)["stale"] is False, status
            clock["now"] += db.STALE_AFTER_SECONDS + 60   # the worker dies here
            assert db.get_sync_progress(user)["stale"] is True, status

    def test_idle_by_default(self, user):
        assert db.get_sync_progress(user)["status"] == "idle"

    def test_interrupted_syncs_are_marked_at_startup(self, user):
        db.set_sync_progress(user, 3, 10, "tagging (batch)")
        db.set_sync_progress("other", 1, 5, "done")
        assert db.mark_interrupted_syncs() == 1
        assert db.get_sync_progress(user)["status"] == "interrupted"
        assert db.get_sync_progress("other")["status"] == "done"


class TestPendingBatches:
    def test_round_trips_the_group_mapping(self, user):
        mapping = {"g0": ["conv-1", "conv-2"], "g1": ["conv-3"]}
        db.add_pending_batch(user, "batch_abc", mapping, "2024-01-01T00:00:00+00:00")
        pending = db.get_pending_batches(user)
        assert len(pending) == 1
        batch_id, got_mapping, submitted = pending[0]
        assert batch_id == "batch_abc"
        assert got_mapping == mapping
        assert submitted == "2024-01-01T00:00:00+00:00"

    def test_batch_ids_containing_colons_survive(self, user):
        db.add_pending_batch(user, "msgbatch:01:xyz", {"g0": ["c1"]}, "2024-01-01")
        assert db.get_pending_batches(user)[0][0] == "msgbatch:01:xyz"

    def test_clearing_removes_only_that_batch(self, user):
        db.add_pending_batch(user, "b1", {"g0": ["c1"]}, "2024-01-01")
        db.add_pending_batch(user, "b2", {"g0": ["c2"]}, "2024-01-01")
        db.clear_pending_batch(user, "b1")
        assert [b[0] for b in db.get_pending_batches(user)] == ["b2"]

    def test_users_with_pending_batches(self, user):
        db.add_pending_batch(user, "b1", {"g0": ["c1"]}, "2024-01-01")
        assert db.users_with_pending_batches() == [user]


class TestDeltaLinks:
    def test_round_trip_per_folder(self, user):
        db.set_delta_link(user, "folder-A", "CURSOR-A")
        db.set_delta_link(user, "folder-B", "CURSOR-B")
        assert db.get_delta_link(user, "folder-A") == "CURSOR-A"
        assert db.get_delta_link(user, "folder-B") == "CURSOR-B"

    def test_absent_folder_returns_none(self, user):
        assert db.get_delta_link(user, "never-seen") is None

    def test_clearing_is_the_drift_recovery_hatch(self, user):
        db.set_delta_link(user, "folder-A", "CURSOR-A")
        db.clear_delta_links(user)
        assert db.get_delta_link(user, "folder-A") is None

    def test_clearing_does_not_touch_other_state(self, user):
        db.set_delta_link(user, "folder-A", "CURSOR-A")
        db.add_pending_batch(user, "b1", {"g0": ["c1"]}, "2024-01-01")
        db.clear_delta_links(user)
        assert len(db.get_pending_batches(user)) == 1


class TestThreadLookup:
    def test_traces_removed_message_ids_back_to_threads(self, user):
        db.upsert_thread(user, make_thread("conv-1", message_ids=[
            {"id": "m1", "date": "2024-01-01", "has_attachments": False},
            {"id": "m2", "date": "2024-01-02", "has_attachments": False}]))
        assert db.find_thread_ids_by_message_ids(user, ["m2"]) == {"conv-1"}

    def test_empty_input_short_circuits(self, user):
        assert db.find_thread_ids_by_message_ids(user, []) == set()

    def test_chunks_beyond_the_sqlite_variable_limit(self, user):
        db.upsert_thread(user, make_thread("conv-1", message_ids=[
            {"id": "m999", "date": "2024-01-01", "has_attachments": False}]))
        ids = [f"absent-{i}" for i in range(1200)] + ["m999"]
        assert db.find_thread_ids_by_message_ids(user, ids) == {"conv-1"}


class TestUserTags:
    def test_added_tags_survive_retagging(self, user):
        db.upsert_thread(user, make_thread("conv-1", ai_tags=["ai"]))
        db.edit_user_tags(user, "conv-1", add=["mine"])
        db.set_thread_tags(user, "conv-1", ["fresh", "ai", "tags"], False)
        rows = db.search_threads(user)
        assert json.loads(rows[0]["user_tags"]) == ["mine"]
        assert json.loads(rows[0]["ai_tags"]) == ["fresh", "ai", "tags"]

    def test_removing_a_tag(self, user):
        db.upsert_thread(user, make_thread("conv-1"))
        db.edit_user_tags(user, "conv-1", add=["a", "b"])
        remaining = db.edit_user_tags(user, "conv-1", remove=["a"])
        assert remaining == ["b"]

    def test_unknown_thread_returns_none(self, user):
        assert db.edit_user_tags(user, "does-not-exist", add=["x"]) is None


class TestExportImport:
    def test_round_trips_a_catalog_with_its_tags(self, user):
        db.upsert_thread(user, make_thread("conv-1", ai_tags=["honda", "car"]))
        db.upsert_thread(user, make_thread("conv-2", ai_tags=["dentist"]))
        payload = db.export_threads(user)

        assert db.import_threads("fresh-user", payload) == 2
        rows = db.search_threads("fresh-user", query="honda")
        assert len(rows) == 1

    def test_rejects_an_unrecognised_format(self, user):
        import pytest
        with pytest.raises(ValueError, match="unrecognised"):
            db.import_threads(user, {"format": 999, "threads": []})

    def test_rejects_a_payload_with_no_thread_list(self, user):
        import pytest
        with pytest.raises(ValueError, match="no threads"):
            db.import_threads(user, {"format": db.EXPORT_FORMAT, "threads": "nope"})

    def test_replace_clears_the_existing_catalog_first(self, user):
        db.upsert_thread(user, make_thread("old-1"))
        payload = {"format": db.EXPORT_FORMAT,
                   "threads": [{"thread_id": "new-1", "ai_tags": '["x"]'}]}
        db.import_threads(user, payload, replace=True)
        assert {r["thread_id"] for r in db.search_threads(user)} == {"new-1"}

    def test_skips_malformed_rows_without_aborting(self, user):
        payload = {"format": db.EXPORT_FORMAT, "threads": [
            {"thread_id": "good"}, {"no_id": True}, "not a dict"]}
        assert db.import_threads(user, payload) == 1

    def test_export_does_not_carry_delta_tokens(self, user):
        """They belong to the mailbox connection that produced them."""
        db.set_delta_link(user, "folder-A", "CURSOR-A")
        db.upsert_thread(user, make_thread("conv-1"))
        payload = db.export_threads(user)
        assert "CURSOR-A" not in json.dumps(payload)


class TestUsageAccounting:
    def test_accumulates_per_user_and_day(self, user):
        db.upsert_user(user, "u@example.com", "U", "2024-01-01T00:00:00Z")
        db.record_usage(user, 100, 20, 0, False, "2024-01-01")
        db.record_usage(user, 50, 10, 0, False, "2024-01-01")
        stats = db.usage_by_user()[user]
        assert stats["input_tokens"] == 150
        assert stats["requests"] == 2

    def test_splits_batched_from_real_time(self, user):
        db.upsert_user(user, "u@example.com", "U", "2024-01-01T00:00:00Z")
        db.record_usage(user, 100, 20, 0, True, "2024-01-01")
        db.record_usage(user, 100, 20, 0, False, "2024-01-01")
        stats = db.usage_by_user()[user]
        assert stats["requests"] == 2
        assert stats["batched_requests"] == 1


class TestUntaggedThreads:
    def test_counts_and_lists_threads_with_no_tags(self, user):
        db.upsert_thread(user, make_thread("tagged", ai_tags=["a"]))
        db.upsert_thread(user, make_thread("untagged", ai_tags=[]))
        assert db.count_untagged(user) == 1
        assert [t["thread_id"] for t in db.get_untagged_threads(user)] == ["untagged"]


class TestWipe:
    def test_deletes_only_the_calling_users_catalog(self, user):
        db.upsert_thread(user, make_thread("mine"))
        db.upsert_thread("other", make_thread("theirs"))
        db.wipe_db(user)
        assert db.count_threads(user) == 0
        assert db.count_threads("other") == 1
