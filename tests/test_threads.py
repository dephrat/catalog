"""Thread reconstruction, change filtering, and attachment processing.

These run above the provider boundary, so everything here is fed normalised
message dicts rather than Graph-shaped ones — which is the whole point of
providers.py and is what lets a Gmail implementation reuse all of it.
"""
import app
import tagger
from conftest import make_message


class TestStripHtml:
    def test_removes_tags_and_collapses_whitespace(self):
        assert app.strip_html("<p>hello</p>\n\n  <b>world</b>") == "hello world"

    def test_empty(self):
        assert app.strip_html("") == ""


class TestBuildThreads:
    def test_groups_messages_by_thread(self):
        threads = app.build_threads([
            make_message("m1", "conv-A"),
            make_message("m2", "conv-B"),
            make_message("m3", "conv-A"),
        ])
        assert len(threads) == 2
        by_id = {t["thread_id"]: t for t in threads}
        assert len(by_id["conv-A"]["message_ids"]) == 2
        assert len(by_id["conv-B"]["message_ids"]) == 1

    def test_deduplicates_the_same_message_twice(self):
        """get_thread spans folders, so a message can arrive more than once."""
        threads = app.build_threads([make_message("m1"), make_message("m1")])
        assert len(threads[0]["message_ids"]) == 1

    def test_orders_by_date_and_takes_subject_from_the_first(self):
        threads = app.build_threads([
            make_message("m2", date="2024-05-01T00:00:00Z", subject="Re: original"),
            make_message("m1", date="2024-01-01T00:00:00Z", subject="original"),
        ])
        t = threads[0]
        assert t["subject"] == "original"
        assert t["date_first"] == "2024-01-01T00:00:00Z"
        assert t["date_last"] == "2024-05-01T00:00:00Z"

    def test_participants_are_sorted_and_deduplicated(self):
        threads = app.build_threads([
            make_message("m1", from_addr="b@example.com", to_addrs=["a@example.com"]),
            make_message("m2", from_addr="a@example.com", to_addrs=["b@example.com"]),
        ])
        assert threads[0]["participants"] == ["a@example.com", "b@example.com"]

    def test_participants_skip_blank_addresses(self):
        threads = app.build_threads([make_message("m1", from_addr="", to_addrs=[""])])
        assert threads[0]["participants"] == []
        assert threads[0]["participants_str"] == ""

    def test_has_attachments_is_true_if_any_message_has_one(self):
        threads = app.build_threads([
            make_message("m1", has_attachments=False),
            make_message("m2", has_attachments=True),
        ])
        assert threads[0]["has_attachments"] == 1

    def test_missing_dates_do_not_crash_the_sort(self):
        threads = app.build_threads([
            make_message("m1", date=""),
            make_message("m2", date="2024-01-01T00:00:00Z"),
        ])
        assert len(threads[0]["message_ids"]) == 2

    def test_empty_input(self):
        assert app.build_threads([]) == []


class TestFilterNewThreads:
    def _store(self, user, thread_id, date_last):
        import db
        from conftest import make_thread
        db.upsert_thread(user, make_thread(thread_id, date_last=date_last,
                                           ai_tags=["existing"]))

    def test_keeps_threads_not_seen_before(self, user):
        candidates = [{"thread_id": "new-1", "date_last": "2024-01-01T00:00:00Z"}]
        assert app.filter_new_threads(user, candidates) == candidates

    def test_keeps_threads_with_a_newer_message(self, user):
        self._store(user, "conv-1", "2024-01-01T00:00:00Z")
        candidates = [{"thread_id": "conv-1", "date_last": "2024-06-01T00:00:00Z"}]
        assert app.filter_new_threads(user, candidates) == candidates

    def test_drops_threads_that_are_unchanged(self, user):
        """Delta reports read/unread flips too; re-tagging those costs money."""
        self._store(user, "conv-1", "2024-01-01T00:00:00Z")
        candidates = [{"thread_id": "conv-1", "date_last": "2024-01-01T00:00:00Z"}]
        assert app.filter_new_threads(user, candidates) == []

    def test_forced_threads_pass_even_when_older(self, user):
        """Deleting a thread's newest message leaves date_last equal or older,
        so a purely date-based filter skipped the update and left a stored row
        pointing at a message that no longer exists."""
        self._store(user, "conv-1", "2024-06-01T00:00:00Z")
        candidates = [{"thread_id": "conv-1", "date_last": "2024-01-01T00:00:00Z"}]
        assert app.filter_new_threads(user, candidates,
                                      force_ids={"conv-1"}) == candidates

    def test_forcing_an_unknown_thread_is_harmless(self, user):
        candidates = [{"thread_id": "conv-1", "date_last": "2024-01-01T00:00:00Z"}]
        assert app.filter_new_threads(user, candidates, force_ids={"other"}) == candidates


