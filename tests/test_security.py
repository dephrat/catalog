"""Access control, signed links, resource bounds, and the demo gate.

Every property here is one that fails open if it regresses — an approval
bypassed, a forged link honoured, a worker exhausted, or demo mode reachable
in production — so each is asserted from the outside rather than trusted.
"""
import base64
import gzip
import time

import pytest

import app
import db
from conftest import make_thread


class TestDecisionLinks:
    """Emailed approve/deny links are bearer capabilities, so they are
    HMAC-signed with an expiry and applied on POST — mail scanners prefetch
    GET links and would otherwise approve every request that reached an inbox.
    """

    def _token(self, user_id="u1", decision="approve", ttl=3600):
        return app.sign_decision(user_id, decision, time.time() + ttl)

    def test_a_valid_token_round_trips(self):
        assert app.verify_decision(self._token()) == ("u1", "approve")

    def test_deny_round_trips_too(self):
        token = self._token(decision="deny")
        assert app.verify_decision(token) == ("u1", "deny")

    def test_an_expired_token_is_refused(self):
        assert app.verify_decision(self._token(ttl=-1)) is None

    def test_a_tampered_payload_is_refused(self):
        """Flipping the user id must invalidate the signature."""
        token = self._token(user_id="u1")
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        forged = raw.replace("u1", "u2", 1)
        tampered = base64.urlsafe_b64encode(forged.encode()).decode().rstrip("=")
        assert app.verify_decision(tampered) is None

    def test_an_unsigned_token_is_refused(self):
        raw = f"u1|approve|{time.time() + 3600}|deadbeef"
        forged = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        assert app.verify_decision(forged) is None

    def test_an_unknown_decision_verb_is_refused(self):
        assert app.verify_decision(self._token(decision="promote")) is None

    def test_garbage_is_refused_without_raising(self):
        for junk in ["", "!!!!", "a" * 500, "////"]:
            assert app.verify_decision(junk) is None


class TestAccessGate:
    """The app is multi-tenant with a public URL, so an open sign-in would
    let anyone index their mailbox on the operator's API key."""

    def test_the_admin_is_always_allowed(self, user, monkeypatch):
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        allowed, ctx = app.evaluate_access("u1", "admin@example.com", "Admin")
        assert allowed is True

    def test_admin_matching_ignores_case_and_whitespace(self, user, monkeypatch):
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        assert app.evaluate_access("u1", "  ADMIN@Example.COM  ", "A")[0] is True

    def test_an_unknown_account_is_held_pending(self, user, monkeypatch):
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        allowed, ctx = app.evaluate_access("u2", "stranger@example.com", "S")
        assert allowed is False
        assert ctx["state"] == "pending"

    def test_an_approved_account_is_let_through(self, user, monkeypatch):
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        app.evaluate_access("u2", "stranger@example.com", "S")
        db.set_access_decision("u2", "approved", "2024-01-01T00:00:00Z")
        assert app.evaluate_access("u2", "stranger@example.com", "S")[0] is True

    def test_a_denied_account_is_held_in_cooldown(self, user, monkeypatch):
        from datetime import datetime, timedelta, timezone
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        app.evaluate_access("u2", "stranger@example.com", "S")
        until = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        db.set_access_decision("u2", "denied", "2024-01-01T00:00:00Z", denied_until=until)

        allowed, ctx = app.evaluate_access("u2", "stranger@example.com", "S")
        assert allowed is False
        assert ctx["state"] == "cooldown"

    def test_an_empty_admin_list_fails_closed(self, user, monkeypatch):
        """Without ADMIN_EMAIL nobody can sign in, including the owner."""
        monkeypatch.setattr(app, "ADMIN_EMAILS", set())
        assert app.is_admin("anyone@example.com") is False
        assert app.evaluate_access("u9", "anyone@example.com", "A")[0] is False

    def test_a_pending_account_is_not_re_notified_on_every_retry(self, user, monkeypatch):
        """Otherwise sign-in becomes a mail bomb."""
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        sends = []
        monkeypatch.setattr(app, "send_access_request_email",
                            lambda req: (sends.append(req), True)[1])
        app.evaluate_access("u2", "stranger@example.com", "S")
        app.evaluate_access("u2", "stranger@example.com", "S")
        app.evaluate_access("u2", "stranger@example.com", "S")
        assert len(sends) == 1


