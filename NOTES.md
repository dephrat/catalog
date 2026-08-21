# Design notes

Why Catalog is built the way it is, and what the numbers said. Most of these
decisions changed after measurement, so the measurements are included.

---

## Delta is a change detector, not a data source

Microsoft Graph's delta feed reports which messages changed since a cursor.
The obvious design is to use those messages directly — but a delta hands you
one new reply in isolation, while thread reconstruction needs the whole
conversation to compute participants, date range and attachment flags. Merging
a lone message into a stored thread means reimplementing that logic against
partial data.

So delta is queried with `$select=id,conversationId` only — a tiny payload —
purely to learn *which conversations moved*. Those conversations are then
refetched in full and rebuilt by the same code path as a first-time sync.

The trade is one extra API call per changed thread, in exchange for one code
path instead of two. On a real mailbox: 9 attachment-bearing threads changed in
90 days, so that extra call is negligible.

A useful side effect: read/unread flips show up in the delta feed but don't
change content, so the refetched thread is identical and gets dropped before
tagging. Marking mail as read costs one cheap API call and zero tokens.

## Providers expose sources with cursors, not folders with tokens

Graph's delta feed is **per mail folder**. Gmail's history feed is
**mailbox-wide**. A folder-shaped interface would have forced Gmail to invent
fake folders.

The interface is therefore `list_change_sources()` and
`changes_for_source(source_id, cursor)`. Microsoft returns one source per
folder; Gmail returns a single source with one `historyId`. Neither pretends to
be the other.

Messages are normalised at the provider boundary — `from_addr`, `to_addrs`,
`date`, `body`, `container_id` — so nothing above that layer knows which mail
API it is talking to. Attachment bytes arrive **already decoded**, which keeps
base64 vs base64url out of the extractor.

## Tagging cost: measured, not estimated

Initial estimates were wrong three times, each guessed from token counts rather
than measured. Actual figures for an 11.6k-thread mailbox:

| | |
|---|---|
| Real-time tagging | **$14.31** |
| Batch API | **$6.65** |
| Input share | ~68%, dominated by body text |
| Output | ~79 tokens/thread |

Cost varies about **3x with mail composition** — a mailbox of short
notifications is far cheaper per thread than one of long correspondence. Quote
a range, not a number.

Four cost levers were considered. Two survived:

- **Batch API** — 50% off, no quality cost. Tagging is asynchronous and already
  grouped, so it is a textbook fit. Worth more than every other lever combined.
- **Larger batches** — the ~375-token instruction block is resent per request,
  so 10 threads per request instead of 3 drops its share from 125 to 37 tokens
  per thread.

And two were rejected on the numbers:

- **Fewer tags** — saves ~$1.40 but attacks the recall the tool exists for.
  Output is only a third of cost; the lever is smaller than it looks.
- **Skipping bulk mail** — 12% of threads but only 11% of body text (~$1.15),
  and the heuristic would wrongly skip legitimate `info@` senders.

## Prompt caching helps Detective and cannot help the tagger

Caching rewards a large stable prefix reused many times.

**Detective** has exactly that: a ~1,100-token system prompt plus a
conversation history resent from the top every round, for up to 20 rounds.
Without caching, cost grows quadratically with round count. Two breakpoints —
one on the system prompt, one on the last message of the history — mean the
accumulated prefix is read at ~0.1x and only the new turn is written.

**The tagger** has the opposite shape: ~375 tokens of stable instructions
against thousands of tokens of email text that differ every call. It is also
below Haiku's 4,096-token minimum cacheable prefix, so it silently would not
cache even if the shape were right. Ceiling on the whole idea was ~$1.25 of a
$14 bill.

Worth noting the minimum is not monotonic across models — some newer models
cache from 512 tokens while Haiku 4.5 requires 4,096.

## Store before tagging

A first import used to show nothing until the whole run finished. Threads are
now written to the database as soon as they are rebuilt, before any tagging, so
a large import is searchable by subject, sender and date within minutes while
tags fill in behind it.

This also makes batch tagging viable: the batch may take up to an hour, but the
catalog is usable immediately, and the batch id is persisted so a restart
resumes collection rather than paying twice.

## Search: honest about what it is

Tags are stored as a JSON array and searched with `LIKE '%term%'`. Measured on
11k threads, p50 / p95 over 30 runs:

| Query | Matches | p50 | p95 |
|---|---|---|---|
| two terms, narrow | 15 | 78ms | 319ms |
| one term, broad | 1,409 | 67ms | 186ms |
| matches nearly everything | 11,109 | 33ms | 72ms |

