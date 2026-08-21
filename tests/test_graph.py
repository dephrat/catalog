"""The Microsoft Graph transport.

Throttling is the theme. Graph enforces a per-mailbox concurrency limit and
signals it two different ways — as the HTTP status of the $batch call, and as
a per-item status *inside* an otherwise-successful 200 response. The second
form was silently discarding attachment text on the real mailbox.

Every test here stubs requests, so nothing reaches the network.
"""
import pytest

import graph


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status at {self.status_code}")


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Retries are exercised for their control flow, not their patience."""
    monkeypatch.setattr(graph.time, "sleep", lambda s: None)


def item(idx, status=200, body=None, headers=None):
    return {"id": str(idx), "status": status, "body": body or {}, "headers": headers or {}}


def content_body(text=b"hello"):
    import base64
    return {"contentBytes": base64.b64encode(text).decode()}


class TestBatchContentThrottling:
    def _run(self, monkeypatch, responses, pairs=None):
        pairs = pairs or [("m1", "a1"), ("m2", "a2")]
        calls = {"n": 0}

        def fake_post(url, headers=None, json=None):
            calls["n"] += 1
            r = responses[min(calls["n"] - 1, len(responses) - 1)]
            return r() if callable(r) else r

        monkeypatch.setattr(graph.requests, "post", fake_post)
        return graph.batch_get_attachment_content("tok", pairs), calls

    def test_a_sustained_429_terminates_instead_of_recursing_forever(self, monkeypatch):
        """The top-level 429 handler recursed with attempt + 1 but never
        compared attempt to anything, so throttling hung the sync for hours
        rather than failing it in seconds."""
        throttled = FakeResponse(429, headers={"Retry-After": "0"})
        out, calls = self._run(monkeypatch, [throttled])

        assert calls["n"] <= graph.BATCH_MAX_RETRIES + 1
        assert set(out) == {("m1", "a1"), ("m2", "a2")}
        assert all(v is None for v in out.values()), "every pair must be accounted for"

    def test_a_transient_429_still_recovers(self, monkeypatch):
        out, calls = self._run(monkeypatch, [
            FakeResponse(429, headers={"Retry-After": "0"}),
            FakeResponse(200, {"responses": [item(0, body=content_body()),
                                             item(1, body=content_body())]}),
        ])
        assert calls["n"] == 2
        # This layer returns Graph's raw payload; decoding is the provider's
        # job, which is what keeps base64-vs-base64url out of the extractor.
        assert out[("m1", "a1")]["contentBytes"]

    def test_per_item_throttling_is_retried_not_recorded_as_failure(self, monkeypatch):
        """429 ApplicationThrottled arrives inside a 200 response. Recording
        it as a failure silently lost the document text, which is the entire
        thing PDF extraction exists to provide."""
        out, calls = self._run(monkeypatch, [
            FakeResponse(200, {"responses": [item(0, body=content_body(b"first")),
                                             item(1, status=429)]}),
            FakeResponse(200, {"responses": [item(0, body=content_body(b"second"))]}),
        ])
        import base64
        assert base64.b64decode(out[("m1", "a1")]["contentBytes"]) == b"first"
        assert base64.b64decode(out[("m2", "a2")]["contentBytes"]) == b"second", \
            "the throttled item must be retried, not recorded as failed"

    def test_a_hard_failure_is_recorded_once_without_retrying(self, monkeypatch):
        out, calls = self._run(monkeypatch, [
            FakeResponse(200, {"responses": [item(0, body=content_body()),
                                             item(1, status=404, body="not found")]}),
        ])
        assert calls["n"] == 1, "404 is not retryable"
        assert out[("m2", "a2")] is None

    def test_permanent_per_item_throttling_gives_up_bounded(self, monkeypatch):
        out, calls = self._run(monkeypatch, [
            FakeResponse(200, {"responses": [item(0, status=429), item(1, status=429)]}),
        ])
        assert calls["n"] <= graph.BATCH_MAX_RETRIES + 1
        assert all(v is None for v in out.values())

    def test_503_and_504_are_treated_as_retryable_too(self, monkeypatch):
        out, _ = self._run(monkeypatch, [
            FakeResponse(200, {"responses": [item(0, status=503), item(1, status=504)]}),
            FakeResponse(200, {"responses": [item(0, body=content_body(b"a")),
                                             item(1, body=content_body(b"b"))]}),
        ])
        import base64
        assert base64.b64decode(out[("m1", "a1")]["contentBytes"]) == b"a"
        assert base64.b64decode(out[("m2", "a2")]["contentBytes"]) == b"b"

    def test_a_400_marks_the_whole_chunk_without_retrying(self, monkeypatch):
        out, calls = self._run(monkeypatch, [FakeResponse(400, text="bad request")])
        assert calls["n"] == 1
        assert all(v is None for v in out.values())

    def test_a_transport_exception_does_not_lose_pairs(self, monkeypatch):
        def explode(url, headers=None, json=None):
            raise ConnectionError("network went away")

        monkeypatch.setattr(graph.requests, "post", explode)
        out = graph.batch_get_attachment_content("tok", [("m1", "a1")])
        assert out == {("m1", "a1"): None}

    def test_chunks_larger_than_twenty_are_split(self, monkeypatch):
        sent = []

        def fake_post(url, headers=None, json=None):
            sent.append(len(json["requests"]))
            return FakeResponse(200, {"responses": [
                item(i, body=content_body()) for i in range(len(json["requests"]))]})

        monkeypatch.setattr(graph.requests, "post", fake_post)
        pairs = [(f"m{i}", f"a{i}") for i in range(45)]
        out = graph.batch_get_attachment_content("tok", pairs)
        assert sorted(sent) == [5, 20, 20]
        assert len(out) == 45

    def test_empty_input_makes_no_requests(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(graph.requests, "post",
                            lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
        assert graph.batch_get_attachment_content("tok", []) == {}
        assert calls["n"] == 0


class TestBatchMetadata:
    def test_maps_results_back_to_the_right_messages(self, monkeypatch):
        def fake_post(url, headers=None, json=None):
            return FakeResponse(200, {"responses": [
                item(1, body={"value": [{"id": "a2", "name": "second.pdf"}]}),
                item(0, body={"value": [{"id": "a1", "name": "first.pdf"}]}),
            ]})  # deliberately out of order

        monkeypatch.setattr(graph.requests, "post", fake_post)
        out = graph.batch_get_attachment_metadata("tok", ["m1", "m2"])
        assert out["m1"][0]["name"] == "first.pdf"
        assert out["m2"][0]["name"] == "second.pdf"

    def test_a_failed_item_yields_an_empty_list_not_a_missing_key(self, monkeypatch):
        monkeypatch.setattr(graph.requests, "post", lambda *a, **k: FakeResponse(
            200, {"responses": [item(0, status=500, body="boom")]}))
        assert graph.batch_get_attachment_metadata("tok", ["m1"]) == {"m1": []}

    def test_a_transport_exception_yields_empty_lists(self, monkeypatch):
        def explode(*a, **k):
            raise ConnectionError("down")

        monkeypatch.setattr(graph.requests, "post", explode)
        assert graph.batch_get_attachment_metadata("tok", ["m1", "m2"]) == \
            {"m1": [], "m2": []}


class TestDeltaMessages:
    def test_pages_to_completion(self, monkeypatch):
        pages = [
            {"value": [{"id": "m1", "conversationId": "c1"}],
             "@odata.nextLink": "https://next"},
            {"value": [{"id": "m2", "conversationId": "c2"}],
             "@odata.deltaLink": "CURSOR-final"},
        ]
        calls = {"n": 0}

        def fake_request(headers, url, retries=3):
            page = pages[calls["n"]]
            calls["n"] += 1
            return page

        monkeypatch.setattr(graph, "make_request", fake_request)
        changed, removed, cursor, full = graph.delta_messages("tok", "folder-A")
        assert [c["conversationId"] for c in changed] == ["c1", "c2"]
        assert cursor == "CURSOR-final"

    def test_removals_are_separated_from_changes(self, monkeypatch):
        monkeypatch.setattr(graph, "make_request", lambda h, u, retries=3: {
            "value": [{"id": "m1", "conversationId": "c1"},
                      {"id": "m2", "@removed": {"reason": "deleted"}}],
            "@odata.deltaLink": "CURSOR",
        })
        changed, removed, cursor, full = graph.delta_messages("tok", "folder-A")
        assert [c["id"] for c in changed] == ["m1"]
        assert removed == ["m2"]

    def test_a_first_run_reports_a_full_resync(self, monkeypatch):
        monkeypatch.setattr(graph, "make_request",
                            lambda h, u, retries=3: {"value": [], "@odata.deltaLink": "C"})
        _, _, _, full = graph.delta_messages("tok", "folder-A", delta_link=None)
        assert full is True

    def test_an_incremental_run_does_not(self, monkeypatch):
        monkeypatch.setattr(graph, "make_request",
                            lambda h, u, retries=3: {"value": [], "@odata.deltaLink": "C"})
        _, _, _, full = graph.delta_messages("tok", "folder-A", delta_link="OLD")
        assert full is False


class TestFolderEnumeration:
    def test_excluded_folders_and_their_children_are_skipped(self, monkeypatch):
        responses = {
            "root": {"value": [{"id": "inbox", "displayName": "Inbox"},
                               {"id": "junk", "displayName": "Junk Email"}]},
        }

        def fake_request(headers, url, retries=3):
            if "childFolders" in url:
                return {"value": []}
            return responses["root"]

        monkeypatch.setattr(graph, "make_request", fake_request)
        folders = graph.list_mail_folders("tok", exclude_ids={"junk"})
        assert [f["id"] for f in folders] == ["inbox"]

    def test_an_unresolvable_well_known_folder_does_not_abort(self, monkeypatch):
        def fake_request(headers, url, retries=3):
            if "junkemail" in url:
                raise RuntimeError("no such folder")
            return {"id": f"id-for-{url.rsplit('/', 1)[-1].split('?')[0]}"}

        monkeypatch.setattr(graph, "make_request", fake_request)
        ids = graph.get_excluded_folder_ids("tok")
        assert len(ids) == len(graph.EXCLUDED_WELL_KNOWN) - 1


class TestProviderBoundary:
    """providers.py is where Graph shapes stop and normalised ones begin.
    A Gmail implementation satisfies the same contract, so these assertions
    are the ones a second provider has to keep passing."""

    def test_attachment_content_arrives_decoded(self, monkeypatch):
        import base64
        import providers

        monkeypatch.setattr(graph, "batch_get_attachment_content",
                            lambda tok, pairs: {
                                ("m1", "a1"): {"contentBytes":
                                               base64.b64encode(b"%PDF-real").decode()}})
        out = providers.MicrosoftProvider().get_attachment_content("tok", [("m1", "a1")])
        assert out[("m1", "a1")] == b"%PDF-real", "extract_text expects bytes"

    def test_undecodable_content_is_dropped_rather_than_passed_on(self, monkeypatch):
        import providers
        monkeypatch.setattr(graph, "batch_get_attachment_content",
                            lambda tok, pairs: {("m1", "a1"): {"contentBytes": "!!!not b64"},
                                                ("m2", "a2"): None})
        out = providers.MicrosoftProvider().get_attachment_content(
            "tok", [("m1", "a1"), ("m2", "a2")])
        assert out == {}

    def test_messages_are_normalised_away_from_graph_shapes(self):
        import providers
        normalised = providers.MicrosoftProvider._normalise({
            "id": "m1",
            "conversationId": "c1",
            "subject": "Hello",
            "from": {"emailAddress": {"address": "a@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "b@example.com"}},
                             {"emailAddress": {}}],
            "receivedDateTime": "2024-03-01T10:00:00Z",
            "hasAttachments": True,
            "body": {"content": "<p>hi</p>"},
            "webLink": "https://outlook.example/m1",
            "parentFolderId": "folder-A",
        })
        assert normalised == {
            "id": "m1", "thread_id": "c1", "subject": "Hello",
            "from_addr": "a@example.com", "to_addrs": ["b@example.com"],
            "date": "2024-03-01T10:00:00Z", "has_attachments": True,
            "body": "<p>hi</p>", "web_link": "https://outlook.example/m1",
            "container_id": "folder-A",
        }

    def test_a_message_with_no_conversation_falls_back_to_its_own_id(self):
        import providers
        normalised = providers.MicrosoftProvider._normalise({"id": "m1"})
        assert normalised["thread_id"] == "m1"
        assert normalised["from_addr"] == ""
        assert normalised["to_addrs"] == []

    def test_body_falls_back_to_the_preview(self):
        import providers
        normalised = providers.MicrosoftProvider._normalise(
            {"id": "m1", "bodyPreview": "preview text"})
        assert normalised["body"] == "preview text"

    def test_the_registry_falls_back_to_the_default_provider(self):
        import providers
        assert providers.get(None).name == providers.DEFAULT_PROVIDER
        assert providers.get("nonexistent").name == providers.DEFAULT_PROVIDER
        assert providers.get("microsoft").name == "microsoft"

    def test_every_interface_method_is_implemented(self):
        """The base class raises NotImplementedError; a provider that forgot
        one would fail at sync time rather than at import."""
        import providers
        required = [n for n in dir(providers.MailProvider)
                    if not n.startswith("_") and
                    callable(getattr(providers.MailProvider, n))]
        for provider in providers.PROVIDERS.values():
            for name in required:
                assert getattr(type(provider), name) is not \
                    getattr(providers.MailProvider, name), f"{provider.name}.{name}"
