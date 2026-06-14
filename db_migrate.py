"""
SQLite schema migrations for mauxy.

The `actions.db` schema is a versioned development artifact: every change ships as an
integer-numbered SQL patch under `db-patches/` (`NNNN_name.sql`), committed to git. Each DB
records which patches it has applied in a `schema_migrations` table, so `apply_pending` runs
only what's missing, in order, idempotently. Deployers apply patches via
`scripts/sqlite_db.py --migrate`; the app only *checks* the version at startup.

This module is plain sync `sqlite3` (no async) so it is reusable from both the CLI and the
app's startup check. Both DDL and DML (e.g. quiz-content seed) patches share one sequence.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PATCHES_DIR = Path(__file__).resolve().with_name("db-patches")
_PATCH_RE = re.compile(r"^(\d+)_(.+)\.sql$")
# Meta table; excluded from schema diffs since it is created here, not by a patch.
_META_TABLE = "schema_migrations"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_patches():
    """All committed patches as (version:int, name:str, path:Path), sorted by version."""
    patches = []
    if PATCHES_DIR.is_dir():
        for p in PATCHES_DIR.iterdir():
            m = _PATCH_RE.match(p.name)
            if m:
                patches.append((int(m.group(1)), m.group(2), p))
    patches.sort(key=lambda t: t[0])
    return patches


def latest_version() -> int:
    """Highest patch number shipped in db-patches/ (0 if none)."""
    patches = list_patches()
    return patches[-1][0] if patches else 0


def ensure_migrations_table(conn: sqlite3.Connection):
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_META_TABLE} (
                version    INTEGER PRIMARY KEY,
                name       TEXT,
                applied_at TEXT
            )"""
    )


def applied_versions(conn: sqlite3.Connection) -> set:
    """Set of applied patch versions; empty if the meta table doesn't exist yet."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (_META_TABLE,)
    ).fetchone()
    if not row:
        return set()
    return {r[0] for r in conn.execute(f"SELECT version FROM {_META_TABLE}")}


def check(db_path) -> dict:
    """Report migration state WITHOUT mutating the DB (no file is created if absent).

    Returns {current, latest, pending: [(version, name), ...]}."""
    latest = latest_version()
    patches = list_patches()
    db_path = Path(db_path)
    if not db_path.exists():
        return {"current": 0, "latest": latest,
                "pending": [(v, n) for v, n, _ in patches]}
    conn = sqlite3.connect(db_path)
    try:
        done = applied_versions(conn)
    finally:
        conn.close()
    current = max(done) if done else 0
    pending = [(v, n) for v, n, _ in patches if v not in done]
    return {"current": current, "latest": latest, "pending": pending}


def apply_pending(db_path) -> list:
    """Apply every patch not yet recorded, in order, each in its own transaction.
    Returns the list of versions applied (empty if already up to date)."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    applied = []
    try:
        ensure_migrations_table(conn)
        conn.commit()
        done = applied_versions(conn)
        for version, name, path in list_patches():
            if version in done:
                continue
            sql = path.read_text()
            try:
                conn.executescript(sql)
                conn.execute(
                    f"INSERT INTO {_META_TABLE} (version, name, applied_at) VALUES (?,?,?)",
                    (version, name, _now()),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            applied.append(version)
    finally:
        conn.close()
    return applied


# -- Schema diff generator ---------------------------------------------------
def _schema_of(conn: sqlite3.Connection) -> dict:
    """Snapshot a DB's schema: {tables: {name: {sql, cols: {col: (type, notnull, dflt)}}},
    indexes: {name: sql}}. The meta + sqlite internal tables are excluded."""
    tables = {}
    for name, sql in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name != ?", (_META_TABLE,)
    ):
        cols = {}
        for _cid, cname, ctype, notnull, dflt, _pk in conn.execute(f"PRAGMA table_info('{name}')"):
            cols[cname] = (ctype, notnull, dflt)
        tables[name] = {"sql": sql, "cols": cols}
    indexes = {
        name: sql
        for name, sql in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
    }
    return {"tables": tables, "indexes": indexes}


