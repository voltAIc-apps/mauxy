#!/usr/bin/env python3
"""
SQLite maintenance for mauxy -- the SQLite analogue of the conventional pg_db.py.

The runtime DB (`ACTION_LOG_DB`, default `/data/actions.db`) holds the `action_log` plus
the config tables `sites` / `quiz_questions` / `mautic_instance`. In production it lives on
the k8s PVC inside the pod; locally we keep a copy under `<app>/data/actions.db`.

Usage:
    python scripts/sqlite_db.py --pull            # copy the live PVC DB to a local copy + backup
    python scripts/sqlite_db.py --backup          # back up the local DB (default action)
    python scripts/sqlite_db.py --restore [name]  # restore newest (or named) backup
    python scripts/sqlite_db.py --list            # list backups

Backups live in `<app>/data/sqlite_backups/`; both that dir and `data/actions.db` are
git-ignored (`*.db`). Config (DEPLOY_NAMESPACE) comes from `.env`, overridable by real env.
"""

import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse the .env loader from the deploy tool (single source of truth, no extra deps).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy import load_dotenv  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BACKUP_DIR = DATA_DIR / "sqlite_backups"
LOCAL_DB = DATA_DIR / "actions.db"          # local copy of the runtime DB
POD_DB_PATH = "/data/actions.db"            # path inside the container (matches the PVC mount)
POD_SELECTOR = "app.kubernetes.io/name=mauxy"


def _config() -> dict:
    """`.env` values overlaid by real env vars (real env wins)."""
    cfg = load_dotenv(PROJECT_ROOT / ".env")
    cfg.update({k: v for k, v in os.environ.items() if k in ("DEPLOY_NAMESPACE", "ACTION_LOG_DB")})
    return cfg


def _local_db_path(cfg: dict) -> Path:
    """The DB this machine reads/writes. ACTION_LOG_DB wins; else the local copy."""
    return Path(cfg["ACTION_LOG_DB"]) if cfg.get("ACTION_LOG_DB") else LOCAL_DB


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_file(src: Path) -> Path:
    """Consistent online copy of `src` into the backup dir, perms 600. Returns its path."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"actions_{_timestamp()}.db"
    # sqlite .backup() is safe even while the DB is in use (snapshot copy).
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    os.chmod(dst, 0o600)
    return dst


def do_backup(cfg: dict) -> int:
    """Back up the local DB into data/sqlite_backups/."""
    src = _local_db_path(cfg)
    if not src.exists():
        print(f"no SQLite DB at {src} -- nothing to back up "
              f"(the runtime DB lives on the PVC; use --pull to fetch it)")
        return 0
    dst = _backup_file(src)
    print(f"backup -> {dst}")
    return 0


def do_pull(cfg: dict) -> int:
    """Copy the live PVC DB out of the running pod into the local copy + a backup."""
    ns = cfg.get("DEPLOY_NAMESPACE", "mauxy")
    # Resolve the running pod name from its label.
    try:
        pod = subprocess.check_output(
            ["kubectl", "get", "pods", "-n", ns, "-l", POD_SELECTOR,
             "-o", "jsonpath={.items[0].metadata.name}"],
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"cannot reach cluster / kubectl (ns={ns}): {exc} -- skipped pull", file=sys.stderr)
        return 0
    if not pod:
        print(f"no running mauxy pod in namespace {ns} -- skipped pull", file=sys.stderr)
        return 0

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"pulling {pod}:{POD_DB_PATH} (ns {ns}) -> {LOCAL_DB}")
    try:
        subprocess.run(
            ["kubectl", "cp", "-n", ns, f"{pod}:{POD_DB_PATH}", str(LOCAL_DB)],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"kubectl cp failed: {exc} -- skipped pull", file=sys.stderr)
        return 0
    os.chmod(LOCAL_DB, 0o600)
    dst = _backup_file(LOCAL_DB)
    print(f"local copy -> {LOCAL_DB}\nbackup     -> {dst}")
    return 0


def do_restore(cfg: dict, name: str | None) -> int:
    """Restore the newest backup (or one named) over the local DB."""
    backups = sorted(BACKUP_DIR.glob("actions_*.db"))
    if not backups:
        print(f"no backups in {BACKUP_DIR}", file=sys.stderr)
        return 1
    # name is "__LATEST__" when --restore was given without an argument.
    src = backups[-1] if name in (None, "__LATEST__") else (BACKUP_DIR / name)
    if not src.exists():
        print(f"backup not found: {src}", file=sys.stderr)
        return 1
    dst = _local_db_path(cfg)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    os.chmod(dst, 0o600)
    print(f"restored {src.name} -> {dst}")
    return 0


def do_list() -> int:
    """List available backups, newest last."""
    backups = sorted(BACKUP_DIR.glob("actions_*.db"))
    if not backups:
        print(f"no backups in {BACKUP_DIR}")
        return 0
    for b in backups:
        print(f"{b.name}\t{b.stat().st_size} bytes")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="SQLite backup/restore/pull for mauxy.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pull", action="store_true", help="Copy the live PVC DB to a local copy + backup.")
    g.add_argument("--backup", action="store_true", help="Back up the local DB (default).")
    g.add_argument("--restore", nargs="?", const="__LATEST__", default=None, metavar="NAME",
                   help="Restore newest backup, or NAME from the backup dir.")
    g.add_argument("--list", action="store_true", help="List backups.")
    args = p.parse_args()
    cfg = _config()

    if args.pull:
        return do_pull(cfg)
    if args.list:
        return do_list()
    if args.restore is not None:           # "__LATEST__" or an explicit name
        return do_restore(cfg, args.restore)
    # Default action: backup.
    return do_backup(cfg)


if __name__ == "__main__":
    sys.exit(main())
