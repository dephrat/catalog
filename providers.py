"""Mail provider abstraction.

Everything above this layer — thread building, tagging, storage, search —
is provider-agnostic. Each provider is responsible for authentication and
for translating its own API into the normalised shapes below.

Normalised message:
    {
        "id":              str,   provider message id
        "thread_id":       str,   conversation/thread this belongs to
        "subject":         str,
        "from_addr":       str,   single address, "" if unknown
        "to_addrs":        [str],
        "date":            str,   ISO 8601, sortable
        "has_attachments": bool,
        "body":            str,   raw HTML or text; caller strips markup
        "web_link":        str,   deep link back into the provider's UI
        "container_id":    str,   folder/label id, used for exclusions
    }

Normalised attachment metadata:
    {"id": str, "name": str, "content_type": str, "size": int}

Change detection is expressed as *sources* with *cursors* rather than
folders with delta tokens, because the two providers differ here:
Microsoft Graph exposes a delta feed per mail folder, while Gmail exposes
a single mailbox-wide history feed. A provider that needs only one cursor
returns a single source.
"""

import auth as ms_auth
import graph as ms_graph


class MailProvider:
    """Interface every provider implements. See module docstring for shapes."""

    name = "unset"
    label = "unset"

    # ── Authentication ────────────────────────────────────────────────────
    def auth_url(self):
        raise NotImplementedError

    def token_from_code(self, code):
        """-> (access_token, serialised_token_cache)"""
        raise NotImplementedError

    def refresh_token(self, token_cache):
        """-> (access_token or None, serialised_token_cache or None)"""
        raise NotImplementedError

    def get_identity(self, access_token):
        """-> {"id", "email", "display_name"}"""
        raise NotImplementedError

    # ── Change detection ──────────────────────────────────────────────────
    def excluded_container_ids(self, access_token):
        """Container ids whose messages should never be indexed."""
        raise NotImplementedError

    def list_change_sources(self, access_token, exclude_ids=frozenset()):
        """-> [{"id", "name"}]  — each carries its own cursor."""
        raise NotImplementedError

    def changes_for_source(self, access_token, source_id, cursor):
        """-> (thread_ids, removed_message_ids, new_cursor, did_full_resync)"""
        raise NotImplementedError

    # ── Content ───────────────────────────────────────────────────────────
    def get_thread(self, access_token, thread_id):
        """-> [normalised message]; empty if the thread no longer exists."""
        raise NotImplementedError

    def get_attachment_metadata(self, access_token, message_ids):
        """-> {message_id: [normalised attachment metadata]}"""
        raise NotImplementedError

    def get_attachment_content(self, access_token, pairs):
        """pairs: [(message_id, attachment_id)] -> {pair: raw bytes}"""
        raise NotImplementedError


class MicrosoftProvider(MailProvider):
    """Personal Microsoft accounts via Graph."""

    name = "microsoft"
    label = "Microsoft / Outlook"

    def auth_url(self):
        return ms_auth.get_auth_url()

    def token_from_code(self, code):
        token, cache = ms_auth.get_token_from_code(code)
        return token["access_token"], cache

    def refresh_token(self, token_cache):
        return ms_auth.get_valid_token(token_cache)

    def get_identity(self, access_token):
        me = ms_graph.get_me(access_token)
        return {
            "id": me.get("id"),
            "email": me.get("mail") or me.get("userPrincipalName") or "",
            "display_name": me.get("displayName") or "",
        }

    def excluded_container_ids(self, access_token):
        return ms_graph.get_excluded_folder_ids(access_token)

    def list_change_sources(self, access_token, exclude_ids=frozenset()):
        # One source per mail folder: Graph's delta feed is folder-scoped.
        return ms_graph.list_mail_folders(access_token, exclude_ids=exclude_ids)

    def changes_for_source(self, access_token, source_id, cursor):
        changed, removed, new_cursor, full = ms_graph.delta_messages(
            access_token, source_id, cursor
        )
        thread_ids = [c["conversationId"] for c in changed if c.get("conversationId")]
        return thread_ids, removed, new_cursor, full

    def get_thread(self, access_token, thread_id):
        data = ms_graph.get_thread_messages(access_token, thread_id)
        return [self._normalise(m) for m in data.get("value", []) if m.get("id")]

    @staticmethod
    def _normalise(m):
        return {
            "id": m["id"],
            "thread_id": m.get("conversationId") or m["id"],
            "subject": m.get("subject") or "",
            "from_addr": (m.get("from") or {}).get("emailAddress", {}).get("address") or "",
            "to_addrs": [
                r.get("emailAddress", {}).get("address")
                for r in (m.get("toRecipients") or [])
                if r.get("emailAddress", {}).get("address")
            ],
            "date": m.get("receivedDateTime") or "",
            "has_attachments": bool(m.get("hasAttachments")),
            "body": (m.get("body") or {}).get("content") or m.get("bodyPreview") or "",
            "web_link": m.get("webLink") or "",
            "container_id": m.get("parentFolderId") or "",
        }

    def get_attachment_metadata(self, access_token, message_ids):
        raw = ms_graph.batch_get_attachment_metadata(access_token, message_ids)
        return {
            mid: [
                {
                    "id": a.get("id"),
                    "name": a.get("name") or "",
                    "content_type": (a.get("contentType") or "").lower(),
                    "size": a.get("size", 0),
                }
                for a in items
                if a.get("id")
            ]
            for mid, items in raw.items()
        }

    def get_attachment_content(self, access_token, pairs):
        import base64

        raw = ms_graph.batch_get_attachment_content(access_token, pairs)
        out = {}
        for pair, payload in raw.items():
            if not payload:
                continue
            content = payload.get("contentBytes")
            if not content:
                continue
            try:
                out[pair] = base64.b64decode(content)
            except Exception:
                continue
        return out


PROVIDERS = {p.name: p for p in (MicrosoftProvider(),)}
DEFAULT_PROVIDER = "microsoft"


def get(name):
    return PROVIDERS.get(name or DEFAULT_PROVIDER, PROVIDERS[DEFAULT_PROVIDER])
