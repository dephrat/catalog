import msal
import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

# Consumer (personal) Microsoft accounts. A single-tenant work/school app
# would use /<tenant-id> here instead.
AUTHORITY = "https://login.microsoftonline.com/consumers"
SCOPES = ["Mail.Read", "User.Read"]
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:5000/callback")

def get_auth_url(state=None):
    app = _build_msal_app()
    return app.get_authorization_request_url(
        SCOPES,
        redirect_uri=REDIRECT_URI,
        prompt="select_account",
        state=state
    )

def get_token_from_code(code):
    cache = msal.SerializableTokenCache()
    app = _build_msal_app(cache=cache)
    result = app.acquire_token_by_authorization_code(
        code,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI
    )
    if "access_token" in result:
        return result, cache.serialize()
    raise Exception(f"Auth failed: {result.get('error_description', 'Unknown error')}")

def get_valid_token(token_cache_data):
    """Get a valid access token, refreshing if necessary."""
    cache = msal.SerializableTokenCache()
    if token_cache_data:
        cache.deserialize(token_cache_data)

    app = _build_msal_app(cache=cache)
    accounts = app.get_accounts()

    if not accounts:
        return None, None

    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if result and "access_token" in result:
        return result["access_token"], cache.serialize()

    return None, None

def _build_msal_app(cache=None):
    return msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=AUTHORITY,
        client_credential=CLIENT_SECRET,
        token_cache=cache
    )