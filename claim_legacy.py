"""One-time helper: adopt pre-migration threads into a real account.

Threads indexed before the catalog became per-user are parked under a
placeholder user id. Sign in to the app once with the account that owns
them, then run:

    python claim_legacy.py you@example.com

Set DB_PATH first if the database isn't ./catalog.db.
"""
import sys
import db


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 1

    email = sys.argv[1]
    db.init_db()

    user = db.get_user_by_email(email)
    if not user:
        print(f"No account on record for {email}.")
        print("Sign in to the app with that account once, then re-run this.")
        return 1

    moved = db.claim_legacy_threads(user["user_id"])
    if moved:
        print(f"Moved {moved} legacy threads to {email} ({user['user_id']}).")
    else:
        print("No legacy threads to claim.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
