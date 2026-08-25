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
import providers
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
        bomb = gzip.compress(b"\0" * (8 * 1024 * 1024))
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


class TestHealthz:
    def test_reports_liveness_and_commit_without_auth(self, user, monkeypatch):
        monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")
        app.app.config["TESTING"] = True
        resp = app.app.test_client().get("/healthz")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["commit"] == "abcdef1"

    def test_says_unknown_off_render(self, user, monkeypatch):
        monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
        app.app.config["TESTING"] = True
        assert app.app.test_client().get("/healthz").get_json()["commit"] == "unknown"

    def test_leaks_no_configuration(self, user, monkeypatch):
        """A public endpoint must not become a config dump."""
        monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1")
        app.app.config["TESTING"] = True
        body = app.app.test_client().get("/healthz").get_json()
        assert set(body) == {"ok", "commit", "demo"}


class TestAdminRetagIsReachable:
    """The retag control used to live inside the tagging-usage loop, so it
    rendered only for accounts that had recorded token spend. usage_log was
    added long after the tagging runs it describes, so it was empty — and the
    control was invisible on precisely the accounts that needed it. The route
    being correct is not the same as the button existing."""

    def _admin_client(self, user, monkeypatch):
        monkeypatch.setattr(app, "DEMO_MODE", False)
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        app.app.config["TESTING"] = True
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user
            sess["user_email"] = "admin@example.com"
            sess["access_token"] = "t"
        return client

    def test_retag_renders_with_no_usage_recorded(self, user, monkeypatch):
        db.upsert_user(user, "owner@example.com", "Owner", "2024-01-01T00:00:00Z")
        db.upsert_thread(user, make_thread("c1", ai_tags=[]))
        assert db.usage_by_user() == {}, "the whole point: no usage rows"

        html = self._admin_client(user, monkeypatch).get("/admin").get_data(as_text=True)
        assert 'data-retag="1"' in html, "retag control must not depend on usage data"
        assert "needs tagging" in html

    def test_untagged_count_is_shown(self, user, monkeypatch):
        db.upsert_user(user, "owner@example.com", "Owner", "2024-01-01T00:00:00Z")
        for i in range(3):
            db.upsert_thread(user, make_thread(f"c{i}", ai_tags=[]))
        html = self._admin_client(user, monkeypatch).get("/admin").get_data(as_text=True)
        assert "3 untagged" in html

    def test_nothing_untagged_shows_no_control(self, user, monkeypatch):
        db.upsert_user(user, "owner@example.com", "Owner", "2024-01-01T00:00:00Z")
        db.upsert_thread(user, make_thread("c1", ai_tags=["tagged"]))
        html = self._admin_client(user, monkeypatch).get("/admin").get_data(as_text=True)
        assert 'data-retag="1"' not in html
        assert "needs tagging" not in html

    def test_retag_offered_only_for_the_signed_in_account(self, user, monkeypatch):
        """The route acts on current_user_id(), so a button beside someone
        else's row promises something it cannot do — retagging refetches
        bodies from the provider using that account's own token."""
        db.upsert_user(user, "me@example.com", "Me", "2024-01-01T00:00:00Z")
        db.upsert_thread(user, make_thread("mine", ai_tags=[]))
        db.upsert_user("other-user", "them@example.com", "Them",
                       "2024-01-01T00:00:00Z")
        db.upsert_thread("other-user", make_thread("theirs", ai_tags=[]))

        html = self._admin_client(user, monkeypatch).get("/admin").get_data(as_text=True)
        assert html.count('data-retag="1"') == 1, "one button, for the viewer only"
        assert "sign in as this account to retag" in html

    def test_legacy_rows_point_at_the_claim_script(self, user, monkeypatch):
        """__legacy__ is a migration placeholder, not an account: nobody can
        sign in as it and it holds no token, so retag can never apply."""
        db.upsert_thread(db.LEGACY_USER_ID, make_thread("orphan", ai_tags=[]))
        html = self._admin_client(user, monkeypatch).get("/admin").get_data(as_text=True)
        assert "claim_legacy.py" in html
        assert 'data-retag="1"' not in html
        assert "invisible to every" in html

    def test_a_user_with_no_users_row_still_appears(self, user, monkeypatch):
        """Threads can outlive their users row via import or legacy claim.
        The row must surface so the untagged threads are visible at all; the
        retag control is a separate question, covered above."""
        db.upsert_thread("orphan-user", make_thread("c1", ai_tags=[]))
        html = self._admin_client(user, monkeypatch).get("/admin").get_data(as_text=True)
        assert "needs tagging" in html
        assert "orphan-user" in html, "falls back to the id when there is no email"