The only index is `(user_id, last_synced)`. A leading wildcard cannot use a
B-tree, so every query scans one user's partition and evaluates four `LIKE`
predicates per row. **It is fast because 11k rows is small, not because it is
well indexed.** It degrades linearly, and `car` matches `carpet`.

FTS5 is the right answer — an actual inverted index plus ranking. It has not
been needed yet, which is the only reason it is not there.

Counter-intuitively the broadest query is fastest: it short-circuits on the
first predicate, while a narrow query evaluates all four on every row.

## Adding instrumentation immediately found a bug

There was no timing anywhere, so no claim about performance was checkable.
Adding a response-time header and slow-request logging surfaced that broad
searches took 3.1–4.9 seconds — not from the query, but because *every* match
was serialised and rendered. 11k rows meant 55k JSON parses and roughly a
quarter of a million tag elements in the DOM.

Capping results at 200 with an accurate total: **3.7s → 58ms**. Narrow queries
were unchanged, because the cost scaled with result count, not corpus size.

The general lesson is the ordinary one — you cannot fix what you do not
measure — but the specific one is worth keeping: the bottleneck was in
rendering, not in the database, and no amount of query tuning would have found
it.

## A bug found in a distribution, and the first theory was wrong

One thread had 460 tags where the prompt asks for at most 50. The obvious
hypothesis was that the JSON-salvage path — which recovers tag arrays from
truncated model output — was concatenating arrays.

Testing the parser directly disproved it: two arrays in, two arrays out,
truncation yields an empty list rather than a merge. Reading the thread's tags
end to end showed a single coherent topic tagged almost word by word, including
filler like `take, moment, review, ideally`. 460 tags, 293 distinct — the model
had over-generated and repeated itself.

Fixed at the write path (normalise, dedupe, cap) rather than in the parser,
which was innocent, and backfilled: 286 threads had duplicates, now zero.

## Failure modes that shaped the code

Each of these happened in production and left a mark:

- **A deploy mid-batch killed the polling thread.** The batch completed and was
  billed, but nothing collected it, and the UI showed a progress bar that would
  never move. Now: batch ids persist, collection runs at startup, progress
  carries a heartbeat, and a stale sync offers a restart instead of a frozen
  bar.
- **A transient API error deleted a pending batch record.** One unreadable
  status check discarded work that was probably finished and already paid for.
  Records are now kept and retried, dropped only past the results retention
  window.
- **Graph returns `429` for individual items inside an otherwise-successful
  `$batch` response.** The retry logic only covered the HTTP status of the
  batch call itself, so throttled attachments were silently recorded as
  permanent failures and their text never reached the tagger.
- **`print()` never reached the platform's log stream.** Container stdout is a
  pipe, so Python block-buffers it. Every diagnostic was invisible for hours
  while looking perfectly fine locally.
- **A 19MB import was truncated in transit** and reported only "not valid
  JSON". Export is gzipped now (2.2MB) and import errors name the cause.

## The test suite is the incident ledger, executed

The suite was written after most of this document, which decided its shape.
Rather than aiming at coverage, each test encodes a failure that had already
happened in production: a re-sync wiping tags that had been paid for, a partial
batch marking the remainder done, per-item throttling hidden inside a
successful `$batch` response, a delta cursor advancing past mail that was never
stored.

That gives a cheap check on whether the suite is worth anything — revert a fix
and a test must go red. A suite written to cover lines offers no such
guarantee, and the four bugs above were all found by reading code, not by
tests that were missing them.

The provider interface pays off here too. The sync tests drive `run_sync`
through `MailProvider` rather than through Graph, so the Gmail implementation
inherits them rather than needing its own.

## Secret scanning reads bytes, not diffs

A vim swap file on `.env` was committed with live credentials inside it. The
pre-commit check in place at the time grepped `git diff --cached` — and a swap
file is binary, so git printed "Binary files differ" and the grep matched
nothing while the secret sat in the blob.

`scripts/check_secrets.py` reads file **bytes**. Building it surfaced several
of its own bugs, each worth the fix:

- `\s*` after `=` matched newlines, so `KEY=` on a blank line matched whatever
  followed on the next line.
- `KEY = os.getenv(...)` in ordinary config code tripped it — which trains
  people to pass `--no-verify`, defeating the point.
- It checked staged files and history but never the working directory, so an
  ignored file carrying credentials was invisible to it.
- Reporting git-ignored findings as acceptable *everywhere* then silently
  weakened the publish check. Dev trees are lenient; publish candidates use
  `--strict`.
- It scanned a 40MB database for every address when the filename alone already
  condemned it — a two-minute run reduced to 0.4s.

The recurring theme: verify the bytes, not a projection of them.
