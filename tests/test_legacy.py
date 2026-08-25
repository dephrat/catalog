"""Adopting pre-migration threads into a real account.

Legacy rows are invisible rather than merely untagged: every query is scoped
by user_id, so nothing parked under the placeholder reaches any account's
search. Claiming them is therefore a data-recovery step, and it runs against
a real archive with no undo — so the failure modes matter more than usual.
"""
import db
from conftest import make_thread


class TestLegacySummary:
    def test_reports_nothing_when_there_is_nothing(self, user):
        assert db.legacy_summary()["count"] == 0

    def test_counts_and_bounds_the_parked_rows(self, user):
        db.upsert_thread(db.LEGACY_USER_ID, make_thread(
            "old", date_first="2014-01-01T00:00:00Z", date_last="2014-01-02T00:00:00Z"))
        db.upsert_thread(db.LEGACY_USER_ID, make_thread(
            "new", date_first="2020-06-01T00:00:00Z", date_last="2020-06-02T00:00:00Z"))
        summary = db.legacy_summary()
        assert summary["count"] == 2
        assert summary["oldest"].startswith("2014")
        assert summary["newest"].startswith("2020")

    def test_sample_is_for_identifying_the_owner(self, user):
        db.upsert_thread(db.LEGACY_USER_ID, make_thread(
            "t", subject="Dentist appointment",
            participants=["someone@example.com"]))
        sample = db.legacy_summary()["sample"]
        assert sample[0]["subject"] == "Dentist appointment"
        assert "someone@example.com" in sample[0]["participants"]

    def test_ignores_rows_belonging_to_real_accounts(self, user):
        db.upsert_thread(user, make_thread("mine"))
        assert db.legacy_summary()["count"] == 0


class TestCollisions:
    def test_none_when_the_account_holds_nothing(self, user):
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("a"))
        assert db.legacy_collisions(user) == set()

    def test_detects_thread_ids_the_account_already_holds(self, user):
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("shared"))
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("only-legacy"))
        db.upsert_thread(user, make_thread("shared"))
        assert db.legacy_collisions(user) == {"shared"}

    def test_another_accounts_copy_is_not_a_collision(self, user):
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("shared"))
        db.upsert_thread("someone-else", make_thread("shared"))
        assert db.legacy_collisions(user) == set()


class TestClaim:
    def test_moves_everything_when_nothing_collides(self, user):
        for tid in ["a", "b", "c"]:
            db.upsert_thread(db.LEGACY_USER_ID, make_thread(tid))
        moved, dupes = db.claim_legacy_threads(user)
        assert (moved, dupes) == (3, 0)
        assert db.count_threads(user) == 3
        assert db.legacy_summary()["count"] == 0

    def test_a_collision_no_longer_aborts_the_whole_claim(self, user):
        """The primary key is (user_id, thread_id), so a bare UPDATE raised
        IntegrityError on the first duplicate and moved nothing at all."""
        db.upsert_thread(user, make_thread("shared", ai_tags=["fresh"]))
        for tid in ["shared", "only-legacy-1", "only-legacy-2"]:
            db.upsert_thread(db.LEGACY_USER_ID, make_thread(tid))

        moved, dupes = db.claim_legacy_threads(user)
        assert moved == 2, "the non-colliding rows must still be adopted"
        assert dupes == 1

    def test_the_accounts_own_copy_is_never_overwritten(self, user):
        """Its version came from a later sync, so it is the fresher one."""
        db.upsert_thread(user, make_thread("shared", ai_tags=["fresh", "tags"]))
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("shared", ai_tags=["stale"]))
        db.claim_legacy_threads(user)
        import json
        row = [r for r in db.search_threads(user) if r["thread_id"] == "shared"][0]
        assert json.loads(row["ai_tags"]) == ["fresh", "tags"]

    def test_duplicates_stay_parked_by_default(self, user):
        db.upsert_thread(user, make_thread("shared"))
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("shared"))
        db.claim_legacy_threads(user)
        assert db.legacy_summary()["count"] == 1, "left alone unless asked"

    def test_drop_duplicates_removes_them(self, user):
        db.upsert_thread(user, make_thread("shared"))
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("shared"))
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("movable"))
        moved, dupes = db.claim_legacy_threads(user, drop_duplicates=True)
        assert (moved, dupes) == (1, 1)
        assert db.legacy_summary()["count"] == 0
        assert db.count_threads(user) == 2

    def test_claiming_nothing_is_harmless(self, user):
        assert db.claim_legacy_threads(user) == (0, 0)

    def test_other_accounts_are_untouched(self, user):
        db.upsert_thread("bystander", make_thread("theirs"))
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("orphan"))
        db.claim_legacy_threads(user)
        assert db.count_threads("bystander") == 1

    def test_claimed_threads_become_searchable(self, user):
        """The actual point: parked rows reach nobody's search."""
        db.upsert_thread(db.LEGACY_USER_ID,
                         make_thread("orphan", ai_tags=["dentist"]))
        assert db.search_threads(user, query="dentist") == []
        db.claim_legacy_threads(user)
        assert len(db.search_threads(user, query="dentist")) == 1
