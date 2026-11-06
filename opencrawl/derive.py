"""`opencrawl derive`: turn crawled pages and documents into structured records.

Each deriver looks at records changed since its last run (a cursor in the kv
table), parses the ones it understands, and writes derived records whose
`meta.derived_from` is the source record's id. Re-running is safe: derived
records have natural keys, so a re-parse updates rather than duplicates, and a
source record that changes gets re-derived.

Which derivers run comes from settings.yaml `derivers:` — a list of module paths, each of which calls
`register(Deriver(...))` when imported. Unset = the built-in civic set (DEFAULT_DERIVERS, in run order):
  elections    city/county results pages and PDFs -> election_result; qualifying lists -> qualifying
  minutes      council minutes -> agenda_item + vote (motions, movers, roll calls)
  ballot       referendum notices and sample ballots -> ballot_question
  press        press releases -> appointment
  guides       voter guides, questionnaires, endorsement pages -> questionnaire, endorsement
Write your own the same way (see docs/extending.md) and list it: `derivers: [opencrawl.civic.press, myproject.derive]`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .store import Record, Store


@dataclass
class Deriver:
    name: str
    where: str                         # SQL filter on source records (no 'subj:' sources unless stated)
    fn: Callable[[dict, dict], Iterable[Record]]   # (source record, context) -> derived records
    params: tuple = ()
    needs: dict = field(default_factory=dict)      # context keys the deriver wants (e.g. targets)


REGISTRY: list[Deriver] = []
DEFAULT_DERIVERS = ["opencrawl.civic.elections", "opencrawl.civic.minutes", "opencrawl.civic.ballot",
                    "opencrawl.civic.press", "opencrawl.civic.guides"]


def register(d: Deriver):
    import sys

    d.module = getattr(d, "module", None) or sys._getframe(1).f_globals.get("__name__", "")
    if all(x.name != d.name for x in REGISTRY):
        REGISTRY.append(d)
    return d


def load(modules: list[str] | None = None) -> list[str]:
    """Import the deriver modules (each registers itself); None = DEFAULT_DERIVERS. Returns the module list."""
    import importlib

    mods = list(DEFAULT_DERIVERS if modules is None else modules)
    for mod in mods:
        importlib.import_module(mod)
    return mods


def run(store: Store, only: set[str] | None = None, context: dict | None = None, deadline: float | None = None,
        log=print, full: bool = False) -> dict:
    """Run derivers over changed records. `full` ignores cursors (re-derive everything). Which modules run comes
    from context["cfg"]["derivers"] when given, else the default civic set."""
    context = context or {}
    mods = set(load((context.get("cfg") or {}).get("derivers")))
    out = {}
    for d in REGISTRY:
        if only and d.name not in only:
            continue
        if getattr(d, "module", "") not in mods:
            continue   # registered by an import, but not in this project's deriver list
        cursor = None if full else store.kv_get(f"derive:{d.name}")
        started = store_now()
        seen = made = 0
        for src in store.changed_since(cursor, d.where, d.params):
            if deadline and time.monotonic() > deadline:
                break
            seen += 1
            try:
                for rec in d.fn(src, context) or ():
                    rec.target = rec.target or src.get("target", "")
                    rec.evidence = rec.evidence or src.get("evidence") or "primary"
                    rec.meta.setdefault("derived_from", src["id"])
                    rec.meta.setdefault("derived_url", src["url"])
                    made += store.add(rec)
            except Exception as exc:  # noqa: BLE001 — one bad document must not stop the run
                log(f"  {d.name}: {src['id']} {type(exc).__name__}: {exc}"[:300])
        else:
            store.kv_set(f"derive:{d.name}", started)  # only advance the cursor when the pass completed
        store.flush()
        out[d.name] = {"scanned": seen, "derived": made}
        log(f"{d.name}: {seen} scanned, {made} new/changed")
    return out


def store_now() -> str:
    from .store import utcnow

    return utcnow()
