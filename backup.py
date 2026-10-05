"""
SQLite online backups with retention pruning.

Usage:
    python backup.py              # one-shot backup, then exit
    BACKUP_DIR=D:\\backups python backup.py

The web app also runs a background thread (see app.py) that calls
backup_loop(): one backup at startup, then every BACKUP_INTERVAL
seconds. Keep the newest BACKUP_KEEP files; older ones are pruned.

Restore procedure (see README):
    1. Stop the app.
    2. Replace uptime.db with the backup file (delete uptime.db-wal
       and uptime.db-shm if present).
    3. Start the app.
"""

import os
import glob
import time
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def db_path():
    return os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "uptime.db")


def backup_dir():
    return os.environ.get("BACKUP_DIR") or os.path.join(BASE_DIR, "backups")


def backup_interval():
    try:
        return max(60, int(os.environ.get("BACKUP_INTERVAL", "86400")))
    except ValueError:
        return 86400


def backup_keep():
    try:
        return max(1, int(os.environ.get("BACKUP_KEEP", "7")))
    except ValueError:
        return 7


def _dest_name():
    # Sortable timestamp so lexicographic order == chronological order.
    return "uptime-" + time.strftime("%Y%m%d-%H%M%S") + ".db"


def create_backup(target_dir=None, source=None, keep=None):
    """
    Copy the live database with the SQLite backup API (safe while the
    app is running, WAL included). Prunes old backups afterwards.
    Returns the path of the new backup file.
    Raises RuntimeError if the copy fails its integrity check.
    """
    target_dir = target_dir or backup_dir()
    source = source or db_path()
    keep = keep if keep is not None else backup_keep()

    if not os.path.exists(source):
        raise FileNotFoundError(f"Database not found: {source}")

    os.makedirs(target_dir, exist_ok=True)
    dest = os.path.join(target_dir, _dest_name())

    src = sqlite3.connect(source, timeout=30)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)          # consistent snapshot, even mid-write
    finally:
        dst.close()
        src.close()

    # Refuse to keep a corrupt snapshot.
    check = sqlite3.connect(dest)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if result != "ok":
        os.remove(dest)
        raise RuntimeError(f"Backup failed integrity check: {result}")

    prune_backups(target_dir, keep)
    return dest


def list_backups(target_dir=None):
    """Newest first: [{name, path, size, mtime}]."""
    target_dir = target_dir or backup_dir()
    files = glob.glob(os.path.join(target_dir, "uptime-*.db"))
    out = []
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({
            "name": os.path.basename(path),
            "path": path,
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    out.sort(key=lambda f: f["name"], reverse=True)
    return out


def prune_backups(target_dir=None, keep=None):
    """Delete all but the newest `keep` backups. Returns removed count."""
    target_dir = target_dir or backup_dir()
    keep = keep if keep is not None else backup_keep()
    removed = 0
    for extra in list_backups(target_dir)[keep:]:
        try:
            os.remove(extra["path"])
            removed += 1
        except OSError:
            pass
    return removed


def backup_loop():
    """Runs forever inside the app's backup thread."""
    import logging

    log = logging.getLogger("backup")
    while True:
        try:
            path = create_backup()
            size = os.path.getsize(path)
            log.info(
                f"Backup created: {path} "
                f"({size / 1024:.0f} KB, keeping {backup_keep()})"
            )
        except Exception as error:
            log.warning(f"Backup failed: {error}")
        time.sleep(backup_interval())


if __name__ == "__main__":
    try:
        created = create_backup()
        print(f"Backup created: {created}")
        print(f"Backups in {backup_dir()}: {len(list_backups())}")
    except Exception as exc:
        print(f"Backup failed: {exc}")
        raise SystemExit(1)