def _expected_schema() -> dict:
    """Schema produced by applying ALL committed patches to a throwaway in-memory DB."""
    conn = sqlite3.connect(":memory:")
    try:
        for _v, _n, path in list_patches():
            conn.executescript(path.read_text())
        return _schema_of(conn)
    finally:
        conn.close()


def _column_decl(col: str, spec: tuple) -> tuple:
    """Build an `ALTER TABLE ADD COLUMN` decl. Returns (sql, warning_or_None).
    SQLite can't add a NOT NULL column without a default, so that case is flagged."""
    ctype, notnull, dflt = spec
    decl = f"{col} {ctype}".rstrip()
    warn = None
    if dflt is not None:
        decl += f" DEFAULT {dflt}"
    if notnull:
        if dflt is None:
            warn = f"column {col} is NOT NULL without a default -- SQLite rejects ADD COLUMN; set a default or rebuild"
        else:
            decl += " NOT NULL"
    return decl, warn


def generate(dev_db_path, name: str) -> dict:
    """Diff the dev DB schema against the schema rebuilt from all committed patches and
    write the additive delta as the next-numbered patch. Non-additive changes (drops,
    renames, retypes) are emitted as commented `-- WARNING:` stubs for manual completion.

    Returns {path, version, additions, warnings}. Raises if the dev DB is missing."""
    dev_db_path = Path(dev_db_path)
    if not dev_db_path.exists():
        raise FileNotFoundError(f"dev DB not found: {dev_db_path}")

    expected = _expected_schema()
    conn = sqlite3.connect(dev_db_path)
    try:
        current = _schema_of(conn)
    finally:
        conn.close()

    lines, warnings, additions = [], [], 0

    # New tables (+ their indexes), then new columns / new indexes on existing tables.
    for tname, tdef in current["tables"].items():
        if tname not in expected["tables"]:
            lines.append(f"{tdef['sql']};")
            additions += 1
        else:
            exp_cols = expected["tables"][tname]["cols"]
            for col, spec in tdef["cols"].items():
                if col not in exp_cols:
                    decl, warn = _column_decl(col, spec)
                    if warn:
                        warnings.append(warn)
                        lines.append(f"-- WARNING: {warn}")
                    lines.append(f"ALTER TABLE {tname} ADD COLUMN {decl};")
                    additions += 1
                elif exp_cols[col][0] != spec[0]:
                    w = f"column {tname}.{col} retyped {exp_cols[col][0]} -> {spec[0]} (needs table rebuild)"
                    warnings.append(w)
                    lines.append(f"-- WARNING: {w}")

    for iname, isql in current["indexes"].items():
        if iname not in expected["indexes"]:
            lines.append(f"{isql};")
            additions += 1

    # Non-additive removals -> warn only (never auto-drop).
    for tname in expected["tables"]:
        if tname not in current["tables"]:
            w = f"table {tname} present in patches but missing in dev DB (drop needs manual rebuild)"
            warnings.append(w)
            lines.append(f"-- WARNING: {w}")
    for tname, tdef in expected["tables"].items():
        if tname in current["tables"]:
            for col in tdef["cols"]:
                if col not in current["tables"][tname]["cols"]:
                    w = f"column {tname}.{col} present in patches but missing in dev DB (drop needs table rebuild)"
                    warnings.append(w)
                    lines.append(f"-- WARNING: {w}")

    version = latest_version() + 1
    fname = f"{version:04d}_{name}.sql"
    path = PATCHES_DIR / fname
    header = [
        f"-- {fname} -- auto-generated by sqlite_db.py --generate. REVIEW before committing.",
        f"-- Diff of {dev_db_path} against the schema rebuilt from patches <= {version - 1}.",
    ]
    if not lines:
        header.append("-- (no schema differences detected)")
    PATCHES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(header + [""] + lines) + "\n")
    return {"path": path, "version": version, "additions": additions, "warnings": warnings}
