"""Tag normalisation and response parsing.

The model is the untrusted input here. It overshoots the tag count, repeats
itself, wraps JSON in markdown, returns the wrong number of arrays, and gets
cut off mid-response — all of which happened on the real corpus.
"""
import json

import tagger


class TestCleanTags:
    def test_lowercases_and_strips(self):
        assert tagger.clean_tags(["  Honda ", "CIVIC"]) == ["honda", "civic"]

    def test_drops_blanks(self):
        assert tagger.clean_tags(["honda", "", "   ", "civic"]) == ["honda", "civic"]

    def test_deduplicates_preserving_order(self):
        # One real thread came back with 460 tags of which 293 were distinct.
        tags = tagger.clean_tags(["car", "honda", "car", "loan", "honda"])
        assert tags == ["car", "honda", "loan"]

    def test_deduplicates_case_insensitively(self):
        assert tagger.clean_tags(["Honda", "honda", "HONDA"]) == ["honda"]

    def test_caps_at_max_tags(self):
        tags = tagger.clean_tags([f"tag{i}" for i in range(200)])
        assert len(tags) == tagger.MAX_TAGS

    def test_cap_counts_distinct_not_raw(self):
        """A flood of duplicates must not consume the budget."""
        raw = ["same"] * 500 + [f"real{i}" for i in range(10)]
        tags = tagger.clean_tags(raw)
        assert tags[0] == "same"
        assert "real9" in tags

    def test_survives_non_string_tags(self):
        """A bare year arrives as an int and used to crash the whole batch."""
        assert tagger.clean_tags([2016, "honda", 3.5]) == ["2016", "honda", "3.5"]

    def test_empty_input(self):
        assert tagger.clean_tags([]) == []


class TestSalvageTags:
    def test_recovers_arrays_from_truncated_json(self):
        truncated = '[["honda","car"],["dentist","teeth"],["bank","stat'
        out = tagger.salvage_tags(truncated, 3)
        assert out[0] == ["honda", "car"]
        assert out[1] == ["dentist", "teeth"]
        assert out[2] == []  # the incomplete one is padded, not guessed

    def test_pads_to_expected_count(self):
        out = tagger.salvage_tags('[["a"]]', 4)
        assert len(out) == 4
        assert out[1:] == [[], [], []]

    def test_truncates_to_expected_count(self):
        out = tagger.salvage_tags('[["a"],["b"],["c"]]', 2)
        assert len(out) == 2

    def test_arrays_stay_separate(self):
        """Investigated and disproved as the cause of the 460-tag thread:
        the parser does not concatenate adjacent arrays."""
        out = tagger.salvage_tags('[["a","b"],["c","d"]]', 2)
        assert out == [["a", "b"], ["c", "d"]]

    def test_applies_clean_tags_to_salvaged(self):
        out = tagger.salvage_tags('[["Honda","honda","  CAR "]]', 1)
        assert out[0] == ["honda", "car"]

    def test_garbage_yields_empty_lists(self):
        assert tagger.salvage_tags("not json at all", 2) == [[], []]


class TestParseTagResponse:
    def test_parses_clean_json(self):
        tags, truncated = tagger.parse_tag_response('[["a"],["b"]]', 2)
        assert tags == [["a"], ["b"]]
        assert truncated == [False, False]

    def test_strips_markdown_fences(self):
        text = '```json\n[["honda"],["dentist"]]\n```'
        tags, truncated = tagger.parse_tag_response(text, 2)
        assert tags == [["honda"], ["dentist"]]
        assert truncated == [False, False]

    def test_length_mismatch_falls_back_to_salvage(self):
        tags, truncated = tagger.parse_tag_response('[["a"]]', 3)
        assert len(tags) == 3
        assert truncated == [False, True, True]

    def test_truncated_flag_marks_only_empty_results(self):
        text = '[["honda","car"],["dent'
        tags, truncated = tagger.parse_tag_response(text, 2)
        assert truncated[0] is False   # recovered
        assert truncated[1] is True    # lost

    def test_none_response(self):
        tags, truncated = tagger.parse_tag_response(None, 2)
        assert tags == [[], []]
        assert all(truncated)

    def test_normalises_tags_on_the_happy_path(self):
        """Normalisation must not be salvage-only, or a well-formed response
        carrying 460 repeated tags is written to the database as-is."""
        payload = json.dumps([["Honda", "honda", "CAR"] + [f"t{i}" for i in range(100)]])
        tags, _ = tagger.parse_tag_response(payload, 1)
        assert len(tags[0]) == tagger.MAX_TAGS
        assert tags[0][:2] == ["honda", "car"]


class TestBuildPrompt:
    def test_numbers_threads_from_zero(self):
        prompt = tagger.build_prompt([
            {"subject": "A", "participants": "x@example.com", "date": "2024-01-01",
             "body_text": "hello", "attachments": []},
            {"subject": "B", "participants": "y@example.com", "date": "2024-01-02",
             "body_text": "world", "attachments": []},
        ])
        assert "Thread 0:" in prompt and "Thread 1:" in prompt

    def test_reports_body_truncation_to_the_model(self):
        prompt = tagger.build_prompt([{
            "subject": "A", "participants": "x@example.com", "date": "2024-01-01",
            "body_text": "hello", "body_scan_status": "truncated", "attachments": [],
        }])
        assert f"truncated at {tagger.BODY_CAP} chars" in prompt

    def test_distinguishes_attachment_scan_outcomes(self):
        prompt = tagger.build_prompt([{
            "subject": "A", "participants": "x@example.com", "date": "2024-01-01",
            "body_text": "", "attachments": [
                {"name": "a.pdf", "text": "extracted", "scan_status": "ok"},
                {"name": "b.xlsx", "text": "", "scan_status": "unsupported"},
                {"name": "c.pdf", "text": "", "scan_status": "failed"},
                {"name": "d.pdf", "text": "partial", "scan_status": "truncated"},
            ],
        }])
        assert "a.pdf: extracted" in prompt
        assert "unsupported type, filename only" in prompt
        assert "failed to extract" in prompt
        assert f"truncated at {tagger.ATTACHMENT_CAP} chars" in prompt

    def test_says_none_when_there_are_no_attachments(self):
        prompt = tagger.build_prompt([{
            "subject": "A", "participants": "x", "date": "2024-01-01",
            "body_text": "", "attachments": [],
        }])
        assert "none" in prompt

    def test_shared_by_both_tagging_paths(self):
        """Real-time and Batch API must not drift: both call build_prompt."""
        import inspect
        rt = inspect.getsource(tagger.generate_tags_batch)
        batch = inspect.getsource(tagger.submit_tag_batch)
        assert "build_prompt" in rt
        assert "build_prompt" in batch
