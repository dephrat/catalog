# Catalog

Search your own email by whatever you actually remember about it.

You rarely recall how a message was worded — you recall that it was *about the
car loan*, or *from the dentist*, or *had the bank statement attached*. Catalog
syncs a mailbox, extracts text from PDF and DOCX attachments, and has an LLM
generate search tags for every thread: topics, names, organisations, document
types, synonyms, plausible misspellings. Then it lets you search those.

On top of the tag index sits **Detective**, an agentic loop for the case where
you can't remember enough to search directly. You describe what you're looking
for in prose; it runs parallel queries, reads the results, follows leads it
finds in them, and either surfaces the thread or explains where the document
probably is and who to ask.

Built against a real 11,000-thread personal archive.

---

## How it works

```
Graph delta feed ──▶ changed thread ids
                         │
                         ▼
                  refetch whole threads ──▶ extract bodies + attachments
                                                      │
                                                      ▼
                                            Claude Haiku (batched)
                                                      │
                                                      ▼
                                            SQLite: threads + tags
                                                   │        │
                                        filtered search   Detective loop
```

**Sync is incremental.** Each mail folder has a delta cursor; a re-sync fetches
only what changed. Delta is used as a *change detector* rather than a data
source — it reports which conversations moved, then those conversations are
refetched in full, so thread reconstruction always sees complete context. On a
real mailbox this is the difference between re-reading 11k threads and reading
about nine.

A folder's cursor only advances once the threads it reported are in the
database. Moving it earlier is the one unrecoverable mistake available here:
the next scan would simply never mention that mail again. Re-reading a folder
costs one delta call and nothing else, so when a sync fails anywhere before
storage, every cursor is held and the next run re-reads.

**Tagging is batched.** A first-time import goes through Anthropic's Message
Batches API at half price. Threads are stored *before* tagging, so a large
import is searchable by subject and sender within minutes while tags fill in
behind it. Small incremental syncs stay on the real-time API, where the saving
is pennies and the latency is not.

**Providers are abstracted.** Change detection is modelled as *sources with
cursors*, not folders with tokens, because Microsoft Graph exposes a delta feed
per folder while Gmail exposes one mailbox-wide history feed. A provider
needing a single cursor returns a single source. Messages are normalised at the
provider boundary so nothing above it knows which mail API it is talking to.

## Detective

A prose description goes in; the loop runs up to 20 rounds, each issuing three
queries in parallel with independent filters. Results are merged, deduplicated
and summarised back into the conversation, so round *n+1* reasons over
everything found so far. It terminates when the model concludes, when queries
run dry, or at the round cap.

The system prompt and the growing history are cached across rounds, which
matters: history is resent from the top every round, so cost grows
quadratically without it.

## Performance

Measured on the 11,000-thread corpus, p50 / p95 over 30 runs:

| Query | Matches | p50 | p95 |
|---|---|---|---|
| two terms, narrow | 15 | 78ms | 319ms |
| one term, broad | 1,409 | 67ms | 186ms |
| matches nearly everything | 11,109 | 33ms | 72ms |

Honest reading: **it is fast because the corpus is small, not because it is
well indexed.** Tags are stored as JSON and searched with `LIKE '%term%'`, which
no B-tree can serve, so every query scans one user's partition. It degrades
linearly. SQLite FTS5 is the right fix and would also bring real ranking
instead of substring matching, where `car` matches `carpet`.

Results are capped at 200 per page. Before that cap, a broad query took 3.7s —
not from the query, but from serialising and rendering every match.

