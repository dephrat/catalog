import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Graph enforces a per-mailbox concurrency limit; exceeding it returns 429
# ApplicationThrottled per item inside an otherwise-successful $batch response.
BATCH_CONTENT_WORKERS = 2
BATCH_RETRY_BASE_SECONDS = 5
BATCH_MAX_RETRIES = 3

def get_headers(access_token):
    return {"Authorization": f"Bearer {access_token}"}

def make_request(headers, url, retries=3):
    """Make a Graph API request with retry on 429 and 504."""
    for attempt in range(retries):
        response = requests.get(url, headers=headers)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 10))
            print(f"Rate limited, waiting {retry_after}s...")
            time.sleep(retry_after)
            continue
        if response.status_code == 504:
            wait = 5 * (attempt + 1)
            print(f"Gateway timeout, retrying in {wait}s... (attempt {attempt + 1}/{retries})")
            time.sleep(wait)
            continue
        response.raise_for_status()
        try:
            return response.json()
        except Exception as e:
            raise Exception(f"JSON parse error on {url}: {e}")
    raise Exception(f"Failed after {retries} retries: {url}")



def get_thread_messages(access_token, conversation_id):
    headers = get_headers(access_token)
    url = (f"{GRAPH_BASE}/me/messages?$filter=conversationId eq '{conversation_id}'"
           "&$select=id,subject,from,toRecipients,receivedDateTime,hasAttachments,"
           "conversationId,parentFolderId,bodyPreview,webLink,body")
    return make_request(headers, url)


def batch_get_attachment_metadata(access_token, message_ids):
    """Fetch attachment metadata for multiple messages in batches of 20."""
    headers = {**get_headers(access_token), "Content-Type": "application/json"}
    results = {}

    for i in range(0, len(message_ids), 20):
        chunk = message_ids[i:i+20]
        requests_body = {
            "requests": [
                {
                    "id": str(idx),
                    "method": "GET",
                    "url": f"/me/messages/{mid}/attachments?$select=id,name,contentType,size"
                }
                for idx, mid in enumerate(chunk)
            ]
        }
        try:
            response = requests.post(
                "https://graph.microsoft.com/v1.0/$batch",
                headers=headers,
                json=requests_body
            )
            if response.status_code == 400:
                print(f"Batch metadata 400 error: {response.text}")
                for mid in chunk:
                    results[mid] = []
                continue
            response.raise_for_status()
            data = response.json()
            for r in data.get("responses", []):
                idx = int(r["id"])
                mid = chunk[idx]
                if r["status"] == 200:
                    results[mid] = r["body"].get("value", [])
                else:
                    print(f"Batch metadata error for {mid}: {r['status']} {r.get('body', '')}")
                    results[mid] = []
        except Exception as e:
            print(f"Batch attachment metadata error: {e}")
            for mid in chunk:
                results[mid] = []

    return results


def batch_get_attachment_content(access_token, message_attachment_pairs):
    """Fetch attachment content for multiple message/attachment pairs in batches of 20,
    with parallel batch calls."""
    headers = {**get_headers(access_token), "Content-Type": "application/json"}
    results = {}
    results_lock = threading.Lock()

    chunks = [message_attachment_pairs[i:i+20] for i in range(0, len(message_attachment_pairs), 20)]

    def fetch_chunk(chunk, attempt=0):
        requests_body = {
            "requests": [
                {
                    "id": str(idx),
                    "method": "GET",
                    "url": f"/me/messages/{mid}/attachments/{aid}"
                }
                for idx, (mid, aid) in enumerate(chunk)
            ]
        }
        try:
            response = requests.post(
                "https://graph.microsoft.com/v1.0/$batch",
                headers=headers,
                json=requests_body
            )
            if response.status_code == 400:
                print(f"Batch content 400 error: {response.text}")
                return {(mid, aid): None for mid, aid in chunk}
            if response.status_code == 429:
                # Bounded like the per-item throttle path below. Without the
                # attempt check a sustained 429 recursed forever, one frame and
                # one sleep at a time, hanging the sync rather than failing it.
                if attempt >= BATCH_MAX_RETRIES:
                    print(f"Batch still rate limited after {BATCH_MAX_RETRIES} "
                          "retries; giving up on this chunk")
                    return {(mid, aid): None for mid, aid in chunk}
                retry_after = int(response.headers.get("Retry-After", 10))
                print(f"Batch rate limited, waiting {retry_after}s... "
                      f"(attempt {attempt + 1}/{BATCH_MAX_RETRIES})")
                time.sleep(retry_after)
                return fetch_chunk(chunk, attempt + 1)
            response.raise_for_status()
            data = response.json()
            chunk_results = {}
            throttled = []
            for r in data.get("responses", []):
                idx = int(r["id"])
                pair = chunk[idx]
                status = r["status"]
                if status == 200:
                    chunk_results[pair] = r["body"]
                elif status in (429, 503, 504):
                    # Per-item throttling inside a 200 $batch response. These
                    # are retryable and must not be recorded as failures, or
                    # the attachment's text is silently lost.
                    retry_after = 0
                    try:
                        retry_after = int((r.get("headers") or {}).get("Retry-After", 0))
                    except (TypeError, ValueError):
                        pass
                    throttled.append((pair, retry_after))
                else:
                    print(f"Batch content error for {pair[0]}/{pair[1]}: {status} {r.get('body', '')}")
                    chunk_results[pair] = None

            if throttled and attempt < BATCH_MAX_RETRIES:
                wait = max([ra for _, ra in throttled] + [BATCH_RETRY_BASE_SECONDS * (attempt + 1)])
                print(f"{len(throttled)} attachments throttled; retrying in {wait}s "
                      f"(attempt {attempt + 1}/{BATCH_MAX_RETRIES})")
                time.sleep(wait)
                retry_pairs = [pair for pair, _ in throttled]
                chunk_results.update(fetch_chunk(retry_pairs, attempt + 1))
            elif throttled:
                print(f"{len(throttled)} attachments still throttled after "
                      f"{BATCH_MAX_RETRIES} retries; giving up")
                for pair, _ in throttled:
                    chunk_results[pair] = None

            return chunk_results
        except Exception as e:
            print(f"Batch attachment content error: {e}")
            return {(mid, aid): None for mid, aid in chunk}

    with ThreadPoolExecutor(max_workers=BATCH_CONTENT_WORKERS) as executor:
        futures = [executor.submit(fetch_chunk, chunk) for chunk in chunks]
        for future in futures:
            chunk_results = future.result()
            with results_lock:
                results.update(chunk_results)

    return results