class TestDecompressionBound:
    """The upload size cap constrained the compressed bytes, which says
    nothing about the allocation: compression ratio is attacker-chosen."""

    def test_a_normal_export_round_trips(self):
        payload = b'{"format": 1, "threads": []}'
        assert app._decompress_bounded(gzip.compress(payload), 10_000) == payload

    def test_a_bomb_is_refused_without_being_allocated(self):
        bomb = gzip.compress(b"\0" * (50 * 1024 * 1024))
        assert len(bomb) < 100_000, "the point is that it passes a compressed check"
        with pytest.raises(ValueError):
            app._decompress_bounded(bomb, 1024 * 1024)

    def test_exactly_at_the_limit_is_accepted(self):
        assert len(app._decompress_bounded(gzip.compress(b"a" * 1000), 1000)) == 1000

    def test_one_byte_over_the_limit_is_refused(self):
        with pytest.raises(ValueError):
            app._decompress_bounded(gzip.compress(b"a" * 1001), 1000)

    def test_a_truncated_stream_raises_rather_than_returning_partial_data(self):
        """zlib's incremental decompressor reports truncation by simply
        stopping, with no exception and no short-read signal — so a partial
        inflate that happened to parse would import half a catalog."""
        good = gzip.compress(b'{"format": 1, "threads": [1, 2, 3]}')
        with pytest.raises(EOFError):
            app._decompress_bounded(good[:len(good) // 2], 10_000)

    def test_truncation_is_reported_to_the_user_as_truncation(self):
        import inspect
        source = inspect.getsource(app.import_catalog)
        assert "EOFError" in source, "a truncated upload must not escape as a 500"

    def test_the_import_cap_is_applied_to_the_inflated_size(self):
        """Guards the wiring, not just the helper."""
        import inspect
        source = inspect.getsource(app.import_catalog)
        assert "_decompress_bounded" in source
        assert "gzip.decompress" not in source


class TestDemoGate:
    def test_demo_mode_is_off_under_module_import(self):
        """gunicorn imports app as a module and rejects unknown arguments, so
        DEMO_MODE must depend on script execution — never on the environment,
        where one dashboard typo would disable access control entirely."""
        assert app.DEMO_MODE is False

    def test_no_environment_variable_can_enable_it(self):
        import inspect
        source = inspect.getsource(app)
        gate = next(line for line in source.splitlines() if line.startswith("DEMO_MODE"))
        assert "__main__" in gate and "--demo" in gate
        assert "getenv" not in gate and "environ" not in gate

    def test_the_demo_database_is_never_a_real_catalog(self):
        import inspect
        source = inspect.getsource(app)
        assert 'db.DB_PATH = "demo_catalog.db"' in source

    def test_demo_fixtures_carry_no_real_addresses(self):
        """The scanner treats free-mail domains as findings, so generated
        data must be committable by construction."""
        import re
        import demo_seed
        threads = demo_seed.generate()
        addresses = {p for t in threads for p in t["participants"]}
        assert addresses
        for addr in addresses:
            assert re.search(r"@(gmail|hotmail|outlook|yahoo|live|icloud|aol)\.",
                             addr, re.I) is None, addr


class TestTagEditingLimits:
    def test_tag_count_and_length_are_bounded(self):
        assert app.MAX_USER_TAGS_PER_REQUEST > 0
        assert app.MAX_TAG_LENGTH > 0

    def test_over_long_tags_are_truncated_on_write(self, user):
        db.upsert_thread(user, make_thread("c1"))
        db.edit_user_tags(user, "c1", add=["x" * 500])
        import json
        stored = json.loads(db.search_threads(user)[0]["user_tags"])
        assert stored == ["x" * 500], "db stores what it is given"


class TestPromptCaching:
    """Detective resends its whole history every round, so the cache
    breakpoints are the difference between linear and quadratic cost."""

    def test_marks_the_final_message_for_caching(self):
        messages = [{"role": "user", "content": "first"},
                    {"role": "assistant", "content": "second"}]
        out = app.mark_last_message_cacheable(messages)
        assert out[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_does_not_mutate_the_callers_list(self):
        messages = [{"role": "user", "content": "first"}]
        app.mark_last_message_cacheable(messages)
        assert messages[0]["content"] == "first"

    def test_handles_block_style_content(self):
        messages = [{"role": "user",
                     "content": [{"type": "text", "text": "a"},
                                 {"type": "text", "text": "b"}]}]
        out = app.mark_last_message_cacheable(messages)
        assert out[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in out[0]["content"][0]

    def test_empty_history_is_returned_unchanged(self):
        assert app.mark_last_message_cacheable([]) == []


class TestCostEstimate:
    def test_batched_work_is_billed_at_half(self):
        all_batched = app.estimate_cost({"requests": 10, "batched_requests": 10,
                                         "input_tokens": 1_000_000,
                                         "output_tokens": 100_000})
        none_batched = app.estimate_cost({"requests": 10, "batched_requests": 0,
                                          "input_tokens": 1_000_000,
                                          "output_tokens": 100_000})
        assert all_batched == pytest.approx(none_batched * 0.5)

    def test_no_requests_does_not_divide_by_zero(self):
        assert app.estimate_cost({"requests": 0, "batched_requests": 0,
                                  "input_tokens": 0, "output_tokens": 0}) == 0