class TestProcessAttachments:
    def _meta(self, name, aid="a1", ctype="application/pdf", size=1000):
        return [{"id": aid, "name": name, "content_type": ctype, "size": size}]

    def test_unsupported_type_keeps_the_filename_only(self):
        out = app.process_attachments("m1", self._meta("sheet.xlsx"), {})
        assert out[0]["scan_status"] == "unsupported"
        assert out[0]["text"] == ""
        assert out[0]["name"] == "sheet.xlsx"

    def test_missing_content_is_a_failure_not_a_silent_empty(self):
        out = app.process_attachments("m1", self._meta("doc.pdf"), {})
        assert out[0]["scan_status"] == "failed"

    def test_extracted_text_is_carried_through(self, monkeypatch):
        import extractor
        monkeypatch.setattr(extractor, "extract_text", lambda n, c, r: "hello world")
        out = app.process_attachments("m1", self._meta("doc.pdf"),
                                      {("m1", "a1"): b"%PDF-fake"})
        assert out[0]["scan_status"] == "ok"
        assert out[0]["text"] == "hello world"
        assert out[0]["actual_char_count"] == 11

    def test_truncation_is_flagged_and_the_real_length_recorded(self, monkeypatch):
        """The extractor used to cap at 3000 silently, so nothing downstream
        could tell the model its input was incomplete. The cap belongs here."""
        import extractor
        long_text = "x" * (tagger.ATTACHMENT_CAP + 500)
        monkeypatch.setattr(extractor, "extract_text", lambda n, c, r: long_text)
        out = app.process_attachments("m1", self._meta("doc.pdf"),
                                      {("m1", "a1"): b"%PDF-fake"})
        assert out[0]["scan_status"] == "truncated"
        assert len(out[0]["text"]) == tagger.ATTACHMENT_CAP
        assert out[0]["actual_char_count"] == tagger.ATTACHMENT_CAP + 500

    def test_extraction_returning_nothing_is_a_failure(self, monkeypatch):
        import extractor
        monkeypatch.setattr(extractor, "extract_text", lambda n, c, r: None)
        out = app.process_attachments("m1", self._meta("doc.pdf"),
                                      {("m1", "a1"): b"%PDF-fake"})
        assert out[0]["scan_status"] == "failed"

    def test_docx_is_supported_too(self, monkeypatch):
        import extractor
        monkeypatch.setattr(extractor, "extract_text", lambda n, c, r: "text")
        out = app.process_attachments("m1", self._meta("letter.docx"),
                                      {("m1", "a1"): b"PK-fake"})
        assert out[0]["scan_status"] == "ok"

    def test_extension_matching_is_case_insensitive(self, monkeypatch):
        import extractor
        monkeypatch.setattr(extractor, "extract_text", lambda n, c, r: "text")
        out = app.process_attachments("m1", self._meta("LETTER.PDF"),
                                      {("m1", "a1"): b"%PDF"})
        assert out[0]["scan_status"] == "ok"


class TestCleanAttachments:
    def test_drops_extracted_text_for_storage(self):
        """Document contents are deliberately not persisted."""
        thread = {"attachments": [{"name": "a.pdf", "content_type": "application/pdf",
                                   "size": 100, "text": "SENSITIVE CONTENTS",
                                   "actual_char_count": 18, "scan_status": "ok"}]}
        stored = app.clean_attachments(thread)
        assert "text" not in stored[0]
        assert stored[0]["actual_char_count"] == 18

    def test_handles_a_thread_with_no_attachments(self):
        assert app.clean_attachments({}) == []
