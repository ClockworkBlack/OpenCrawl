"""Erasure, retention and the HTTP cache: personal data is kept only as long as it is useful, and can be removed.

  erase_subject   everything held about one subject: their research records, quarantined claims, verified accounts
                  and posts, research progress, links from screenshots, and their files in the export bundle.
                  Remove them from config/subjects.yaml (or the imported list) too, or the next run researches them
                  again: erase_subject says so.
  expire          retention: settings.yaml `privacy.retention_days` (unset = keep). Subject research, screenshots,
                  quarantined claims and reply excerpts older than that are deleted; so is the HTTP cache.
  prune_cache     state/http_cache/ entries older than N days (all with 0). The cache holds full copies of fetched
                  pages; it only saves bandwidth, so it is safe to empty at any time.

Deleted records are removed for good: secure_delete overwrites their pages, the full-text index is optimized and
the database is vacuumed (Store.compact), so their text is not left in the file or its write-ahead log.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path


def _cutoff(days: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=float(days))).replace(microsecond=0).isoformat()


def _rewrite_json(path: Path, fn) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return 0
    data, n = fn(data)
    if n:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return n


def _drop_jsonl(path: Path, pred) -> int:
    if not path.exists():
        return 0
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    keep = [r for r in rows if not pred(r)]
    if len(keep) != len(rows):
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep), encoding="utf-8")
        tmp.replace(path)
    return len(rows) - len(keep)


def erase_subject(root: Path, store, subject_id: str, bundle: Path | None = None, compact: bool = True) -> dict:
    """Remove everything held about one subject. Returns counts per place."""
    from .research.packets import file_name
    from .research.quarantine import Quarantine
    from .targets import safe_id

    if not safe_id(subject_id):
        raise ValueError(f"not a subject id: {subject_id!r}")
    root = Path(root)
    sid = f"subj:{subject_id}"
    out: dict[str, int] = {"records": store.erase("source_id=?", (sid,))}
    # screenshots and other records that name the subject in meta.subjects: drop the link, keep the item
    n_links = 0
    for r in store.iter_query("kind='screenshot' AND meta LIKE ?", (f'%"{subject_id}"%',)):
        subs = [x for x in (r["meta"].get("subjects") or []) if x != subject_id]
        if subs != (r["meta"].get("subjects") or []):
            with store._lock:
                store.db.execute("UPDATE records SET meta=json_set(meta, '$.subjects', json(?)) WHERE id=?",
                                 (json.dumps(subs), r["id"]))
            n_links += 1
    store.flush()
    out["screenshot_links"] = n_links
    out["quarantined"] = Quarantine(root).drop(lambda r: r.get("subject_id") == subject_id)
    reg = root / "data" / "verified"
    out["verified_accounts"] = _drop_jsonl(reg / "accounts.jsonl", lambda r: r.get("subject_id") == subject_id)
    out["verified_posts"] = _drop_jsonl(reg / "posts.jsonl", lambda r: r.get("subject_id") == subject_id)

    def drop_key(d):
        if isinstance(d, dict) and sid in d:
            d.pop(sid)
            return d, 1
        return d, 0
    out["progress"] = _rewrite_json(root / "state" / "subject_progress.json", drop_key)
    raw = store.kv_get("profile_inputs")
    if raw:
        d = json.loads(raw)
        if d.pop(subject_id, None) is not None:
            store.kv_set("profile_inputs", json.dumps(d, separators=(",", ":")))
    files = 0
    for b in [bundle or root / "dist" / "bundle"]:
        name = file_name({"id": subject_id})
        for rel in (f"subjects/{subject_id}.json", f"packets/{name}.json", f"profiles/{name}.json"):
            p = Path(b) / rel
            if p.exists():
                p.unlink()
                files += 1
    out["export_files"] = files
    if compact:
        out.update(store.compact())
    out["note"] = ("remove the subject from config/subjects.yaml (and re-import without them) or the next research run "
                   "collects them again; rerun `opencrawl export` so indexes no longer list them")
    return out


def prune_cache(state_dir: Path, older_than_days: float = 0) -> dict:
    """Delete HTTP cache entries (full page copies) not written for `older_than_days` days (0 = all)."""
    cache = Path(state_dir) / "http_cache"
    if not cache.exists():
        return {"removed": 0, "bytes": 0}
    limit = time.time() - float(older_than_days) * 86400
    n = size = 0
    for p in cache.iterdir():
        if p.is_file() and (older_than_days <= 0 or p.stat().st_mtime < limit):
            size += p.stat().st_size
            p.unlink()
            n += 1
    return {"removed": n, "bytes": size}


def expire(root: Path, cfg: dict, store, days: float | None = None, compact: bool = True) -> dict:
    """Retention: delete personal-data-bearing material older than `days` (privacy.retention_days)."""
    from .research.quarantine import Quarantine

    days = days if days is not None else ((cfg or {}).get("privacy") or {}).get("retention_days")
    if not days:
        return {"skipped": "privacy.retention_days is not set (nothing expires)"}
    cut = _cutoff(days)
    root = Path(root)
    out = {"cutoff": cut,
           "subject_records": store.erase("source_id LIKE 'subj:%' AND fetched_at < ?", (cut,)),
           "screenshots": store.erase("kind='screenshot' AND fetched_at < ?", (cut,)),
           "quarantined": Quarantine(root).drop(lambda r: (r.get("first_seen") or "") < cut)}

    def scrub_replies(d):
        n = 0
        for t in (d or {}).values():
            for rep in t.get("replies") or []:
                if (rep.get("at") or "") < cut and rep.get("excerpt"):
                    rep["excerpt"] = ""
                    n += 1
        return d, n
    out["reply_excerpts"] = _rewrite_json(root / "data" / "openrecords" / "requests.json", scrub_replies)
    out["http_cache"] = prune_cache(Path((cfg or {}).get("state_dir") or root / "state"), days)["removed"]
    if compact:
        out.update(store.compact())
    return out
