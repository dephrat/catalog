import anthropic
import json
import os
from dotenv import load_dotenv

load_dotenv()

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

BODY_CAP = 5000
ATTACHMENT_CAP = 5000

# The prompt asks for "up to 50" tags, but the model routinely overshoots and
# repeats itself — one real thread came back with 460 tags of which only 293
# were distinct. Duplicates cost storage and slow every LIKE scan without
# adding any recall, so normalise here rather than trusting the instruction.
MAX_TAGS = 60
TAG_MODEL = "claude-haiku-4-5"
MAX_OUTPUT_TOKENS = 8000

PROMPT_TEMPLATE = """You are an email tagging system. For each email thread below, generate tags that would help someone find it later using any word they might remember.
{numbered}
Be generous with tags — it's better to over-tag than under-tag. Someone searching for this email might remember it differently than it was written.

For each thread generate up to 50 lowercase tags including:
- Key topics and themes
- People's names (first and last separately)
- Organizations or companies
- Locations
- Document types (invoice, receipt, contract, etc.)
- Actions or events (payment, meeting, appointment, etc.)
- Years and time periods
- Product names, brands, services
- Variations and synonyms of key terms (e.g. "car", "vehicle", "automobile", "auto" for the same thing)
- Attachment filenames broken into meaningful words (e.g. "TD_Bank_Statement_March_2016.pdf" -> "td bank", "statement", "march", "2016", "pdf")
- Common misspellings or abbreviations someone might search
- Related concepts (e.g. if about a mortgage, also tag "home", "house", "property", "real estate")
- be generous with synonyms, include common alternatives someone might search
- For each key concept, include at least 2-3 synonyms or related terms

Return ONLY a JSON array of arrays, one per thread, in order.
Example for 2 threads: [["honda","car","loan","2016","vehicle","financing","auto"],["dentist","appointment","health","teeth","dental"]]
No explanation, no markdown."""


def clean_tags(tags):
    """Lowercase, strip, drop blanks, de-duplicate (order-preserving), cap."""
    seen = set()
    out = []
    for tag in tags:
        t = str(tag).strip().lower()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out

def salvage_tags(text, expected_count):
    """Extract complete tag arrays from truncated JSON."""
    results = []
    depth = 0
    current = ""
    in_array = False

    for char in text:
        if char == '[':
            depth += 1
            if depth == 2:
                in_array = True
                current = "["
            elif depth > 2:
                current += char
        elif char == ']':
            if depth == 2 and in_array:
                current += "]"
                try:
                    parsed = json.loads(current)
                    results.append(clean_tags(parsed))
                except:
                    results.append([])
                in_array = False
                current = ""
            elif depth > 2:
                current += char
            depth -= 1
        elif in_array:
            current += char

    while len(results) < expected_count:
        results.append([])

    return results[:expected_count]


def build_prompt(threads):
    """Render the tagging prompt for a group of threads.

    Shared by the real-time and Batch API paths so the two can never drift.
    """
    numbered = ""

    for i, t in enumerate(threads):
        body_note = ""
        if t.get("body_scan_status") == "truncated":
            body_note = f" (truncated at {BODY_CAP} chars)"

        attachment_lines = ""
        for a in t.get("attachments", []):
            status = a.get("scan_status", "ok")
            if status == "unsupported":
                attachment_lines += f"  - {a['name']}: unsupported type, filename only\n"
            elif status == "failed":
                attachment_lines += f"  - {a['name']}: failed to extract\n"
            elif status == "truncated":
                attachment_lines += f"  - {a['name']} (truncated at {ATTACHMENT_CAP} chars): {a.get('text', '')}\n"
            else:
                attachment_lines += f"  - {a['name']}: {a.get('text', '')}\n"

        if not attachment_lines:
            attachment_lines = "  none\n"

        numbered += f"""
Thread {i}:
- Subject: {t['subject']}
- Participants: {t['participants']}
- Date: {t['date']}
- Body{body_note}: {t.get('body_text', '')}
- Attachments:
{attachment_lines}"""

    return PROMPT_TEMPLATE.format(numbered=numbered)


