"""Consistent, verified snapshots of the catalog.

The database is the only copy of a personal mail archive and of the tagging
that was paid for to build it. Losing it is the one failure here with no
recovery path, so snapshots are taken with SQLite's online backup API rather
than by copying the file: `cp` on a live database can capture a write in
progress and produce a file that only fails when you finally need it.

Every snapshot is opened and counted before it is kept, because an unverified
backup is a guess.

    python backup.py                     # take one, prune to --keep
    python backup.py --list              # what exists, and how big
    python backup.py --verify FILE       # re-check an old snapshot
    python backup.py --restore FILE      # put one back

Set DB_PATH first if the database isn't ./catalog.db. On a hosted instance
that means the mounted disk, e.g. DB_PATH=/data/catalog.db.

Snapshots land beside the database by default, which protects against a bad
import, a wipe, or corruption — but not against losing the disk itself. Pass
--dir, or copy them off the machine, for that.
"""
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

import db

KEEP_DEFAULT = 7
PREFIX = "catalog-backup-"
SUFFIX = ".db.gz"


def _arg(name, default=None):
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _backup_dir():
    return _arg("--dir") or (os.path.dirname(os.path.abspath(db.DB_PATH)) or ".")


def _snapshot_to(path):
    """Copy the live database consistently, using SQLite's own backup API.

    It takes a read lock per page rather than for the whole operation, so a
    running sync is neither blocked nor able to tear the snapshot.
    """
    source = sqlite3.connect(db.DB_PATH)
    target = sqlite3.connect(path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _describe(path):
    """Open a database and report what is in it. Returns None if unreadable."""
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return None
            threads = conn.execute("SELECT COUNT(*) c FROM threads").fetchone()["c"]
            users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
            tagged = conn.execute(
                "SELECT COUNT(*) c FROM threads "
                "WHERE ai_tags IS NOT NULL AND ai_tags != '[]'").fetchone()["c"]
            return {"threads": threads, "users": users, "tagged": tagged}
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _unzip_to_temp(path):
    fd, plain = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    with gzip.open(path, "rb") as src, open(plain, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return plain


def take(keep=KEEP_DEFAULT):
    if not os.path.exists(db.DB_PATH):
        print(f"No database at {db.DB_PATH}.")
        return 1

    live = _describe(db.DB_PATH)
    if live is None:
        print(f"Refusing: {db.DB_PATH} fails its own integrity check.")
        return 1

    directory = _backup_dir()
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = os.path.join(directory, f"{PREFIX}{stamp}{SUFFIX}")

    fd, staging = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        _snapshot_to(staging)

        # Verify before keeping. An unverified backup is a guess.
        snap = _describe(staging)
        if snap is None:
            print("Refusing: the snapshot failed its integrity check.")
            return 1
        if snap["threads"] != live["threads"]:
            print(f"Refusing: snapshot has {snap['threads']} threads, "
                  f"live has {live['threads']}.")
            return 1

        with open(staging, "rb") as src, gzip.open(final, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
    finally:
        os.unlink(staging)

    size = os.path.getsize(final)
    print(f"{os.path.basename(final)}  {size / 1e6:.1f} MB")
    print(f"  {snap['threads']:,} threads ({snap['tagged']:,} tagged), "
          f"{snap['users']} account(s) — verified")

    pruned = prune(keep)
    if pruned:
        print(f"  pruned {pruned} older snapshot(s), keeping {keep}")
    return 0


def existing():
    directory = _backup_dir()
    if not os.path.isdir(directory):
        return []
    names = [n for n in os.listdir(directory)
             if n.startswith(PREFIX) and n.endswith(SUFFIX)]
    return sorted((os.path.join(directory, n) for n in names), reverse=True)


def prune(keep):
    stale = existing()[keep:]
    for path in stale:
        os.unlink(path)
    return len(stale)


def listing():
    snapshots = existing()
    if not snapshots:
        print(f"No snapshots in {_backup_dir()}.")
        return 0
    print(f"{len(snapshots)} snapshot(s) in {_backup_dir()}:")
    for path in snapshots:
        print(f"  {os.path.basename(path):<44} {os.path.getsize(path) / 1e6:>7.1f} MB")
    return 0


def verify(path):
    if not os.path.exists(path):
        print(f"No such snapshot: {path}")
        return 1
    plain = _unzip_to_temp(path)
    try:
        info = _describe(plain)
    finally:
        os.unlink(plain)
    if info is None:
        print(f"{os.path.basename(path)}: UNREADABLE — do not rely on this one.")
        return 1
    print(f"{os.path.basename(path)}: ok — {info['threads']:,} threads "
          f"({info['tagged']:,} tagged), {info['users']} account(s)")
    return 0


def restore(path):
    if not os.path.exists(path):
        print(f"No such snapshot: {path}")
        return 1

    plain = _unzip_to_temp(path)
    try:
        info = _describe(plain)
        if info is None:
            print("Refusing: that snapshot is unreadable.")
            return 1

        current = _describe(db.DB_PATH) if os.path.exists(db.DB_PATH) else None
        print(f"Restoring {os.path.basename(path)}: {info['threads']:,} threads")
        if current:
            print(f"Replacing the database now at {db.DB_PATH}: "
                  f"{current['threads']:,} threads")

        if "--yes" not in sys.argv:
            print()
            print("Re-run with --yes to proceed. Nothing has changed.")
            return 0

        # Keep what is being replaced, so a restore of the wrong snapshot is
        # itself recoverable.
        if os.path.exists(db.DB_PATH):
            aside = f"{db.DB_PATH}.replaced-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
            shutil.copy2(db.DB_PATH, aside)
            print(f"Previous database kept at {aside}")

        shutil.copy2(plain, db.DB_PATH)
    finally:
        os.unlink(plain)

    print("Restored. Restart the app so it reopens the database.")
    return 0


def main():
    if "--list" in sys.argv:
        return listing()
    if "--verify" in sys.argv:
        return verify(_arg("--verify"))
    if "--restore" in sys.argv:
        return restore(_arg("--restore"))
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        return 0
    return take(keep=int(_arg("--keep", KEEP_DEFAULT)))


if __name__ == "__main__":
    sys.exit(main())