## Try it in one minute

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python app.py --demo
```

No configuration, no mailbox, no keys. `--demo` seeds a fabricated catalog —
326 threads spanning a decade of one invented household's mail: the dentist,
the bank, the plumber, the school, a sister planning midsummer, and the bulk
mail around all of it — then serves the real app against it. Search and every
filter are fully live; try `honda car loan`, `water heater invoice`, or
`annika midsummer`. Detective works too if `ANTHROPIC_API_KEY` is set.

Demo mode cannot be reached in a deployment: it requires being executed as a
script with the flag, and gunicorn rejects unknown arguments, so a worker
cannot start with `--demo` and no environment variable can switch it on. It
also writes to its own `demo_catalog.db`, never a real catalog.

## Setup with a real mailbox

```bash
cp .env.example .env      # then fill it in
python app.py
```

Requires an Azure app registration for personal Microsoft accounts
(`Mail.Read`, `User.Read`) and an Anthropic API key. `.env.example` documents
every variable, including which Azure field is which — the client secret is the
**Value** column, not the Secret ID, and it is shown exactly once.

`ADMIN_EMAIL` is required. Access control is fail-closed: with it unset, nobody
can sign in, including you.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

182 tests, no network: the mail provider, the Anthropic client and the Graph
transport are all stubbed, so the suite runs offline in about five seconds.

Most of them encode a specific failure this project has already had — a
re-sync wiping tags it had paid for, a partial batch marking the remainder
done, per-item throttling hidden inside a successful `$batch` response, a
delta cursor advancing past mail that was never stored. Reverting any of
those fixes turns a test red, which is the only real evidence a regression
suite works.

The sync tests drive `run_sync` through the provider interface rather than
through Graph, so a second provider inherits them.

## Access control

Catalog is multi-tenant — each signed-in account gets an isolated catalog — so a
public deployment would otherwise let anyone index their mailbox on the
operator's API key. Sign-in therefore creates an approval request; an
unapproved account gets no session at all. Approve or deny from `/admin`, or
from emailed links that are HMAC-signed with an expiry and apply on POST rather
than GET, because mail scanners prefetch links and would otherwise approve
every request that reached an inbox.

## Operational notes

A few things this handles because they actually happened:

- A deploy mid-batch kills the polling thread. Batch ids are persisted and
  collected at startup; progress carries a heartbeat so a dead sync is
  distinguishable from a slow one, and the UI offers a restart instead of a
  frozen bar.
- Graph returns `429 ApplicationThrottled` for individual items *inside* an
  otherwise-successful `$batch` response. Those are retried with backoff rather
  than recorded as permanent failures, which would silently drop attachment
  text.
- A transient API error must not delete a pending batch record — the batch is
  probably finished and already billed. Records are kept and retried, and
  dropped only past the results retention window.
- Catalogs export and import with tags intact, so moving one between machines
  costs nothing instead of re-running the tagger.

## Secret scanning

`scripts/check_secrets.py` blocks secrets and real mailbox data at commit time.
It reads file *bytes* rather than diffs, because a vim swap file is binary: git
prints "Binary files differ" and a diff-based grep sees nothing while a live
credential sits in the blob.

```bash
git config core.hooksPath .githooks     # enable the pre-commit hook
python scripts/check_secrets.py --all      # tracked files
python scripts/check_secrets.py --history  # every blob in every commit
python scripts/check_secrets.py --tree DIR --strict   # a publish candidate
```

`scripts/new_clean_repo.sh` builds a publishable copy with no history, refusing
to commit if anything is found.

## Limitations

- Substring matching, not full-text search. No stemming, no ranking.
- No pagination beyond the 200-result cap.
- Detective's breadth control is prompt-driven; the only mechanical signal is a
  result-count threshold.
- Single gunicorn worker by design — job state is in-process. Concurrency comes
  from threads.
- Attachment extraction covers PDF and DOCX only.

## Design notes

[`NOTES.md`](NOTES.md) covers why the architecture is shaped this way — the
delta-as-change-detector decision, measured tagging costs and the levers that
were rejected on the numbers, where prompt caching helps and where it cannot,
and the production failures that shaped the code.

## License

Apache 2.0. You can run it, fork it, and build on it. Keep the copyright
notice and the `NOTICE` file with anything you redistribute, and say so if you
modified it.

Worth being clear about what that does and does not cover: the licence governs
this code, not the design. If you want to build your own agentic search loop
over a tagged archive, the README describes how this one works and you owe
nothing for reading it.

## Stack

Python 3, Flask, SQLite, Jinja2, vanilla JavaScript (no build step). Microsoft
Graph for mail, Anthropic Claude for tagging and Detective.