def parse_tag_response(text, expected_count):
    """Turn a model response into (tag lists, truncated flags)."""
    text = (text or "").replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(text)
        if len(result) == expected_count:
            return [clean_tags(tags) for tags in result], [False] * expected_count
        print(f"Length mismatch ({len(result)} vs {expected_count}), attempting salvage...")
    except json.JSONDecodeError:
        print("JSON parse failed, attempting salvage...")

    result = salvage_tags(text, expected_count)
    return result, [len(tags) == 0 for tags in result]


def generate_tags_batch(threads, on_usage=None):
    """
    threads: list of dicts with keys:
        subject, participants, date, body_text, body_scan_status,
        attachments: list of {name, text, scan_status}
    returns: (list of tag lists, list of truncated flags), same order as input
    """
    prompt = build_prompt(threads)

    try:
        message = client.messages.create(
            model=TAG_MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": prompt}]
        )
        if on_usage is not None:
            _report_usage(on_usage, getattr(message, "usage", None), batched=False)
        return parse_tag_response(message.content[0].text, len(threads))
    except Exception as e:
        print(f"Batch tagging error: {e}")
        return [[] for _ in threads], [True] * len(threads)


# ── Batch API ─────────────────────────────────────────────────────────────────
#
# Tagging is asynchronous and non-latency-sensitive, so a large backfill goes
# through the Batch API at half price. Requests are grouped exactly as the
# real-time path groups them, and custom_id carries the group index so results
# can be mapped back after a process restart.

def submit_tag_batch(groups):
    """groups: [[thread_input, ...], ...] -> batch id.

    Returns None if submission fails, so the caller can fall back to
    real-time tagging rather than losing the work.
    """
    requests = [
        {
            "custom_id": f"g{i}",
            "params": {
                "model": TAG_MODEL,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": [{"role": "user", "content": build_prompt(group)}],
            },
        }
        for i, group in enumerate(groups)
    ]
    try:
        batch = client.messages.batches.create(requests=requests)
        return batch.id
    except Exception as e:
        print(f"Batch submission failed: {e}")
        return None


def batch_status(batch_id):
    """-> 'in_progress' | 'ended' | 'unknown'"""
    try:
        return client.messages.batches.retrieve(batch_id).processing_status
    except Exception as e:
        print(f"Batch status check failed: {e}")
        return "unknown"


def collect_tag_batch(batch_id, expected_counts, on_usage=None):
    """Retrieve results for an ended batch.

    expected_counts: {custom_id: number of threads in that group}
    -> {custom_id: (tag_lists, truncated_flags)}; missing entries mean that
    group errored, expired, or was cancelled and needs re-tagging.
    """
    out = {}
    try:
        for result in client.messages.batches.results(batch_id):
            cid = result.custom_id
            n = expected_counts.get(cid)
            if n is None:
                continue
            if result.result.type != "succeeded":
                print(f"Batch group {cid}: {result.result.type}")
                continue
            msg = result.result.message
            if on_usage is not None:
                _report_usage(on_usage, getattr(msg, "usage", None), batched=True)
            text = next((b.text for b in msg.content if b.type == "text"), "")
            out[cid] = parse_tag_response(text, n)
    except Exception as e:
        print(f"Batch retrieval failed: {e}")
    return out


def _report_usage(on_usage, usage, batched):
    """Hand token counts to the caller. Never let accounting break tagging."""
    if usage is None:
        return
    try:
        on_usage(
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
            getattr(usage, "cache_read_input_tokens", 0) or 0,
            batched,
        )
    except Exception as e:
        print(f"Usage recording failed: {e}")


def cancel_batch(batch_id):
    """Stop a batch we've given up waiting on, so it isn't billed twice.

    Cancellation is best-effort: requests already processed still complete
    and remain retrievable, which is why the caller collects before
    re-tagging the remainder.
    """
    try:
        client.messages.batches.cancel(batch_id)
        return True
    except Exception as e:
        print(f"Batch cancel failed: {e}")
        return False
