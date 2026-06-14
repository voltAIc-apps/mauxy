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
    python scripts/sqlite_db.py --migrate         # apply pending db-patches/*.sql
    python scripts/sqlite_db.py --status          # show current vs shipped schema version
    python scripts/sqlite_db.py --generate NAME   # auto-generate the next patch from a schema diff
    python scripts/sqlite_db.py --new NAME        # scaffold an empty next-numbered patch
    python scripts/sqlite_db.py --dump-quiz       # dump quiz_questions as INSERT OR IGNORE SQL

Backups live in `<app>/data/sqlite_backups/`; both that dir and `data/actions.db` are
git-ignored (`*.db`). Schema patches live in `<app>/db-patches/` (committed). Config
(DEPLOY_NAMESPACE) comes from `.env`, overridable by real env.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Reuse the .env loader from the deploy tool + the shared migration logic (no duplication).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(PROJECT_ROOT))
from deploy import load_dotenv  # noqa: E402
import db_migrate  # noqa: E402
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


# -- Schema migrations (db-patches/) -----------------------------------------
def do_migrate(cfg: dict) -> int:
    """Apply pending patches to the local/target DB (the deployer's apply step)."""
    db = _local_db_path(cfg)
    applied = db_migrate.apply_pending(db)
    if applied:
        print(f"migrated {db} -> applied patches {applied}")
    else:
        print(f"{db} already up to date (version {db_migrate.check(db)['current']})")
    return 0


def do_status(cfg: dict) -> int:
    """Show the DB's current schema version vs the latest shipped patch."""
    db = _local_db_path(cfg)
    st = db_migrate.check(db)
    print(f"db:      {db}")
    print(f"current: {st['current']}")
    print(f"shipped: {st['latest']}")
    if st["pending"]:
        print("pending: " + ", ".join(f"{v:04d}_{n}" for v, n in st["pending"]))
    else:
        print("pending: none (up to date)")
    return 0


def do_generate(cfg: dict, name: str) -> int:
    """Auto-generate the next patch from a schema diff of the local dev DB."""
    db = _local_db_path(cfg)
    try:
        res = db_migrate.generate(db, name)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"generated {res['path'].relative_to(PROJECT_ROOT)} "
          f"({res['additions']} additive change(s))")
    for w in res["warnings"]:
        print(f"  WARNING: {w}", file=sys.stderr)
    print("review/edit the file, then commit it.")
    return 0


def do_new(name: str) -> int:
    """Scaffold an empty next-numbered patch for hand-authoring (e.g. content/DML)."""
    version = db_migrate.latest_version() + 1
    path = db_migrate.PATCHES_DIR / f"{version:04d}_{name}.sql"
    db_migrate.PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        print(f"refusing to overwrite {path}", file=sys.stderr)
        return 1
    path.write_text(f"-- {path.name} -- write your schema/content change below.\n\n")
    print(f"created {path.relative_to(PROJECT_ROOT)}")
    return 0


def do_dump_quiz(cfg: dict) -> int:
    """Dump quiz_questions as INSERT OR IGNORE SQL (to author/refresh a content patch)."""
    db = _local_db_path(cfg)
    if not db.exists():
        print(f"no DB at {db}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT topic, qid, question, choices_json, answer FROM quiz_questions ORDER BY topic, qid"
        ).fetchall()
    finally:
        conn.close()
    for topic, qid, question, choices_json, answer in rows:
        # Re-encode choices to normalise formatting; values are SQL-escaped.
        try:
            choices = json.dumps(json.loads(choices_json), ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            choices = choices_json
        vals = ", ".join(_sql_str(v) for v in (topic, qid, question, choices)) + f", {int(answer)}"
        print(f"INSERT OR IGNORE INTO quiz_questions (topic, qid, question, choices_json, answer) VALUES ({vals});")
    return 0


def _sql_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def main() -> int:
    p = argparse.ArgumentParser(description="SQLite backup/restore/pull for mauxy.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pull", action="store_true", help="Copy the live PVC DB to a local copy + backup.")
    g.add_argument("--backup", action="store_true", help="Back up the local DB (default).")
    g.add_argument("--restore", nargs="?", const="__LATEST__", default=None, metavar="NAME",
                   help="Restore newest backup, or NAME from the backup dir.")
    g.add_argument("--list", action="store_true", help="List backups.")
    g.add_argument("--migrate", action="store_true", help="Apply pending db-patches/*.sql.")
    g.add_argument("--status", action="store_true", help="Show current vs shipped schema version.")
    g.add_argument("--generate", metavar="NAME", help="Auto-generate the next patch from a schema diff.")
    g.add_argument("--new", metavar="NAME", help="Scaffold an empty next-numbered patch.")
    g.add_argument("--dump-quiz", action="store_true", help="Dump quiz_questions as INSERT OR IGNORE SQL.")
    args = p.parse_args()
    cfg = _config()

    if args.pull:
        return do_pull(cfg)
    if args.list:
        return do_list()
    if args.migrate:
        return do_migrate(cfg)
    if args.status:
        return do_status(cfg)
    if args.generate:
        return do_generate(cfg, args.generate)
    if args.new:
        return do_new(args.new)
    if args.dump_quiz:
        return do_dump_quiz(cfg)
    if args.restore is not None:           # "__LATEST__" or an explicit name
        return do_restore(cfg, args.restore)
    # Default action: backup.
    return do_backup(cfg)


if __name__ == "__main__":
    sys.exit(main())