def get_me(access_token):
    """Fetch the signed-in user's identity. Used to scope the catalog per account."""
    headers = get_headers(access_token)
    return make_request(headers, f"{GRAPH_BASE}/me?$select=id,displayName,mail,userPrincipalName")


# ── Folder enumeration ────────────────────────────────────────────────────────

# Well-known folders deliberately kept out of the catalog:
#   junkemail    - spam; costs tagging tokens and dilutes search
#   deleteditems - deliberately discarded, and auto-purged so webLinks rot
#   drafts       - incomplete, and every autosave would churn the delta feed
EXCLUDED_WELL_KNOWN = ("junkemail", "deleteditems", "drafts")


def get_excluded_folder_ids(access_token, names=EXCLUDED_WELL_KNOWN):
    """Resolve well-known folder names to ids so enumeration can skip them."""
    headers = get_headers(access_token)
    ids = set()
    for name in names:
        try:
            data = make_request(headers, f"{GRAPH_BASE}/me/mailFolders/{name}?$select=id")
            if data.get("id"):
                ids.add(data["id"])
        except Exception as e:
            print(f"Could not resolve well-known folder '{name}': {e}")
    return ids


def list_mail_folders(access_token, exclude_ids=frozenset()):
    """Walk the full mail folder tree, skipping excluded folders and their children."""
    headers = get_headers(access_token)
    folders = []
    queue = [f"{GRAPH_BASE}/me/mailFolders?$top=100&$select=id,displayName"]

    while queue:
        url = queue.pop(0)
        data = make_request(headers, url)
        for f in data.get("value", []):
            fid = f.get("id")
            if not fid or fid in exclude_ids:
                continue  # skipping the parent skips its whole subtree
            folders.append({"id": fid, "name": f.get("displayName", "")})
            queue.append(
                f"{GRAPH_BASE}/me/mailFolders/{fid}/childFolders?$top=100&$select=id,displayName"
            )
        if data.get("@odata.nextLink"):
            queue.append(data["@odata.nextLink"])

    return folders


# ── Delta sync ────────────────────────────────────────────────────────────────

# Only what's needed to detect *which* conversations changed. Full thread
# content is refetched via get_thread_messages, so the delta payload stays tiny.
DELTA_SELECT = "id,conversationId"


def delta_messages(access_token, folder_id, delta_link=None):
    """Page a folder's delta feed to completion.

    Returns (changed, removed_ids, new_delta_link, did_full_resync) where
    `changed` is a list of {id, conversationId} and `removed_ids` are message
    ids deleted or moved out of this folder.

    An expired deltaLink returns 410; we transparently restart that folder
    from scratch and report it via did_full_resync.
    """
    headers = get_headers(access_token)
    initial = (
        f"{GRAPH_BASE}/me/mailFolders/{folder_id}/messages/delta"
        f"?$select={DELTA_SELECT}&$top=500"
    )

    url = delta_link or initial
    did_full_resync = delta_link is None
    changed, removed_ids = [], []

    while True:
        try:
            data = make_request(headers, url)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (410, 400) and url != initial:
                # Token expired or rejected — restart this folder from scratch.
                print(f"Delta token invalid for folder {folder_id} ({status}); full resync.")
                url = initial
                did_full_resync = True
                changed, removed_ids = [], []
                continue
            raise

        for item in data.get("value", []):
            if "@removed" in item:
                if item.get("id"):
                    removed_ids.append(item["id"])
            elif item.get("id"):
                changed.append({
                    "id": item["id"],
                    "conversationId": item.get("conversationId"),
                })

        next_link = data.get("@odata.nextLink")
        if next_link:
            url = next_link
            continue

        return changed, removed_ids, data.get("@odata.deltaLink"), did_full_resync
