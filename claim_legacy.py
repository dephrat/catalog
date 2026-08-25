"""Adopt pre-migration threads into a real account.

Threads indexed before the catalog became per-user are parked under a
placeholder id. They are worse off than untagged: every search is scoped by
user_id, so nothing under the placeholder is visible to *any* account.

Sign in to the app once with the account that owns them, then:

    python claim_legacy.py --dry-run you@example.com   # review first
    python claim_legacy.py you@example.com

Set DB_PATH first if the database isn't ./catalog.db. On a hosted instance
that means the mounted disk, e.g. DB_PATH=/data/catalog.db.

Threads the account already holds are left where they are. The primary key
is (user_id, thread_id), so moving one would violate it — and a single
collision aborts the whole statement, which is why this used to fail
outright. Its own copy came from a later sync and is the fresher one, so the
legacy duplicate is redundant; --drop-duplicates deletes those instead of
leaving them parked.
"""
import sys

import db


def _preview(email, user_id):
    summary = db.legacy_summary()
    if not summary["count"]:
        print("Nothing is parked under the legacy placeholder.")
        return False

    collisions = db.legacy_collisions(user_id)
    movable = summary["count"] - len(collisions)

    print(f"{summary['count']} legacy threads, "
          f"{summary['oldest'][:10] if summary['oldest'] else '?'} to "
          f"{summary['newest'][:10] if summary['newest'] else '?'}")
    print()
    print("Oldest few, so you can confirm whose mail this is:")
    for row in summary["sample"]:
        import json
        try:
            people = ", ".join(json.loads(row["participants"] or "[]"))
        except (ValueError, TypeError):
            people = ""
        print(f"  {(row['date_first'] or '')[:10]}  {row['subject'] or '(no subject)'}")
        if people:
            print(f"              {people[:96]}")
    print()
    print(f"Would move to {email}: {movable}")
    if collisions:
        print(f"Already held by {email}, so left alone: {len(collisions)}")
        print("  (pass --drop-duplicates to delete those legacy copies instead)")
    return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    drop = "--drop-duplicates" in sys.argv

    if len(args) != 1:
        print(__doc__)
        return 1

    email = args[0]
    db.init_db()

    user = db.get_user_by_email(email)
    if not user:
        print(f"No account on record for {email}.")
        print("Sign in to the app with that account once, then re-run this.")
        return 1

    if not _preview(email, user["user_id"]):
        return 0

    if dry_run:
        print()
        print("Dry run — nothing was changed.")
        return 0

    moved, duplicates = db.claim_legacy_threads(user["user_id"], drop_duplicates=drop)
    print()
    print(f"Moved {moved} threads to {email} ({user['user_id']}).")
    if duplicates:
        verb = "Deleted" if drop else "Left parked"
        print(f"{verb} {duplicates} legacy copies the account already had.")

    remaining = db.legacy_summary()["count"]
    print(f"Still under the placeholder: {remaining}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
