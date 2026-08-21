#!/bin/sh
# Stage a publishable copy of Catalog with no git history.
#
# Two purges of this repo each missed something the next one found: the first
# left third-party PII in templates, the second left a vim swap file holding
# live credentials. A single reviewable commit is verifiable by inspection in a
# way a rewritten history is not.
#
#   ./scripts/new_clean_repo.sh ../catalog-public
set -e
DEST="${1:-../catalog-public}"
SRC="$(git rev-parse --show-toplevel)"

# --force rebuilds over an existing checkout. Without an update path this
# script only ever did first publishes, so the public copy drifted from this
# one and the gap was closed by hand — which is how HANDOFF.md reached a
# public commit in the first place.
if [ "$2" = "--force" ] || [ "$1" = "--force" ]; then
  [ "$1" = "--force" ] && DEST="${2:-../catalog-public}"
  rm -rf "$DEST"
elif [ -e "$DEST" ]; then
  echo "refusing: $DEST already exists (pass --force to rebuild it)"
  exit 1
fi

echo "Copying tracked files only (no .git, no ignored files)..."
mkdir -p "$DEST"
git -C "$SRC" archive HEAD | tar -x -C "$DEST"

# git archive copies whatever is in HEAD, including files that should never
# have been tracked. Strip them from disk before anything else happens.
find "$DEST" \( -name ".env" -o -name ".env.*" ! -name ".env.example" \
     -o -name "*.sw?" -o -name "*.db" -o -name "*.sqlite*" \
     -o -name "*.db.backup" -o -name "*.pem" -o -name "*.key" \) \
     -type f -print -delete | sed 's/^/  stripped: /'

# Private-only documents. These carry no secret the scanner would catch —
# HANDOFF.md is session-continuity notes, and it explains why this repo's
# history must stay private, which is not a thing to publish. The first
# public build missed this and the file had to be removed by hand afterwards.
for private_doc in HANDOFF.md; do
  [ -f "$DEST/$private_doc" ] && rm -v "$DEST/$private_doc" | sed 's/^/  stripped: /'
done

cd "$DEST"
git init -q -b main   # not "master": the remote default is main
git config core.hooksPath .githooks

echo "Scanning the directory on disk (tracked, untracked, ignored alike)..."
python3 scripts/check_secrets.py --tree . --strict || {
  echo
  echo "REFUSING — secrets present on disk. Remove them and rerun."
  exit 1
}

echo "Scanning what would be committed..."
git add -A
python3 scripts/check_secrets.py --staged || {
  echo
  echo "REFUSING TO COMMIT — fix the findings above, then rerun."
  exit 1
}

git commit -q -m "Catalog: agentic email search over a personal mail archive

Syncs a Microsoft mailbox via Graph, groups messages into threads,
extracts text from PDF/DOCX attachments, and generates search tags with
Claude Haiku. Two search surfaces: a filtered results page, and an
agentic loop that runs parallel queries across multiple rounds, following
leads until it finds the thread or explains where the document actually is.

Python/Flask, SQLite, vanilla JS. Per-user catalogs with approval-gated
access, incremental sync via Graph delta, and batch tagging at half price."

echo
echo "Clean repo ready at $DEST"
git log --oneline
echo
echo "Verifying history of the NEW repo:"
python3 scripts/check_secrets.py --history
