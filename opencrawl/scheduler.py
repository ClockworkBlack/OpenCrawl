"""Runs hundreds of sources politely inside a time budget.

- Each source has a cadence from its target's tier (a: daily, b: weekly,
  c: monthly by default; `cadence_days` in settings.yaml). A source is *due*
  when its last successful run is older than its cadence (or it has never run).
- Due sources are ordered most-overdue first, tier a before b before c.
- `workers` threads run sources concurrently; the per-host rate limiter keeps
  any single site at one request per interval no matter how many workers.
- Progress (state/progress.json) survives between runs, so a run that hits its
  time budget simply resumes where it left off next time.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import channels, topics, transcripts  # noqa: F401 — transcripts registers its page hook
from .blocked import Blocked
from .fetch import Disallowed, Fetcher, OutOfTime
from .polite import Schedule
from .store import Store
from .targets import TIER_CADENCE_DAYS

# record kinds that get topic tags from config/topics.yaml (settings.yaml crawl.tag_kinds overrides; [] = none)
DEFAULT_TAG_KINDS = {"page", "document", "post", "feed_item", "article", "press_release", "statement", "transcript",
                     "ordinance", "matter", "agenda_item", "vote", "meeting"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Progress:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}
        self._lock = threading.Lock()

    def get(self, source_id: str) -> dict:
        return self.data.get(source_id, {})

    def update(self, source_id: str, **fields):
        from . import redact

        fields = {k: redact.text(v) if isinstance(v, str) and k in ("status", "error", "note") else v
                  for k, v in fields.items()}
        with self._lock:
            rec = self.data.setdefault(source_id, {})
            rec.update(fields)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.replace(self.path)


def due(sources: list[dict], progress: Progress, cadence_days: dict | None = None, now: dt.datetime | None = None,
        force: bool = False) -> list[dict]:
    """Sources that should run now, most overdue first."""
    cadence_days = {**TIER_CADENCE_DAYS, **(cadence_days or {})}
    now = now or _now()
    scored = []
    for s in sources:
        p = progress.get(s["id"])
        cad = dt.timedelta(days=float(cadence_days.get(s.get("tier", "b"), 7)))
        last_ok = dt.datetime.fromisoformat(p["last_ok"]) if p.get("last_ok") else None
        last_try = dt.datetime.fromisoformat(p["last_run"]) if p.get("last_run") else None
        if not force:
            if last_ok and now - last_ok < cad:
                continue
            # a source that keeps failing backs off: wait a quarter cadence (at least 6h) between retries
            if last_try and not last_ok and p.get("status", "").startswith(("error", "blocked", "stopped")) \
                    and now - last_try < max(dt.timedelta(hours=6), cad / 4):
                continue
        overdue = (now - last_ok) / cad if last_ok else 99.0
        scored.append((-overdue, {"a": 0, "b": 1, "c": 2}.get(s.get("tier", "b"), 1), s["id"], s))
    scored.sort(key=lambda t: t[:3])
    return [t[3] for t in scored]


def run_sources(cfg: dict, sources: list[dict], fetcher: Fetcher, store: Store, cache: channels.ChannelCache,
                progress: Progress, deadline: float, force: bool = False, workers: int = 4, log=print,
                full: bool = False) -> dict:
    """Crawl `sources` concurrently inside the time budget.

    force  ignore the off-peak window (cadence is handled by `due`)
    full   re-crawl from the beginning of history instead of a delta (also restarts interrupted backfills)
    """
    sch = Schedule.from_config(cfg.get("schedule", {}))
    api_anytime = cfg.get("schedule", {}).get("api_anytime", True)
    limits = cfg.get("limits", {})
    fr = cfg.get("frontier", {})
    crawl_cfg = cfg.get("crawl") or {}
    channels.configure(crawl_cfg)
    tag_kinds = set(crawl_cfg["tag_kinds"]) if crawl_cfg.get("tag_kinds") is not None else DEFAULT_TAG_KINDS
    summary: dict[str, str] = {}
    stop = threading.Event()
    fetcher.set_pool(max(16, workers * 2))  # one hot keep-alive connection per concurrent worker, with headroom
    fetcher.stop_at = deadline              # a source mid-way stops at its next request once the budget is spent

    def pages_ok() -> bool:
        return force or sch.is_good_time()

    def may_run(structured: bool) -> bool:
        return pages_ok() or (structured and api_anytime)

    def choose(src: dict) -> tuple[dict, list]:
        hit = cache.get(src["id"])
        if hit:
            return hit["chosen"], hit.get("probes", [])
        if src.get("channel"):
            return {"channel": src["channel"], "endpoint": src.get("endpoint", src["url"])}, []
        probes = channels.probe_source(fetcher, src)
        chosen = channels.choose(probes)
        if not chosen:
            raise RuntimeError("no working channel: " + ("; ".join(f"{p.channel}: {p.note}" for p in probes if p.note)
                                                         or "nothing answered"))
        cache.put(src["id"], chosen, probes)
        hit = cache.get(src["id"])
        return hit["chosen"], hit["probes"]

    def one(src: dict) -> tuple[str, str]:
        sid = src["id"]
        if stop.is_set() or time.monotonic() > deadline:
            return sid, "skipped: run time budget used up"
        fetcher.source_id = sid
        started = _now().isoformat(timespec="seconds")
        # Delta since the last *successful* run; None (full crawl) on the first run or with --full.
        # last_ok is stamped with this run's start, so records changed mid-crawl are caught next time.
        modified_since = None if full else (progress.get(sid).get("last_ok") or None)
        if full:
            store.clear_cursors(sid)
        progress.update(sid, last_run=started, status="running")
        try:
            chosen, probes = choose(src)
        except OutOfTime:
            progress.update(sid, status="skipped: run time budget used up")
            return sid, "skipped: run time budget used up"
        except Blocked as exc:
            fetcher.blocklist.note_source(exc.host, sid)
            progress.update(sid, status=f"blocked: {exc}")
            return sid, f"blocked: {exc}"
        except Exception as exc:  # noqa: BLE001
            progress.update(sid, status=f"error choosing channel: {exc}"[:300])
            return sid, f"error choosing channel: {exc}"[:300]
        structured = chosen["channel"] in channels.STRUCTURED
        if not may_run(structured):
            progress.update(sid, status="waiting for off-peak window")
            return sid, f"waiting for off-peak window ({chosen['channel']})"
        ctx = channels.CrawlContext(frontier=store, cursors=store.cursors(sid), pages_ok=pages_ok(), probes=probes,
                                    leaf_ttl_days=float(fr.get("document_ttl_days", 3650)),
                                    page_revisit_days=float(fr.get("page_revisit_days", 7)))
        new = total = 0
        mode = "delta" if modified_since else ("resume" if ctx.cursors else "full")
        try:
            for item in channels.crawl(fetcher, src, chosen, limits, modified_since=modified_since, ctx=ctx):
                if isinstance(item, channels.Checkpoint):
                    store.set_cursor(sid, item.stream, item.value)
                    continue
                item.target = item.target or src.get("target", "")
                item.evidence = item.evidence or "primary"
                item.source_id = sid  # adapters label records with their client; the source id is the stable owner
                if item.kind in tag_kinds and "topics" not in item.meta:
                    item.meta["topics"] = topics.tag(item.title, (item.text or "")[:4000])
                for k in ("guide_name", "guide_kind", "level"):     # newsroom / guide sources label their records
                    if src.get(k) and k not in item.meta:
                        item.meta[k] = src[k]
                total += 1
                new += store.add(item)
                if stop.is_set() or time.monotonic() > deadline or not may_run(structured):
                    store.flush()
                    progress.update(sid, status=f"partial ({chosen['channel']} {mode}): {new} new / {total} seen",
                                    new=new, seen=total, fetched=ctx.stats["fetched"], skipped=ctx.stats["skipped_fresh"])
                    return sid, f"partial ({chosen['channel']} {mode}): {new} new / {total} seen"
            store.flush()
            if not modified_since:
                store.clear_cursors(sid)  # backfill complete; later runs are deltas
            progress.update(sid, status="ok", last_ok=started, channel=chosen["channel"], mode=mode, new=new, seen=total,
                            fetched=ctx.stats["fetched"], skipped=ctx.stats["skipped_fresh"])
            return sid, f"{chosen['channel']} {mode}: {new} new / {total} seen ({ctx.stats['fetched']} fetched, " \
                        f"{ctx.stats['skipped_fresh']} skipped as unchanged)"
        except OutOfTime:
            store.flush()
            progress.update(sid, status=f"partial ({chosen['channel']} {mode}): {new} new / {total} seen (time budget)",
                            new=new, seen=total)
            return sid, f"partial ({chosen['channel']} {mode}): {new} new / {total} seen (time budget)"
        except Blocked as exc:
            store.flush()
            fetcher.blocklist.note_source(exc.host, sid)
            progress.update(sid, status=f"blocked: {exc}", new=new, seen=total)
            return sid, f"blocked after {total} records: {exc}"
        except Disallowed as exc:
            store.flush()
            progress.update(sid, status=f"stopped: robots.txt disallows {exc}")
            return sid, f"stopped: robots.txt disallows {exc}"
        except Exception as exc:  # noqa: BLE001
            store.flush()
            cache.invalidate(sid)
            progress.update(sid, status=f"error ({chosen['channel']}): {type(exc).__name__}: {exc}"[:300])
            return sid, f"error ({chosen['channel']}): {type(exc).__name__}: {exc}"[:300]

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, s): s for s in sources}
        try:
            for fut in as_completed(futures):
                sid, msg = fut.result()
                summary[sid] = msg
                log(f"{sid}: {msg}")
                if time.monotonic() > deadline:
                    stop.set()
        except KeyboardInterrupt:
            stop.set()
            raise
    store.flush()
    fetcher.stop_at = None
    return summary