def _decode_session_cookie(resp):
    """Read a Flask session cookie without the signing key.

    Flask signs the session; it does not encrypt it. That is the whole point
    of this file's concern: anything put in there is readable by whoever
    holds the cookie.
    """
    import base64
    import zlib
    raw_cookie = next((v.split(";")[0].split("=", 1)[1]
                       for v in resp.headers.getlist("Set-Cookie")
                       if v.startswith("session=")), None)
    if not raw_cookie:
        return None
    parts = raw_cookie.split(".")
    payload = parts[1] if parts[0] == "" else parts[0]
    data = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    if parts[0] == "":
        data = zlib.decompress(data)
    return data.decode("utf8", "ignore")


class TestCredentialsAreNotInTheCookie:
    """A signed cookie is tamper-proof, not confidential. Keeping the OAuth
    token cache there put a long-lived refresh token — ongoing read access to
    a mailbox, independent of this app — into the browser of whoever held it.
    """

    SECRET = "LIVE-REFRESH-TOKEN-SHOULD-NEVER-LEAVE-THE-SERVER"

    class _FakeProvider:
        name = "microsoft"
        label = "Fake"

        def auth_url(self, state=None):
            return f"https://login.example/authorize?state={state}"

        def token_from_code(self, code):
            return "access-token-abc", (
                '{"RefreshToken": {"k": {"secret": '
                '"LIVE-REFRESH-TOKEN-SHOULD-NEVER-LEAVE-THE-SERVER"}}}')

        def refresh_token(self, token_cache):
            return "access-token-abc", token_cache

        def get_identity(self, access_token):
            return {"id": "u-real", "email": "owner@example.com",
                    "display_name": "Owner"}

    def _sign_in(self, monkeypatch):
        monkeypatch.setattr(app, "DEMO_MODE", False)
        monkeypatch.setattr(providers, "get", lambda name=None: self._FakeProvider())
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"owner@example.com"})
        app.app.config["TESTING"] = True
        client = app.app.test_client()
        client.get("/login")
        with client.session_transaction() as sess:
            state = sess["oauth_state"]
        resp = client.get(f"/callback?code=real-code&state={state}")
        return client, resp

    def test_the_refresh_token_never_reaches_the_cookie(self, user, monkeypatch):
        client, resp = self._sign_in(monkeypatch)
        assert resp.status_code == 302, "the sign-in must still succeed"

        decoded = _decode_session_cookie(resp)
        assert decoded is not None, "a session cookie should have been issued"
        assert self.SECRET not in decoded
        assert "token_cache" not in decoded
        assert "access_token" not in decoded

    def test_the_cookie_carries_identity_only(self, user, monkeypatch):
        client, resp = self._sign_in(monkeypatch)
        decoded = _decode_session_cookie(resp)
        assert "u-real" in decoded, "the session still identifies the user"

    def test_the_credentials_are_stored_server_side(self, user, monkeypatch):
        self._sign_in(monkeypatch)
        assert self.SECRET in (db.get_token_cache("u-real") or "")

    def test_signing_out_clears_the_stored_credentials(self, user, monkeypatch):
        client, _ = self._sign_in(monkeypatch)
        assert db.get_token_cache("u-real")
        client.get("/logout")
        assert db.get_token_cache("u-real") is None, \
            "a signed-out account must leave no refresh token at rest"

    def test_a_second_sign_in_does_not_wipe_the_cache(self, user, monkeypatch):
        """upsert_user runs on every sign-in and must not clobber it."""
        self._sign_in(monkeypatch)
        db.upsert_user("u-real", "owner@example.com", "Owner",
                       "2025-01-01T00:00:00Z")
        assert self.SECRET in (db.get_token_cache("u-real") or "")

    def test_a_session_without_stored_credentials_yields_no_token(self, user, monkeypatch):
        """The cookie alone must not be enough to act on a mailbox."""
        monkeypatch.setattr(providers, "get", lambda name=None: self._FakeProvider())
        app.app.config["TESTING"] = True
        with app.app.test_request_context("/"):
            from flask import session as flask_session
            flask_session["user_id"] = "u-never-signed-in"
            assert db.get_token_cache("u-never-signed-in") is None


