"""Google OAuth, hand-rolled against the token endpoint.

msal does this job for Microsoft; Google's equivalent SDK (google-auth +
google-auth-oauthlib) buys nothing here that three requests calls don't,
so the flow is written out. The serialised cache is a JSON object holding
the refresh token plus the current access token and its expiry — the same
contract auth.py exposes: opaque string in, opaque string out.
"""
import json
import os
import time
import urllib.parse

import requests
from dotenv import load_dotenv

load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Read-only mail access is all the app needs; identity comes from Gmail's
# own getProfile rather than an extra OpenID scope.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Shared with the Microsoft flow: both providers redirect to /callback, and
# the session's pending_provider records which one issued the code.
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")

# Refresh a minute early so a token never expires mid-request.
EXPIRY_MARGIN_SECONDS = 60


def get_auth_url(state=None):
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        # offline + consent: Google only issues a refresh token on a consent
        # screen, and only the first time unless consent is forced. Without
        # both, a re-login silently yields a cache that cannot refresh.
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


def get_token_from_code(code):
    """-> (token_response_dict, serialised_cache). Raises on failure."""
    r = requests.post(TOKEN_ENDPOINT, data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }, timeout=30)
    result = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in result:
        raise Exception(f"Auth failed: {result.get('error_description') or result.get('error') or r.status_code}")
    return result, _serialise(result, refresh_token=result.get("refresh_token"))


def get_valid_token(token_cache_data):
    """Get a valid access token, refreshing if necessary.

    Same contract as auth.get_valid_token: (token, new_cache), or
    (None, None) when the refresh token itself is dead and the user must
    sign in again.
    """
    try:
        cache = json.loads(token_cache_data) if token_cache_data else {}
    except (TypeError, ValueError):
        cache = {}

    refresh_token = cache.get("refresh_token")
    if not refresh_token:
        return None, None

    # Still fresh: hand back the cached token without a network call.
    if cache.get("access_token") and \
            cache.get("expires_at", 0) - EXPIRY_MARGIN_SECONDS > time.time():
        return cache["access_token"], token_cache_data

    r = requests.post(TOKEN_ENDPOINT, data={
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    result = r.json() if r.content else {}
    if r.status_code != 200 or "access_token" not in result:
        # invalid_grant means revoked or expired consent — unrecoverable
        # here, so signal re-login the same way the Microsoft path does.
        print(f"Google token refresh failed: {result.get('error', r.status_code)}")
        return None, None

    # A refresh response rarely carries a new refresh token; keep the old one.
    new_cache = _serialise(result, refresh_token=result.get("refresh_token") or refresh_token)
    return result["access_token"], new_cache


def _serialise(result, refresh_token):
    return json.dumps({
        "refresh_token": refresh_token,
        "access_token": result.get("access_token"),
        "expires_at": time.time() + int(result.get("expires_in", 0)),
    })
