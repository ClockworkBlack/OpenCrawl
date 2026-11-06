"""Parallel runs: split sources across N runners, then merge what each one found.

Sharding is by *host*, so every request to a given site comes from one runner
and per-host politeness holds across the whole fleet. Adapters that talk to one
shared API host (Legistar's webapi.legistar.com, say) report it through
`shard_key`, so all their sources land in the same shard.

After a run, `delta` copies just what changed (records changed since the run
started, frontier rows fetched, cursors) into a small database; `merge` applies
deltas to the main database. State files (progress, channel choices, blocked
hosts) merge per key, newest wins.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path
from urllib.parse import urlsplit

from .store import COLS, DDL, Store


def shard_key(src: dict) -> str:
    from .adapters import ADAPTERS

    for ad in ADAPTERS.values():
        k = ad.shard_key(src)
        if k:
            return k
    return urlsplit(src.get("url", "")).netloc.lower().removeprefix("www.") or src["id"]


def parse_shard(spec: str | None) -> tuple[int, int] | None:
    if not spec:
        return None
    i, n = (int(x) for x in spec.split("/"))
    if not (n >= 1 and 0 <= i < n):
        raise ValueError(f"bad shard {spec!r}: use i/N with 0 <= i < N")
    return i, n


def in_shard(src: dict, shard: tuple[int, int] | None) -> bool:
    if not shard:
        return True
    i, n = shard
    return zlib.crc32(shard_key(src).encode()) % n == i


# --------------------------------------------------------------------------- database deltas
# a delta holds plain tables only: no full-text index, no triggers (merge refuses a delta that has any)
DELTA_DDL = ";\n".join(st.strip() for st in DDL.split(";") if st.strip().upper().startswith("CREATE TABLE")) + ";"

def write_delta(store: Store, since: str, out: Path) -> dict:
    """Copy rows changed/fetched at or after `since` into a fresh database at `out`."""
    store.flush()
    out = Path(out)
    if out.exists():
        out.unlink()
    d = sqlite3.connect(out)
    d.executescript(DELTA_DDL)
    d.close()
    db = store.db
    with store._lock:
        db.execute("ATTACH DATABASE ? AS d", (str(out),))
        try:
            db.execute("BEGIN")
            n_rec = db.execute(f"INSERT INTO d.records({','.join(COLS)}) SELECT {','.join(COLS)} FROM main.records "
                               "WHERE changed_at >= ?", (since,)).rowcount
            n_url = db.execute("INSERT INTO d.urls SELECT * FROM main.urls WHERE fetched_at >= ?", (since,)).rowcount
            n_cur = db.execute("INSERT INTO d.cursors SELECT * FROM main.cursors WHERE updated_at >= ?", (since,)).rowcount
            db.execute("COMMIT")
        finally:
            db.execute("DETACH DATABASE d")
    return {"records": n_rec, "urls": n_url, "cursors": n_cur, "file": str(out)}


def merge_delta(store: Store, delta: Path) -> dict:
    """Apply a delta database: newer versions of records win (by changed_at), frontier and cursors upserted."""
    store.flush()
    db = store.db
    cols = ",".join(COLS)
    upd = ",".join(f"{c}=excluded.{c}" for c in COLS if c not in ("key", "first_seen"))
    with store._lock:
        db.execute("ATTACH DATABASE ? AS d", (str(delta),))
        try:
            _check_delta(db)
            db.execute("BEGIN")
            n_rec = db.execute(
                f"INSERT INTO main.records({cols}) SELECT {cols} FROM d.records WHERE true "
                f"ON CONFLICT(key) DO UPDATE SET {upd}, first_seen=min(records.first_seen, excluded.first_seen) "
                f"WHERE excluded.changed_at >= records.changed_at AND excluded.hash <> records.hash").rowcount
            n_url = db.execute(
                "INSERT INTO main.urls SELECT * FROM d.urls WHERE true ON CONFLICT(url) DO UPDATE SET "
                "source_id=excluded.source_id, status=excluded.status, content_type=excluded.content_type, "
                "hash=excluded.hash, fetched_at=excluded.fetched_at, changed_at=excluded.changed_at "
                "WHERE excluded.fetched_at >= urls.fetched_at").rowcount
            n_cur = db.execute(
                "INSERT INTO main.cursors SELECT * FROM d.cursors WHERE true ON CONFLICT(source_id, stream) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at WHERE excluded.updated_at >= cursors.updated_at"
            ).rowcount
            db.execute("COMMIT")
        finally:
            db.execute("DETACH DATABASE d")
    return {"records": n_rec, "urls": n_url, "cursors": n_cur, "file": str(delta)}


def _check_delta(db) -> None:
    """A delta must be plain tables with the expected columns (no views, no triggers) and pass a quick check;
    the main connection already runs with trusted_schema=OFF, so nothing in it can call functions."""
    from .store import StoreError

    kinds = {r[0]: r[1] for r in db.execute("SELECT name, type FROM d.sqlite_master")}
    for t in ("records", "urls", "cursors"):
        if kinds.get(t) != "table":
            raise StoreError(f"delta: {t} is missing or not a table")
    if any(v == "trigger" for v in kinds.values()) or any(v == "view" for v in kinds.values()):
        raise StoreError("delta: unexpected triggers or views")
    have = {r[1] for r in db.execute("PRAGMA d.table_info(records)")}
    if not set(COLS) <= have:
        raise StoreError("delta: records table has the wrong columns")
    ok = db.execute("PRAGMA d.quick_check").fetchone()[0]
    if ok != "ok":
        raise StoreError(f"delta: integrity check failed ({ok})")


# --------------------------------------------------------------------------- state files
def _load(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def merge_state(target: Path, shard_dirs: list[Path]) -> dict:
    """Merge progress.json, channels.json and blocked.json from shard state dirs into `target`."""
    target.mkdir(parents=True, exist_ok=True)
    rules = {
        "progress.json": lambda a, b: b if (b.get("last_run") or "") >= (a.get("last_run") or "") else a,
        "channels.json": lambda a, b: b if (b.get("checked_at") or "") >= (a.get("checked_at") or "") else a,
        "blocked.json": lambda a, b: {
            **(b if (b.get("last_seen") or "") >= (a.get("last_seen") or "") else a),
            "count": max(a.get("count", 0), b.get("count", 0)),
            "first_seen": min(a.get("first_seen") or "9", b.get("first_seen") or "9"),
            "sources": sorted(set(a.get("sources", [])) | set(b.get("sources", []))),
            "urls": list(dict.fromkeys(a.get("urls", []) + b.get("urls", [])))[-20:]},
    }
    counts = {}
    for name, pick in rules.items():
        merged = _load(target / name)
        for d in shard_dirs:
            for k, v in _load(Path(d) / name).items():
                merged[k] = pick(merged[k], v) if k in merged else v
        if merged:
            (target / name).write_text(json.dumps(merged, indent=1))
        counts[name] = len(merged)
    return counts
