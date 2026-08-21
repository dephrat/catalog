"""One-off maintenance: de-duplicate and cap tags already in the database.

The tagger now normalises tags at write time (see tagger.clean_tags), but rows
written before that can contain duplicates and runaway counts. This rewrites
them in place. Non-destructive in meaning: it only removes repeats and trims to
tagger.MAX_TAGS, never invents or reorders surviving tags.

    python cleanup_tags.py --dry-run
    python cleanup_tags.py
"""
import json
import sys

import db
import tagger


def main():
    dry_run = "--dry-run" in sys.argv
    conn = db.get_db()
    rows = conn.execute(
        "SELECT user_id, thread_id, ai_tags, user_tags FROM threads"
    ).fetchall()

    changed = removed_total = 0
    updates = []
    for r in rows:
        for col in ("ai_tags", "user_tags"):
            try:
                tags = json.loads(r[col] or "[]")
            except (ValueError, TypeError):
                continue
            if not isinstance(tags, list):
                continue
            cleaned = tagger.clean_tags(tags)
            if cleaned != tags:
                changed += 1
                removed_total += len(tags) - len(cleaned)
                updates.append((col, json.dumps(cleaned), r["user_id"], r["thread_id"]))

    print(f"{len(rows)} threads scanned; {changed} tag lists need cleaning "
          f"({removed_total} redundant tags)")

    if dry_run:
        print("dry run — nothing written")
    else:
        for col, value, user_id, thread_id in updates:
            conn.execute(
                f"UPDATE threads SET {col}=? WHERE user_id=? AND thread_id=?",
                (value, user_id, thread_id)
            )
        conn.commit()
        print("written")
    conn.close()


if __name__ == "__main__":
    main()