class TestDetectiveBounds:
    """MAX_ROUNDS lives in browser JavaScript, so it binds only a cooperative
    client. /detective/ask relays whatever it is handed, which makes it an
    open-ended model endpoint paid for by the operator unless the server
    imposes its own limits."""

    def _client(self, user, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        app.app.config["TESTING"] = True
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user
            sess["user_email"] = "admin@example.com"
        return client

    def test_too_many_messages_is_refused(self, user, monkeypatch):
        client = self._client(user, monkeypatch)
        messages = [{"role": "user", "content": "hi"}
                    for _ in range(app.DETECTIVE_MAX_MESSAGES + 1)]
        resp = client.post("/detective/ask", json={"messages": messages})
        assert resp.status_code == 400
        assert "too long" in resp.get_json()["error"]

    def test_an_oversized_conversation_is_refused(self, user, monkeypatch):
        client = self._client(user, monkeypatch)
        huge = [{"role": "user", "content": "x" * (app.DETECTIVE_MAX_CHARS + 10)}]
        resp = client.post("/detective/ask", json={"messages": huge})
        assert resp.status_code == 400
        assert "too large" in resp.get_json()["error"]

    def test_a_normal_session_is_not_obstructed(self, user, monkeypatch):
        """The caps must sit well clear of ordinary use."""
        client = self._client(user, monkeypatch)
        sent = {}

        def fake_create(**kwargs):
            sent.update(kwargs)
            class R:
                content = [type("B", (), {"type": "text", "text": "ok"})()]
                usage = type("U", (), {"cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0,
                                       "input_tokens": 1, "output_tokens": 1})()
            return R()

        monkeypatch.setattr(app.anthropic_client.messages, "create", fake_create)
        messages = [{"role": "user", "content": "find the car loan"}] * 40
        resp = client.post("/detective/ask", json={"messages": messages})
        assert resp.status_code == 200
        assert sent, "a legitimate session must still reach the model"

    def test_a_non_list_body_is_refused(self, user, monkeypatch):
        client = self._client(user, monkeypatch)
        resp = client.post("/detective/ask", json={"messages": {"role": "user"}})
        assert resp.status_code == 400


class TestSpendLimit:
    """An approved account can cost the operator real money: a large mailbox
    is ~$6.65 to tag, and Detective bills per round. Approval is binary and
    says nothing about how much someone may spend."""

    def _client(self, user, monkeypatch, email="guest@example.com"):
        monkeypatch.setattr(app, "DEMO_MODE", False)
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        app.app.config["TESTING"] = True
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user
            sess["user_email"] = email
        return client

    def _spend(self, user, dollars):
        """Record enough input tokens to reach roughly `dollars`."""
        db.upsert_user(user, "guest@example.com", "Guest", "2024-01-01T00:00:00Z")
        tokens = int(dollars * 1e6 / app.PRICE_IN_PER_MTOK)
        db.record_usage(user, tokens, 0, 0, False, app.month_start())

    def test_unset_by_default_so_a_solo_instance_is_unaffected(self, user, monkeypatch):
        monkeypatch.setattr(app, "USER_SPEND_LIMIT_USD", None)
        self._spend(user, 500)
        assert app.USER_SPEND_LIMIT_USD is None
        with app.app.test_request_context("/"):
            assert app.over_spend_limit(user)[0] is False

    def test_detective_is_refused_over_the_limit(self, user, monkeypatch):
        monkeypatch.setattr(app, "USER_SPEND_LIMIT_USD", 5.0)
        self._spend(user, 6)
        client = self._client(user, monkeypatch)
        resp = client.post("/detective/ask",
                           json={"messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 402
        assert "monthly limit" in resp.get_json()["error"]

    def test_detective_runs_under_the_limit(self, user, monkeypatch):
        monkeypatch.setattr(app, "USER_SPEND_LIMIT_USD", 100.0)
        self._spend(user, 1)
        client = self._client(user, monkeypatch)

        def fake_create(**kwargs):
            class R:
                content = [type("B", (), {"type": "text", "text": "ok"})()]
                usage = type("U", (), {"cache_read_input_tokens": 0,
                                       "cache_creation_input_tokens": 0,
                                       "input_tokens": 1, "output_tokens": 1})()
            return R()

        monkeypatch.setattr(app.anthropic_client.messages, "create", fake_create)
        resp = client.post("/detective/ask",
                           json={"messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 200

    def test_sync_is_refused_over_the_limit(self, user, monkeypatch):
        monkeypatch.setattr(app, "USER_SPEND_LIMIT_USD", 5.0)
        self._spend(user, 6)
        client = self._client(user, monkeypatch)
        resp = client.get("/sync")
        assert resp.status_code == 302
        assert "spend_limited" in resp.headers["Location"]
        assert app.is_running(app.sync_running, user) is False, "no job may start"

    def test_the_admin_is_exempt(self, user, monkeypatch):
        """The limit protects the operator from guests, not from themselves."""
        monkeypatch.setattr(app, "USER_SPEND_LIMIT_USD", 5.0)
        self._spend(user, 500)
        self._client(user, monkeypatch, email="admin@example.com")
        with app.app.test_request_context("/"):
            from flask import session as flask_session
            flask_session["user_email"] = "admin@example.com"
            assert app.over_spend_limit(user)[0] is False

    def test_only_this_month_counts(self, user, monkeypatch):
        """Last month's spend must not permanently lock an account out."""
        monkeypatch.setattr(app, "USER_SPEND_LIMIT_USD", 5.0)
        db.upsert_user(user, "guest@example.com", "Guest", "2024-01-01T00:00:00Z")
        db.record_usage(user, int(50 * 1e6), 0, 0, False, "2020-01-01")
        with app.app.test_request_context("/"):
            assert app.over_spend_limit(user)[0] is False

    def test_spend_is_scoped_to_the_account(self, user, monkeypatch):
        monkeypatch.setattr(app, "USER_SPEND_LIMIT_USD", 5.0)
        self._spend("someone-else", 500)
        with app.app.test_request_context("/"):
            assert app.over_spend_limit(user)[0] is False

    def test_a_run_crossing_midnight_files_to_the_right_day(self, user, monkeypatch):
        """usage_recorder captured the day when it was built. A batch sync
        runs for hours, so a run crossing month end charged the old month and
        left the new one looking untouched — with the spend limit reading
        from the first, that is a hole in the limit itself."""
        from datetime import datetime as real_datetime, timezone as real_tz
        db.upsert_user(user, "guest@example.com", "Guest", "2024-01-01T00:00:00Z")
        clock = {"now": real_datetime(2024, 1, 31, 23, 59, tzinfo=real_tz.utc)}

        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                return clock["now"]

        monkeypatch.setattr(app, "datetime", FakeDatetime)
        record = app.usage_recorder(user)          # built before midnight
        clock["now"] = real_datetime(2024, 2, 1, 0, 30, tzinfo=real_tz.utc)
        record(1_000_000, 0, 0, False)             # recorded after it

        february = db.usage_since(user, "2024-02-01")
        assert february["input_tokens"] == 1_000_000, \
            "spend after midnight belongs to the new month"

    def test_a_malformed_limit_is_ignored_rather_than_crashing(self, monkeypatch):
        """A typo in the dashboard must not take the app down at import."""
        import importlib
        monkeypatch.setenv("USER_SPEND_LIMIT_USD", "twenty dollars")
        importlib.reload(app)
        assert app.USER_SPEND_LIMIT_USD is None
        monkeypatch.delenv("USER_SPEND_LIMIT_USD")
        importlib.reload(app)


class TestOAuthState:
    """Login CSRF. Without a state parameter, an attacker can send a victim
    to /callback carrying the attacker's authorisation code; the victim's
    browser ends up holding a session bound to the attacker's mailbox, and
    every thread it indexes — on the operator's API key — lands in someone
    else's catalog."""

    def _client(self, monkeypatch):
        monkeypatch.setattr(app, "DEMO_MODE", False)
        app.app.config["TESTING"] = True
        return app.app.test_client()

    class _FakeProvider:
        name = "microsoft"
        label = "Fake"
        seen_state = None

        def auth_url(self, state=None):
            type(self).seen_state = state
            return f"https://login.example/authorize?state={state}"

        def token_from_code(self, code):
            return "access-token", "cache"

        def get_identity(self, access_token):
            return {"id": "u-attacker", "email": "a@example.com",
                    "display_name": "A"}

    def test_login_issues_a_state_and_passes_it_to_the_provider(self, user, monkeypatch):
        fake = self._FakeProvider()
        monkeypatch.setattr(providers, "get", lambda name=None: fake)
        client = self._client(monkeypatch)

        resp = client.get("/login")
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            stored = sess["oauth_state"]
        assert stored, "no state was issued"
        assert fake.seen_state == stored, "the provider must receive the same value"
        assert stored in resp.headers["Location"]

    def test_state_is_unpredictable(self, user, monkeypatch):
        monkeypatch.setattr(providers, "get", lambda name=None: self._FakeProvider())
        seen = set()
        for _ in range(5):
            client = self._client(monkeypatch)
            client.get("/login")
            with client.session_transaction() as sess:
                seen.add(sess["oauth_state"])
        assert len(seen) == 5
        assert all(len(v) >= 32 for v in seen)

    def test_a_callback_with_no_state_is_refused(self, user, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get("/callback?code=attacker-code")
        assert resp.status_code == 400
        with client.session_transaction() as sess:
            assert "user_id" not in sess, "a forged callback must establish nothing"

    def test_a_callback_with_the_wrong_state_is_refused(self, user, monkeypatch):
        monkeypatch.setattr(providers, "get", lambda name=None: self._FakeProvider())
        client = self._client(monkeypatch)
        client.get("/login")            # a real state is now in the session
        resp = client.get("/callback?code=attacker-code&state=guessed")
        assert resp.status_code == 400
        with client.session_transaction() as sess:
            assert "user_id" not in sess

    def test_the_matching_state_is_accepted(self, user, monkeypatch):
        monkeypatch.setattr(providers, "get", lambda name=None: self._FakeProvider())
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"a@example.com"})
        client = self._client(monkeypatch)
        client.get("/login")
        with client.session_transaction() as sess:
            state = sess["oauth_state"]

        resp = client.get(f"/callback?code=real-code&state={state}")
        assert resp.status_code == 302, "the legitimate flow must still work"
        with client.session_transaction() as sess:
            assert sess["user_id"] == "u-attacker"

    def test_a_state_cannot_be_replayed(self, user, monkeypatch):
        """Popped rather than read, so a leaked value is spent once."""
        monkeypatch.setattr(providers, "get", lambda name=None: self._FakeProvider())
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"a@example.com"})
        client = self._client(monkeypatch)
        client.get("/login")
        with client.session_transaction() as sess:
            state = sess["oauth_state"]

        assert client.get(f"/callback?code=c&state={state}").status_code == 302
        assert client.get(f"/callback?code=c&state={state}").status_code == 400

    def test_the_pending_provider_is_cleared_on_rejection(self, user, monkeypatch):
        monkeypatch.setattr(providers, "get", lambda name=None: self._FakeProvider())
        client = self._client(monkeypatch)
        client.get("/login")
        client.get("/callback?code=attacker-code&state=wrong")
        with client.session_transaction() as sess:
            assert "pending_provider" not in sess


class TestDemoModeRoutes:
    """Hiding a control in the template is not the same as disabling it.
    Anything that reaches for a mailbox, or destroys state the demo only
    builds at startup, has to be refused at the route."""

    def _demo_client(self, monkeypatch):
        import demo_seed
        monkeypatch.setattr(app, "DEMO_MODE", True)
        monkeypatch.setattr(app, "ADMIN_EMAILS", {demo_seed.DEMO_EMAIL})
        app.app.config["TESTING"] = True
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = demo_seed.DEMO_USER_ID
            sess["user_email"] = demo_seed.DEMO_EMAIL
            sess["access_token"] = "demo"
        return client

    def test_wipe_is_refused(self, user, monkeypatch):
        """It emptied the catalog with no way back short of a restart."""
        client = self._demo_client(monkeypatch)
        assert client.post("/wipe").status_code == 403

    def test_resync_is_refused(self, user, monkeypatch):
        client = self._demo_client(monkeypatch)
        assert client.post("/resync").status_code == 409

    def test_retag_is_refused(self, user, monkeypatch):
        client = self._demo_client(monkeypatch)
        assert client.get("/retag-empty").status_code == 400

    def test_sync_is_a_no_op(self, user, monkeypatch):
        client = self._demo_client(monkeypatch)
        assert client.get("/sync").status_code == 302

    def test_the_login_bypass_establishes_the_demo_session(self, user, monkeypatch):
        """The bypass was never exercised: demo_seed was imported only under
        the flag, so app.login referenced an undefined name in any test that
        set DEMO_MODE after import."""
        import demo_seed
        client = self._demo_client(monkeypatch)
        with client.session_transaction() as sess:
            sess.clear()
        resp = client.get("/login")
        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess["user_id"] == demo_seed.DEMO_USER_ID
            assert "oauth_state" not in sess, "the bypass skips OAuth entirely"

    def test_search_still_works(self, user, monkeypatch):
        import demo_seed
        db.upsert_thread(demo_seed.DEMO_USER_ID,
                         make_thread("c1", ai_tags=["honda", "car"]))
        client = self._demo_client(monkeypatch)
        resp = client.get("/?q=honda")
        assert resp.status_code == 200
        assert b"honda" in resp.data


class TestDetectiveWithoutAKey:
    def test_a_missing_key_is_reported_rather_than_crashing(self, user, monkeypatch):
        """With no key the SDK raises TypeError, which is neither an
        APIStatusError nor an APIConnectionError — so it escaped the handlers
        and handed the browser a traceback in place of JSON."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        app.app.config["TESTING"] = True
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user
            sess["user_email"] = "admin@example.com"
            sess["access_token"] = "t"

        resp = client.post("/detective/ask",
                           json={"messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 503
        assert "ANTHROPIC_API_KEY" in resp.get_json()["error"]

    def test_an_unexpected_sdk_error_still_returns_json(self, user, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})

        def explode(**kwargs):
            raise TypeError("something the SDK did not document")

        monkeypatch.setattr(app.anthropic_client.messages, "create", explode)
        app.app.config["TESTING"] = True
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user
            sess["user_email"] = "admin@example.com"
            sess["access_token"] = "t"

        resp = client.post("/detective/ask",
                           json={"messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 500
        assert resp.is_json, "the polling loop parses JSON; HTML kills it"


class TestTagEditingLimits:
    """Bounds live in the route, not the storage layer — db.edit_user_tags
    writes whatever it is handed, so the route is what has to enforce them."""

    def _client(self, user, monkeypatch):
        monkeypatch.setattr(app, "ADMIN_EMAILS", {"admin@example.com"})
        app.app.config["TESTING"] = True
        client = app.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = user
            sess["user_email"] = "admin@example.com"
            sess["access_token"] = "t"
        return client

    def test_too_many_tags_in_one_request_are_refused(self, user, monkeypatch):
        db.upsert_thread(user, make_thread("c1"))
        client = self._client(user, monkeypatch)
        resp = client.post("/threads/c1/tags",
                           json={"add": ["t"] * (app.MAX_USER_TAGS_PER_REQUEST + 1)})
        assert resp.status_code == 400

    def test_over_long_tags_are_truncated_by_the_route(self, user, monkeypatch):
        db.upsert_thread(user, make_thread("c1"))
        client = self._client(user, monkeypatch)
        resp = client.post("/threads/c1/tags", json={"add": ["x" * 500]})
        assert resp.status_code == 200
        assert resp.get_json()["user_tags"] == ["x" * app.MAX_TAG_LENGTH]

    def test_non_list_input_is_refused(self, user, monkeypatch):
        db.upsert_thread(user, make_thread("c1"))
        client = self._client(user, monkeypatch)
        assert client.post("/threads/c1/tags", json={"add": "not a list"}).status_code == 400

    def test_an_unknown_thread_is_a_404(self, user, monkeypatch):
        client = self._client(user, monkeypatch)
        assert client.post("/threads/nope/tags", json={"add": ["x"]}).status_code == 404


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
