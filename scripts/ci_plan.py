#!/usr/bin/env python3
"""Decide what a crawl workflow run does, from the event and the repository variables, and write it to
$GITHUB_OUTPUT. Every value from outside (workflow inputs, repository variables) is validated here, so later steps
only ever see these checked outputs. Standard library and opencrawl.backfill only (nothing installed yet).

Environment (set by .github/workflows/crawl.yml from github.* and vars.*; never interpolated into a script):
  EVENT, SCHEDULE, NIGHTLY_CRON, WEEKLY_CRON, BACKFILL_CRON
  IN_JOB, IN_SOURCE, IN_TIER, IN_FORCE, IN_FULL, IN_RESEED
  CRAWL_SHARDS, CRAWL_MINUTES, SEED_MINUTES, RESEARCH_MINUTES, RESEARCH_NIGHTLY_MINUTES, FINANCE_MINUTES,
  DERIVE_MINUTES, TRANSCRIPT_MINUTES, SPONSOR_MINUTES, YOUTUBE_MINUTES
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

JOBS = ("", "crawl", "seed", "subjects", "all", "export-only", "backfill")
SOURCE_RX = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,99}$")


class PlanError(ValueError):
    pass


def _int(env, name: str, default: int, lo: int, hi: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not (lo <= int(raw) <= hi):
        raise PlanError(f"{name} must be a whole number from {lo} to {hi} (got {raw[:20]!r})")
    return int(raw)


def _bool(env, name: str) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if raw not in ("", "true", "false"):
        raise PlanError(f"{name} must be true or false")
    return raw == "true"


def plan(env=None, root: Path | None = None, now: dt.datetime | None = None) -> dict:
    from opencrawl import backfill

    env = os.environ if env is None else env
    job = (env.get("IN_JOB") or "").strip()
    if job not in JOBS:
        raise PlanError(f"unknown job {job[:30]!r}")
    source = (env.get("IN_SOURCE") or "").strip()
    if source and not SOURCE_RX.match(source):
        raise PlanError("source must be a target or source id (lower-case letters, digits, . _ : -)")
    tier = (env.get("IN_TIER") or "").strip().lower()
    if tier not in ("", "a", "b", "c"):
        raise PlanError("tier must be a, b or c")
    schedule = (env.get("SCHEDULE") or "").strip()
    weekly = bool(schedule) and schedule == env.get("WEEKLY_CRON")
    nightly = bool(schedule) and schedule == env.get("NIGHTLY_CRON")
    backfill_run = job == "backfill" or (bool(schedule) and schedule == env.get("BACKFILL_CRON"))

    n = _int(env, "CRAWL_SHARDS", 2, 1, 20)
    minutes = _int(env, "CRAWL_MINUTES", 18, 1, 300)
    bf = backfill.plan(root or backfill.ROOT, now)
    crawl = job in ("", "crawl", "all")
    research = weekly or job in ("subjects", "all")
    skip = False
    if backfill_run:
        if bf["go"]:
            crawl, research, n, minutes = True, True, 1, int(bf["crawl_minutes"])
        else:
            crawl, skip = False, True
    elif nightly and bf["active"]:
        n, minutes = int(bf["night_shards"]), int(bf["night_minutes"])   # more page crawling while a backfill runs
    if job == "export-only":
        crawl = research = False
    research_minutes = (int(bf["research_minutes"]) if backfill_run else
                        _int(env, "RESEARCH_MINUTES", 90, 1, 300) if research and not weekly else
                        _int(env, "RESEARCH_NIGHTLY_MINUTES", 13, 0, 300))
    return {
        "n": n, "minutes": minutes, "shards": json.dumps(list(range(n))),
        "started": (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "crawl": crawl, "research": research, "skip": skip, "weekly": weekly, "backfill": backfill_run,
        "seed": weekly or job in ("seed", "all"), "reseed": _bool(env, "IN_RESEED"),
        "source": source, "tier": tier, "force": _bool(env, "IN_FORCE"), "full": _bool(env, "IN_FULL"),
        "seed_minutes": _int(env, "SEED_MINUTES", 20, 1, 300), "research_minutes": research_minutes,
        "finance_minutes": int(bf["finance_minutes"]) if backfill_run else _int(env, "FINANCE_MINUTES", 15, 1, 300),
        "derive_minutes": _int(env, "DERIVE_MINUTES", 5, 1, 120), "transcript_minutes": _int(env, "TRANSCRIPT_MINUTES", 4, 0, 120),
        "sponsor_minutes": _int(env, "SPONSOR_MINUTES", 3, 0, 120), "youtube_minutes": _int(env, "YOUTUBE_MINUTES", 10, 0, 120),
        "backfill_why": str(bf["why"])[:200],
    }


def main() -> int:
    try:
        out = plan()
    except PlanError as exc:
        print(f"invalid run settings: {exc}", file=sys.stderr)
        return 1
    lines = [f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in out.items()]
    for ln in lines:
        if "\n" in ln or "\r" in ln:
            raise SystemExit("refusing a multi-line output")
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
