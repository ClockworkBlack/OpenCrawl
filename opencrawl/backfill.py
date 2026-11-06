"""Daytime backfill: back-to-back CI runs until subject profiles stop filling, then back to the nightly cadence.

config/backfill.json   the switch and the limits (edit `enabled` to stop or restart it)
state/backfill.json    what has happened (committed with the rest of state): Actions minutes per month (every run),
                       backfill runs, the last runs' research yield, done

A backfill run is a normal run with one crawl shard (by day only API sources are due: HTML page crawling keeps to
the off-peak windows in settings.yaml), a long subject-research block and finance. At its end it dispatches the
next one while it is inside the day window, under the month's minute cap, and not done. Done means every subject
has been researched at least once and `quiet_runs` runs in a row each added fewer than `min_added` records.
While backfill is active the nightly run crawls with more shards (`night_shards`, `night_minutes`) so page
crawling (minutes, documents) fills faster inside the off-peak window.

`python -m opencrawl.backfill plan` needs only the standard library (the workflow's prepare job runs it before
installing anything); `opencrawl backfill next` runs in the merge job.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ROOT = Path(os.environ.get("OPENCRAWL_ROOT") or (Path.cwd() if (Path.cwd() / "config").exists() else Path(__file__).resolve().parent.parent))
DEFAULTS = {
    "enabled": False, "window": {"start": "06:00", "end": "21:30", "timezone": "America/New_York"},
    "research_minutes": 100, "finance_minutes": 20, "crawl_minutes": 18,
    "night_shards": 3, "night_minutes": 40,
    "stop": {"never_researched": 0, "quiet_runs": 2, "min_added": 60},
    "month_minutes_cap": 6000,
}


def load_cfg(root: Path = ROOT) -> dict:
    p = Path(root) / "config" / "backfill.json"
    cfg = json.loads(json.dumps(DEFAULTS))
    if p.exists():
        d = json.loads(p.read_text())
        for k, v in d.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            elif not k.startswith("_"):
                cfg[k] = v
    return cfg


def load_state(root: Path = ROOT) -> dict:
    p = Path(root) / "state" / "backfill.json"
    return json.loads(p.read_text()) if p.exists() else {"months": {}, "runs": 0, "quiet": 0, "done": False, "history": []}


def save_state(st: dict, root: Path = ROOT) -> None:
    p = Path(root) / "state" / "backfill.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    st["history"] = st.get("history", [])[-40:]
    p.write_text(json.dumps(st, indent=1) + "\n")


def _now(cfg, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    tz = ZoneInfo(cfg["window"]["timezone"]) if ZoneInfo else dt.timezone.utc
    return now.astimezone(tz)


def in_window(cfg, now=None) -> bool:
    t = _now(cfg, now).strftime("%H:%M")
    return cfg["window"]["start"] <= t < cfg["window"]["end"]


def month_minutes(st, now=None, cfg=None) -> int:
    """Minutes counted this month, plus any the config says were used before counting began."""
    m = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m")
    return int((st.get("months") or {}).get(m, 0)) + int(((cfg or {}).get("month_minutes_before") or {}).get(m, 0))


def active(cfg, st, now=None) -> tuple[bool, str]:
    if not cfg.get("enabled"):
        return False, "backfill is switched off (config/backfill.json)"
    if st.get("done"):
        return False, "backfill finished: " + (st.get("done_reason") or "profiles stopped filling")
    used, cap = month_minutes(st, now, cfg), int(cfg["month_minutes_cap"])
    if used >= cap:
        return False, f"month's Actions minutes reached the cap ({used} of {cap})"
    return True, f"active ({used} of {cap} minutes this month)"


def plan(root: Path = ROOT, now=None) -> dict:
    cfg, st = load_cfg(root), load_state(root)
    ok, why = active(cfg, st, now)
    return {"active": ok, "go": ok and in_window(cfg, now), "why": why if ok and in_window(cfg, now) or not ok else "outside the day window",
            "research_minutes": cfg["research_minutes"], "finance_minutes": cfg["finance_minutes"], "crawl_minutes": cfg["crawl_minutes"],
            "night_shards": cfg["night_shards"], "night_minutes": cfg["night_minutes"]}


def next_run(root: Path, minutes: int, backfill_run: bool, research: dict | None, never_researched: int | None,
             run_id: str = "", now=None) -> dict:
    """Record this run's minutes (every run), and for a backfill run its yield; decide whether to dispatch the next."""
    cfg, st = load_cfg(root), load_state(root)
    now = now or dt.datetime.now(dt.timezone.utc)
    if run_id and run_id == st.get("last_run"):
        return {"go": False, "why": "this run was already counted"}
    st["last_run"] = run_id
    m = now.strftime("%Y-%m")
    st.setdefault("months", {})[m] = int(st["months"].get(m, 0)) + int(minutes)
    out = {"minutes": minutes, "month_minutes": month_minutes(st, now, cfg)}
    if backfill_run:
        added = sum((research or {}).get("added", {}).values()) if research else 0
        st["runs"] = int(st.get("runs", 0)) + 1
        stop = cfg["stop"]
        st["quiet"] = int(st.get("quiet", 0)) + 1 if added < int(stop["min_added"]) else 0
        st.setdefault("history", []).append({"at": now.isoformat(timespec="minutes"), "minutes": minutes, "added": added,
                                             "researched": (research or {}).get("researched"), "never_researched": never_researched})
        if never_researched is not None and never_researched <= int(stop["never_researched"]) and st["quiet"] >= int(stop["quiet_runs"]):
            st["done"] = True
            st["done_reason"] = (f"every subject researched, and the last {st['quiet']} runs each added fewer than "
                                 f"{stop['min_added']} records ({now.date().isoformat()})")
        out.update(added=added, never_researched=never_researched, quiet=st["quiet"])
    ok, why = active(cfg, st, now)
    go = backfill_run and ok and in_window(cfg, now)
    out.update(go=go, why=why if (go or not ok) else ("outside the day window" if backfill_run else "not a backfill run"),
               done=bool(st.get("done")))
    save_state(st, root)
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["plan"]:
        p = plan(Path(os.environ.get("OPENCRAWL_ROOT", ROOT)))
        import shlex

        for k, v in p.items():   # shell-quoted: the workflow evals these lines
            print(f"{k}={shlex.quote(str(v).lower() if isinstance(v, bool) else str(v))}")
    else:
        sys.exit("usage: python -m opencrawl.backfill plan")
