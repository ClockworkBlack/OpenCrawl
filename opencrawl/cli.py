"""opencrawl — command line entry point.

Project
  opencrawl init [DIR] [--example NAME]  create a project (config/, data/, state/, imports/) from the starter or an example
  opencrawl status                       targets, discovery coverage, crawl progress, database size
  opencrawl window                       is now a good time to crawl? when's the next one?

Discover and crawl
  opencrawl seed [--only ID] [--refresh] [--max-age-days D] [--max-minutes M]
                                         find every target's website, portals, feeds (state/discovered.json)
  opencrawl targets [--sources]          list targets (or the crawl sources they expand into)
  opencrawl discover [--source ID]       probe API/RSS/sitemap/HTML channels for a source and pick the fastest
  opencrawl run [--source ID] [--tier a|b|c] [--kind K] [--shard i/N] [--force] [--full] [--workers N] [--max-minutes M]
  opencrawl delta --since ISO --out FILE copy what changed since ISO into a small database (for parallel runs)
  opencrawl merge DELTA.db... [--state DIR...]   apply deltas (and shard state) to the main database
  opencrawl search QUERY [--target ID] [--kind K] [--limit N]   full-text search of everything crawled
  opencrawl derive [--only NAME,...] [--full] [--max-minutes M]   parse crawled pages/documents into structured records
  opencrawl transcripts [--max-minutes M]  captions / transcripts for records that carry a video link
  opencrawl sponsors [--max-minutes M]   Legistar sponsor lists for stored ordinances/resolutions
  opencrawl tasks                        write data/manual_tasks.md (blocked hosts, captures, records requests)
  opencrawl blocked [--clear HOST]       list hosts that refused the crawler

Screenshots and documents
  opencrawl ocr FILE... [--url U] [--source ID] [--kind K]     screenshots, scans and PDFs to text
  opencrawl capture URL                  headless screenshot + OCR (robots-allowed pages only)
  opencrawl screens ingest [FILES...] [--subject ID] [--note TEXT]   read imports/screens/
  opencrawl screens capture URL [--device phone|desktop] [--selector CSS] [--source ID] [--kind K]
  opencrawl screens simulate [--out DIR] / screens box [--box NAME]

Subjects (research module)
  opencrawl subjects list|research|import [--subject ID] [--max-minutes M] [--since DATE] [--skip step,step] [FILE]
  opencrawl posts account-add|verify|mark|add|pull|list|export   verified social accounts and posts
  opencrawl quarantine list|add|corroborate|debunk|retract   unverified claims, kept apart from the records
  opencrawl finance [run|probe] [--subject ID] [--max-minutes M] [--org-limit N]
  opencrawl profiles [--subject ID] [--out DIR]   profiles (positions, markers, support, career, money)
  opencrawl packets [--out DIR]          research packets only

Civic module
  opencrawl records status|plan|contacts|send|check|follow-up [--limit N] [--dry-run] [--max-minutes M]
                                         public records requests for codes of ordinances, from the project mailbox

Export and scheduling
  opencrawl export [--out DIR] [--no-packets]   the JSON bundle (dist/bundle/)
  opencrawl backfill status|next [--minutes N --run-id ID --backfill]   daytime backfill bookkeeping (CI)

Security and privacy
  opencrawl doctor [--browser] [--ci]    check settings, secrets, permissions, library versions
  opencrawl purge --subject ID | --expired [--days N]   erase one subject's data / apply privacy.retention_days
  opencrawl cache prune [--days N]       delete HTTP cache entries (full page copies) older than N days (default all)
  opencrawl vault keygen | encrypt IN OUT | decrypt IN OUT | pack OUT PATH... | unpack IN DEST
                                         AES-256-GCM files for data that leaves the machine (key: OPENCRAWL_DATA_KEY)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

from . import channels, config, targets
from .fetch import Disallowed
from .polite import Schedule
from .store import Store

STARTER = Path(__file__).resolve().parent / "templates" / "starter"      # = config/ of the repository (tested)
EXAMPLES = Path(__file__).resolve().parent / "examples"


# what a new project keeps out of git: the database, the page cache, exports, quarantined claims, replies kept for
# review, decrypted archives
GITIGNORE = """__pycache__/
*.pyc
data/*.db
data/*.db-wal
data/*.db-shm
data/quarantine/
data/openrecords/unverified/
dist/
state/http_cache/
state/*.tmp
*.ocv
.ci-in/
.ci-out/
.ci-state/
.ci-restored
"""


# ----------------------------------------------------------------------------- project
def cmd_init(cfg, args):
    dest = Path(args.dir or ".").resolve()
    src = EXAMPLES / args.example / "config" if args.example else STARTER
    if not src.exists():
        have = sorted(p.name for p in EXAMPLES.iterdir() if (p / "config").exists()) if EXAMPLES.exists() else []
        sys.exit(f"no example {args.example!r}; examples: {', '.join(have) or 'none'}")
    (dest / "config").mkdir(parents=True, exist_ok=True)
    copied = []
    for p in sorted(src.iterdir()):
        if p.is_file() and not (dest / "config" / p.name).exists():
            shutil.copy2(p, dest / "config" / p.name)
            copied.append(p.name)
    if args.example and (EXAMPLES / args.example / "README.md").exists() and not (dest / "README-example.md").exists():
        shutil.copy2(EXAMPLES / args.example / "README.md", dest / "README-example.md")
    for d in ("data", "state", "imports/screens/ordinances", "imports/screens/posts", "imports/screens/text", "imports/finance"):
        (dest / d).mkdir(parents=True, exist_ok=True)
    gi = dest / ".gitignore"
    if not gi.exists():
        gi.write_text(GITIGNORE)
    print(f"project at {dest}: config/ ({', '.join(copied) or 'kept existing files'}), data/, state/, imports/")
    if (dest / "config" / "jurisdictions.yaml").exists():
        print("next: set http.user_agent in config/settings.yaml, then `opencrawl seed --max-minutes 30` and `opencrawl run`")
    else:
        print("next: edit config/targets.yaml, config/sources.yaml and config/settings.yaml, then `opencrawl seed` and `opencrawl run`")


def cmd_window(cfg, args):
    sch = Schedule.from_config(cfg.get("schedule", {}))
    now = dt.datetime.now(dt.timezone.utc)
    good = sch.is_good_time(now)
    print(json.dumps({"good_time": good, "now_local": now.astimezone(sch.tz).isoformat(timespec="minutes"),
                      "next_good_time": None if good else sch.next_good_time(now).isoformat(timespec="minutes")}))


def cmd_status(cfg, args):
    from .scheduler import Progress

    tg = targets.targets(config.ROOT)
    srcs = targets.all_sources(config.ROOT, cfg.get("limits"))
    prog = Progress(cfg["state_dir"] / "progress.json")
    portals = Counter(k for t in tg for k in t.get("portals", {}))
    st = Counter((prog.get(s["id"]).get("status") or "never run").split(":")[0].split(" (")[0] for s in srcs)
    print(json.dumps({
        "targets": len(tg), "by_kind": dict(Counter(t.get("kind", "target") for t in tg)),
        "with_website": sum(1 for t in tg if t.get("website")),
        "seeded": sum(1 for t in tg if t.get("checked_at")),
        "portals_found": dict(portals), "tiers": dict(Counter(t["tier"] for t in tg)),
        "crawl_sources": len(srcs), "source_status": dict(st),
        "blocked_hosts": list(config.make_fetcher(cfg).blocklist.data.keys()),
        "database": _db_stats(cfg),
    }, indent=1))


def _db_stats(cfg) -> dict:
    path = Path(cfg.get("db_path") or Path(cfg["data_dir"]) / "crawl.db")
    if not path.exists():
        return {"path": str(path), "records": 0, "note": "no database yet (created by the first run)"}
    store = config.make_store(cfg)
    try:
        size = store.path.stat().st_size if store.path.exists() else 0
        return {"path": str(store.path), "mb": round(size / 1e6, 1), "records": store.total(),
                "by_kind": store.counts("kind")}
    finally:
        store.close()


# ----------------------------------------------------------------------------- discover and crawl
def cmd_seed(cfg, args):
    from . import seed as generic
    from .civic import seed as civic
    from .civic.registry import CIVIC_KINDS

    civic.configure(cfg.get("civic"))
    fetcher = config.make_fetcher(cfg)
    tg = targets.targets(config.ROOT)
    deadline = time.monotonic() + float(args.max_minutes) * 60 if args.max_minutes else None
    workers = args.workers or cfg.get("workers", 6)
    state_path = cfg["state_dir"] / "discovered.json"
    kw = dict(workers=workers, only=args.only, refresh=args.refresh, max_age_days=args.max_age_days, deadline=deadline)
    govs = [t for t in tg if t.get("kind") in CIVIC_KINDS and t["kind"] != "school_district"]
    districts = [t for t in tg if t.get("kind") == "school_district"]
    others = [t for t in tg if t.get("kind") not in CIVIC_KINDS]
    for group, fn in ((others, generic.seed), (govs, civic.seed_jurisdictions), (districts, civic.seed_school_districts)):
        if group and (not args.only or any(t["id"] == args.only for t in group)):
            fn(fetcher, group, state_path, **kw)
    cmd_status(cfg, args)


def cmd_targets(cfg, args):
    if args.sources:
        for s in targets.all_sources(config.ROOT, cfg.get("limits")):
            print(f"{s['id']:<40} {s.get('kind', 'page'):<14} {s.get('tier', '-'):<3} {s.get('channel') or 'auto':<8} {s['url']}")
        return
    for t in targets.targets(config.ROOT):
        portals = ",".join(t.get("portals", {}).keys()) or "-"
        print(f"{t['id']:<32} {t.get('kind', 'target'):<16} {t['tier']:<3} {t.get('website') or '-':<50} {portals}")


def _sources(cfg, only: str | None, tier: str | None = None, shard: str | None = None):
    from .shard import in_shard, parse_shard

    srcs = targets.all_sources(config.ROOT, cfg.get("limits"))
    sh = parse_shard(shard)
    srcs = [s for s in srcs if in_shard(s, sh)]
    if only:
        srcs = [s for s in srcs if s["id"] == only or s["id"].split(":")[0] == only]
        if not srcs:
            sys.exit(f"no source or target with id {only!r} (run `opencrawl seed` first?)")
    if tier:
        srcs = [s for s in srcs if s.get("tier") == tier]
    return srcs


def cmd_discover(cfg, args):
    channels.configure(cfg.get("crawl"))
    fetcher = config.make_fetcher(cfg)
    cache = channels.ChannelCache(cfg["state_dir"] / "channels.json", cfg.get("recheck_days", 7))
    for src in _sources(cfg, args.source):
        try:
            probes = channels.probe_source(fetcher, src)
            chosen = channels.choose(probes)
            if chosen:
                cache.put(src["id"], chosen, probes)
            print(f"{src['id']}: using {chosen.channel + ' -> ' + chosen.endpoint if chosen else 'NOTHING WORKS'}")
            for p in probes:
                mark = "ok " if p.ok else "-- "
                print(f"   {mark}{p.channel:<8} {p.latency*1000:7.0f} ms  {p.items:>5} items  {p.endpoint} {p.note}")
        except Exception as exc:  # noqa: BLE001
            print(f"{src['id']}: FAILED {exc}")


def cmd_run(cfg, args):
    import os

    from .scheduler import Progress, due, run_sources

    fetcher = config.make_fetcher(cfg)
    cache = channels.ChannelCache(cfg["state_dir"] / "channels.json", cfg.get("recheck_days", 7))
    store = config.make_store(cfg)
    progress = Progress(cfg["state_dir"] / "progress.json")
    deadline = time.monotonic() + float(args.max_minutes or cfg.get("max_minutes", 300)) * 60

    allsrc = _sources(cfg, args.source, args.tier, args.shard)
    if args.kind:
        allsrc = [s for s in allsrc if s.get("kind") == args.kind]
    yt = lambda s: str(s.get("endpoint") or "").startswith("youtube:")  # noqa: E731
    if not os.environ.get("YOUTUBE_API_KEY"):
        allsrc = [s for s in allsrc if not yt(s)]       # kept due until there is a key to list them with
    srcs = due(allsrc, progress, cfg.get("cadence_days"), force=bool(args.force or args.full or args.source))
    if os.environ.get("YOUTUBE_API_KEY") and not store.kv_get("youtube:keyed"):
        # first run with a key: list every channel once, even ones an earlier keyless run marked as crawled
        have = {s["id"] for s in srcs}
        srcs += [s for s in allsrc if yt(s) and s["id"] not in have]
        store.kv_set("youtube:keyed", time.strftime("%Y-%m-%d"))
    print(f"{len(srcs)} sources due" + (f" (shard {args.shard})" if args.shard else ""))
    try:
        summary = run_sources(cfg, srcs, fetcher, store, cache, progress, deadline, force=args.force,
                              workers=args.workers or cfg.get("workers", 4), full=args.full)
    finally:
        store.close()
    tally = Counter(v.split(":")[0].split(" (")[0] for v in summary.values())
    print(json.dumps({"ran": len(summary), "outcomes": dict(tally)}, indent=1))


def cmd_delta(cfg, args):
    from .shard import write_delta

    store = config.make_store(cfg)
    try:
        print(json.dumps(write_delta(store, args.since, Path(args.out)), indent=1))
    finally:
        store.close()


def cmd_merge(cfg, args):
    from .shard import merge_delta, merge_state

    store = config.make_store(cfg)
    try:
        for d in args.deltas:
            print(json.dumps(merge_delta(store, Path(d))))
    finally:
        store.close()
    if args.state:
        print(json.dumps(merge_state(cfg["state_dir"], [Path(x) for x in args.state])))


def cmd_search(cfg, args):
    store = config.make_store(cfg)
    try:
        where, params = "1", ()
        if args.target:
            where, params = "r.target=?", (args.target,)
        if args.kind:
            where += " AND r.kind=?"
            params += (args.kind,)
        match = args.query if any(op in args.query for op in ('"', " OR ", " AND ", "NEAR(")) else Store.fts_phrase(args.query)
        for r in store.search(match, where, params, limit=args.limit):
            print(f"{(r.get('published') or '')[:10]:<10} {r['kind']:<12} {r['target']:<22} {r['title'][:70]}")
            print(f"           {r['url']}")
            print(f"           {r['snippet'][:220]}")
    finally:
        store.close()


def cmd_tasks(cfg, args):
    from .blocked import BlockList, manual_tasks_md
    from .civic.openrecords import law_settings

    md = manual_tasks_md(BlockList(cfg["state_dir"] / "blocked.json"), targets.targets(config.ROOT),
                         requester=(cfg.get("openrecords") or {}).get("requester") or cfg.get("requester") or "[your name / organization]",
                         law=law_settings(cfg))
    out = cfg["data_dir"] / "manual_tasks.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"wrote {out} ({md.count(chr(10))} lines)")


def cmd_blocked(cfg, args):
    from .blocked import BlockList

    bl = BlockList(cfg["state_dir"] / "blocked.json")
    if args.clear:
        bl.clear(args.clear)
        print(f"cleared {args.clear}")
        return
    for host, rec in sorted(bl.data.items()):
        print(f"{host}: {rec['reason']} (x{rec['count']}, last {rec['last_seen'][:10]}) sources={len(rec.get('sources', []))}")
        for a in rec.get("alternatives", []):
            print(f"   -> {a}")


def _civic(cfg) -> None:
    """Project settings the civic modules read (state, ballot-question kinds)."""
    from .civic import elections, seed as civic_seed

    elections.configure(cfg.get("civic"))
    civic_seed.configure(cfg.get("civic"))


def cmd_derive(cfg, args):
    from . import derive

    _civic(cfg)
    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes) * 60 if args.max_minutes else None
        only = set(args.only.split(",")) if args.only else None
        print(json.dumps(derive.run(store, only, {"targets": targets.targets(config.ROOT), "root": config.ROOT, "cfg": cfg},
                                    deadline, full=args.full), indent=1))
    finally:
        store.close()


def cmd_transcripts(cfg, args):
    from . import transcripts

    store = config.make_store(cfg)
    fetcher = config.make_fetcher(cfg)
    deadline = time.monotonic() + float(args.max_minutes or 20) * 60
    try:
        out = {"meetings": transcripts.from_meetings(fetcher, store, limit=int(args.limit or 60), deadline=deadline)}
        out["recordings"] = transcripts.transcribe_recordings(fetcher, store, cfg, deadline)
        print(json.dumps(out, indent=1))
    finally:
        store.close()


def cmd_sponsors(cfg, args):
    from .adapters.legistar import fill_sponsors

    store = config.make_store(cfg)
    try:
        deadline = time.monotonic() + float(args.max_minutes or 20) * 60
        from .adapters.legistar import env_tokens

        tokens = env_tokens()
        print(json.dumps(fill_sponsors(config.make_fetcher(cfg), store, limit=int(args.limit or 2000), deadline=deadline,
                                       tokens=tokens), indent=1))
    finally:
        store.close()


# ----------------------------------------------------------------------------- screenshots and documents
def cmd_ocr(cfg, args):
    from .ingest import image_to_record, pdf_to_record

    store = config.make_store(cfg)
    for f in args.files:
        p = Path(f)
        fn = pdf_to_record if p.suffix.lower() == ".pdf" else image_to_record
        rec = fn(p, source_id=args.source, kind=args.kind, url=args.url or "")
        rec.target = args.source.split(":")[0]
        rec.evidence = "primary"
        store.add(rec)
        out = p.with_suffix(p.suffix + ".md")
        out.write_text(rec.markdown, encoding="utf-8")
        conf = rec.meta.get("mean_confidence")
        print(f"{p.name}: {len(rec.text)} chars, confidence {conf}, review {len(rec.meta.get('needs_review', []))} lines -> {out}")
    store.close()


def cmd_capture(cfg, args):
    args.action, args.files, args.device, args.selector, args.kind = "capture", [args.url], "desktop", None, "page"
    cmd_screens(cfg, args)


def cmd_screens(cfg, args):
    from . import screens
    from .blocked import Blocked

    screens.configure(cfg.get("civic"))
    if args.action == "simulate":
        out = Path(args.out or (config.ROOT / "simulated_screens"))  # never the real inbox by default
        for f in screens.simulate(out):
            print(f)
        return
    store = config.make_store(cfg)
    try:
        if args.action == "ingest":
            from .research.subjects import load_subjects

            inbox = screens.Inbox(config.ROOT, store, load_subjects(config.ROOT, cfg), targets=targets.targets(config.ROOT))
            files = [Path(f) for f in args.files] if args.files else None
            print(json.dumps(inbox.ingest(files, subject_id=args.subject, note=args.note or "",
                                          move=files is None), indent=1))
        elif args.action == "capture":
            for url in args.files:
                try:
                    rec = screens.capture_record(url, config.make_fetcher(cfg), source_id=args.source or "capture",
                                                 device=args.device, selector=args.selector, kind=args.kind,
                                                 save_to=cfg["data_dir"] / "screens")
                except (Disallowed, Blocked) as exc:
                    print(f"{url}: not captured ({type(exc).__name__}: {exc}). This site doesn't allow automated access; "
                          f"take the screenshot yourself and put it in imports/screens/.")
                    continue
                if not rec:
                    print(f"{url}: page did not load")
                    continue
                store.add(rec)
                print(f"{url}: {len(rec.text)} chars from {rec.meta['text_from']}")
        elif args.action == "box":
            box = screens.Inbox.box_export(store)
            for name, items in box.items():
                if args.box and name != args.box:
                    continue
                print(f"== {name} ({len(items)})")
                for it in items[:50]:
                    print(f"  {it['status']:<13} {', '.join(it['targets']) or '-':<22} {it['title'][:70]}")
    finally:
        store.close()


# ----------------------------------------------------------------------------- subjects
def cmd_posts(cfg, args):
    from .verified_posts import Registry

    reg = Registry(cfg["data_dir"] / "verified")
    if args.action == "account-add":
        row = reg.add_account(args.subject, args.profile, args.proof or "", args.office or "", args.target or "")
        if args.subject_id:
            reg.mark_subject(row["id"], args.subject_id)
        print(json.dumps(row, indent=2))
    elif args.action == "verify":
        for r in reg.verify_accounts(config.make_fetcher(cfg)):
            print(f"{r['status']:<11} {r['id']:<40} {r.get('check_note','')}")
    elif args.action == "mark":
        reg.mark_account(args.account, args.status, args.note or "")
    elif args.action == "add":
        print(json.dumps(reg.add_post(args.url, args.text or "", args.posted_at, args.account, args.screenshot,
                                      topics=args.topics.split(",") if args.topics else None,
                                      archived_url=args.archived or ""), indent=2, ensure_ascii=False))
    elif args.action == "pull":
        print(f"{reg.pull(config.make_fetcher(cfg), args.since)} posts pulled")
    elif args.action == "list":
        for r in reg.posts():
            print(f"{r['status']:<12} {r.get('subject',''):<24} {r.get('posted_at') or '':<25} {r['post_url']}")
    elif args.action == "export":
        n = reg.export_csv(Path(args.out), only_verified=not args.all)
        print(f"wrote {n} rows to {args.out}")


def cmd_subjects(cfg, args):
    from .research.subjects import Researcher, load_subjects
    from .scheduler import Progress

    if args.action == "import":
        from .research.importers import import_subjects

        if not args.file:
            sys.exit("subjects import FILE: a CSV or JSON file of subjects (see docs/research.md)")
        print(json.dumps(import_subjects(Path(args.file), config.ROOT), indent=1))
        return
    subs = load_subjects(config.ROOT, cfg)
    if args.subject:
        subs = [c for c in subs if c["id"] == args.subject or c.get("external_id") == args.subject]
        if not subs:
            sys.exit(f"no subject {args.subject!r} (config/subjects.yaml or `opencrawl subjects import`)")
    if args.action == "list":
        for c in subs[: args.limit or len(subs)]:
            print(f"{c['id']:<40} {(c.get('office') or c.get('kind') or '')[:40]:<40} {c.get('target','')}")
        print(f"{len(subs)} subjects")
        return
    store = config.make_store(cfg)
    r = Researcher(config.ROOT, cfg, config.make_fetcher(cfg), store)
    skip = set(args.skip.split(",")) if args.skip else set()
    try:
        if args.subject:
            from .redact import subject_id

            for c in subs:
                print(f"== {subject_id(c['id'])}")
                print("   " + json.dumps(r.research(c, since=args.since, skip=skip)))
            return
        opts = cfg.get("subjects", {})
        deadline = time.monotonic() + float(args.max_minutes or opts.get("max_minutes", 60)) * 60
        progress = Progress(cfg["state_dir"] / "subject_progress.json")
        res = r.research_many(subs[: args.limit] if args.limit else subs, args.since, deadline, progress, skip,
                              cadence_days=float(opts.get("cadence_days", 14)),
                              recent_cadence_days=float(opts.get("recent_cadence_days", 3)))
        print(json.dumps(res, indent=1))
        (cfg["state_dir"] / "research_last.json").write_text(json.dumps(res))   # the backfill reads this run's yield
    finally:
        store.close()


def cmd_packets(cfg, args):
    from .research.packets import write_packets
    from .research.quarantine import Quarantine
    from .research.subjects import load_subjects

    store = config.make_store(cfg)
    try:
        subs = load_subjects(config.ROOT, cfg)
        held = Quarantine(config.ROOT).all() if (cfg.get("privacy") or {}).get("export_quarantine") else []
        have = set(s for s in store.sources() if s.startswith("subj:")) | {f"subj:{r['subject_id']}" for r in held}
        write_packets(store, [c for c in subs if f"subj:{c['id']}" in have], held,
                      Path(args.out or (config.ROOT / "dist" / "bundle" / "packets")))
    finally:
        store.close()


def cmd_quarantine(cfg, args):
    from .research.quarantine import Quarantine

    q = Quarantine(config.ROOT)
    if args.action == "list":
        for r in q.all():
            if args.subject and r["subject_id"] != args.subject:
                continue
            print(f"{r['id']} {r['status']:<12} {r['subject_id']:<20} {r['domain']:<28} {r['claim'][:70]}")
    elif args.action == "add":
        if not (args.subject and args.url):
            sys.exit("quarantine add needs --subject and --url")
        row = q.add(args.subject, args.url, args.claim or "", origin="manual", author=args.author or "")
        q.flush()
        print(json.dumps(row, indent=2))
    else:
        if not args.id:
            sys.exit(f"quarantine {args.action} needs --id")
        status = {"corroborate": "corroborated", "debunk": "debunked", "retract": "retracted"}[args.action]
        row = q.set_status(args.id, status, args.evidence or "", args.note or "")
        print(json.dumps(row, indent=2) if row else f"no quarantined claim {args.id}")


def cmd_finance(cfg, args):
    """Campaign finance: imports, posted disclosure reports, FEC, entity resolution, organization lookups."""
    from .research.finance import Finance
    from .research.subjects import load_subjects

    store = config.make_store(cfg)
    try:
        fin = Finance(config.ROOT, cfg, config.make_fetcher(cfg), store)
        if args.action == "probe":
            print(json.dumps(fin.probe_commission(), indent=1))
            return
        subs = load_subjects(config.ROOT, cfg)
        if args.subject:
            subs = [c for c in subs if c["id"] == args.subject or c.get("external_id") == args.subject]
        deadline = time.monotonic() + float(args.max_minutes or 30) * 60
        print(json.dumps(fin.run(subs, since=args.since or cfg.get("limits", {}).get("since", "2016-01-01"),
                                 deadline=deadline, org_limit=int(args.org_limit or fin.opts.get("org_limit", 40))), indent=1))
    finally:
        store.close()


def cmd_profiles(cfg, args):
    """Profiles (positions, ideology markers, endorsements, ratings, votes, bills, career, money)."""
    from .research.profiles import build_profiles, write_profiles

    store = config.make_store(cfg)
    try:
        profiles, bundle = build_profiles(config.ROOT, store, cfg)
        if args.subject:
            for p in profiles:
                if p["subject_id"] == args.subject or p.get("external_id") == args.subject:
                    print(json.dumps(p, indent=1, ensure_ascii=False))
            return
        out = Path(args.out or (config.ROOT / "dist" / "bundle" / "profiles"))
        n = write_profiles(profiles, out, state_dir=cfg["state_dir"])
        thin = sum(1 for p in profiles if p["evidence_count"] < 2)
        print(json.dumps({"profiles": n, "thin": thin, "graph": bundle["graph"]}, indent=1))
    finally:
        store.close()


# ----------------------------------------------------------------------------- civic: public records requests
def cmd_records(cfg, args):
    from .civic.openrecords import Requests, discover_contacts, draft, plan
    from .civic.registry import jurisdictions

    reqs = Requests(config.ROOT, cfg)
    jur = jurisdictions(config.ROOT)
    if args.action == "contacts":
        deadline = time.monotonic() + float(args.max_minutes or 20) * 60
        n = discover_contacts(config.make_fetcher(cfg), jur, cfg["state_dir"] / "discovered.json", deadline)
        print(f"checked {n} jurisdictions for a records contact")
        return
    if args.action == "status":
        print(json.dumps({"sending_enabled": reqs.enabled, "why_not": reqs.why_not() or None, **reqs.summary()}, indent=1))
        return
    store = config.make_store(cfg)
    try:
        if args.action == "plan":
            items = plan(jur, reqs.tracker, store, int(cfg.get("openrecords", {}).get("cooldown_days", 180)))
            for it in items[: args.limit or 25]:
                print(f"{it['how']:<6} {it['target']['id']:<24} {it['to']}  (found on {it['found_on']})")
            print(f"{len(items)} due; {sum(i['how'] == 'email' for i in items)} by email, "
                  f"{sum(i['how'] == 'portal' for i in items)} portal-only")
            if items and args.show:
                print("\n" + "\n".join(draft(items[0]["target"], reqs.requester or "[requester]",
                                             reqs.mailbox.sender or "", reqs.law)))
        elif args.action == "send":
            print(json.dumps(reqs.send_batch(jur, store, limit=args.limit, dry_run=args.dry_run), indent=1))
        elif args.action == "check":
            print(json.dumps(reqs.check_replies(), indent=1))
        elif args.action == "follow-up":
            print(json.dumps(reqs.follow_ups(), indent=1))
    finally:
        store.close()


# ----------------------------------------------------------------------------- export and scheduling
def cmd_export(cfg, args):
    from .export import export

    _civic(cfg)
    store = config.make_store(cfg)
    try:
        export(config.ROOT, Path(args.out or (config.ROOT / "dist" / "bundle")), store=store, cfg=cfg,
               packets=not args.no_packets)
    finally:
        store.close()


def cmd_backfill(cfg, args):
    """Daytime backfill bookkeeping (opencrawl/backfill.py): `status`, or `next` at the end of a run."""
    from . import backfill
    from .research.subjects import load_subjects
    from .scheduler import Progress

    def never():
        try:
            prog = Progress(cfg["state_dir"] / "subject_progress.json")
            return sum(1 for c in load_subjects(config.ROOT, cfg) if not prog.get(f"subj:{c['id']}").get("last_ok"))
        except Exception:  # noqa: BLE001
            return None
    if args.action == "status":
        st = backfill.load_state(config.ROOT)
        print(json.dumps({"plan": backfill.plan(config.ROOT), "never_researched": never(),
                          "state": {k: v for k, v in st.items() if k != "history"}, "recent": st.get("history", [])[-5:]}, indent=1))
        return
    rp = cfg["state_dir"] / "research_last.json"
    research = json.loads(rp.read_text()) if rp.exists() else None
    out = backfill.next_run(config.ROOT, int(args.minutes or 0), bool(args.backfill), research,
                            never() if args.backfill else None, run_id=args.run_id or "")
    print(json.dumps(out, indent=1))
    if args.github_output:
        with open(args.github_output, "a") as f:
            f.write(f"go={'true' if out.get('go') else 'false'}\n")


# ----------------------------------------------------------------------------- security and privacy
def cmd_doctor(cfg, args):
    from . import doctor

    sys.exit(doctor.main(cfg, config.ROOT, browser=args.browser, ci=args.ci))


def cmd_purge(cfg, args):
    from . import privacy

    if not args.subject and not args.expired:
        sys.exit("purge: give --subject ID or --expired")
    store = config.make_store(cfg)
    try:
        if args.subject:
            print(json.dumps(privacy.erase_subject(config.ROOT, store, args.subject), indent=1))
        if args.expired:
            print(json.dumps(privacy.expire(config.ROOT, cfg, store, args.days), indent=1))
    finally:
        store.close()


def cmd_cache(cfg, args):
    from . import privacy

    print(json.dumps(privacy.prune_cache(cfg["state_dir"], args.days or 0), indent=1))


def cmd_vault(cfg, args):
    from . import vault

    if args.action == "keygen":
        print(vault.keygen())
        print("store it as the OPENCRAWL_DATA_KEY secret (and somewhere safe): without it the data can't be read",
              file=sys.stderr)
        return
    try:
        key = vault.key_from_env(args.key_env)
        p = [Path(x) for x in args.paths]
        if args.action == "encrypt" and len(p) == 2:
            print(json.dumps({"bytes": vault.encrypt_file(p[0], p[1], key), "out": str(p[1])}))
        elif args.action == "decrypt" and len(p) == 2:
            print(json.dumps({"bytes": vault.decrypt_file(p[0], p[1], key), "out": str(p[1])}))
        elif args.action == "pack" and len(p) >= 2:
            print(json.dumps(vault.pack(p[1:], p[0], key, base=Path.cwd(), exclude=tuple(args.exclude or ()))))
        elif args.action == "unpack" and len(p) == 2:
            print(json.dumps(vault.unpack(p[0], p[1], key)))
        else:
            sys.exit("vault: encrypt IN OUT | decrypt IN OUT | pack OUT PATH... | unpack IN DEST")
    except vault.VaultError as exc:
        sys.exit(f"vault: {exc}")


# ----------------------------------------------------------------------------- argument parsing
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="opencrawl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init"); i.add_argument("dir", nargs="?"); i.add_argument("--example")
    sub.add_parser("window")
    s = sub.add_parser("seed"); s.add_argument("--only"); s.add_argument("--refresh", action="store_true")
    s.add_argument("--workers", type=int); s.add_argument("--max-age-days", type=float); s.add_argument("--max-minutes", type=float)
    sub.add_parser("status")
    tg = sub.add_parser("targets"); tg.add_argument("--sources", action="store_true")
    d = sub.add_parser("discover"); d.add_argument("--source")
    r = sub.add_parser("run"); r.add_argument("--source"); r.add_argument("--force", action="store_true")
    r.add_argument("--full", action="store_true", help="re-crawl from the start of history (no delta)")
    r.add_argument("--shard", help="i/N: only sources whose host hashes to shard i of N")
    r.add_argument("--max-minutes", type=float); r.add_argument("--workers", type=int); r.add_argument("--tier", choices=["a", "b", "c"])
    r.add_argument("--kind", help="only sources of this kind (video = YouTube channels, ...)")
    dl = sub.add_parser("delta"); dl.add_argument("--since", required=True); dl.add_argument("--out", required=True)
    mg = sub.add_parser("merge"); mg.add_argument("deltas", nargs="*"); mg.add_argument("--state", nargs="*", default=[])
    se = sub.add_parser("search"); se.add_argument("query"); se.add_argument("--target"); se.add_argument("--kind")
    se.add_argument("--limit", type=int, default=20)
    sub.add_parser("tasks")
    b = sub.add_parser("blocked"); b.add_argument("--clear")
    o = sub.add_parser("ocr"); o.add_argument("files", nargs="+"); o.add_argument("--url")
    o.add_argument("--source", default="manual"); o.add_argument("--kind", default="ocr")
    c = sub.add_parser("capture"); c.add_argument("url"); c.add_argument("--source", default="capture")
    sc = sub.add_parser("screens"); sc.add_argument("action", choices=["ingest", "capture", "simulate", "box"])
    sc.add_argument("files", nargs="*", help="ingest: image files (default: imports/screens/); capture: URLs")
    sc.add_argument("--subject"); sc.add_argument("--note"); sc.add_argument("--device", choices=["phone", "desktop"], default="phone")
    sc.add_argument("--selector"); sc.add_argument("--source"); sc.add_argument("--kind", default="page"); sc.add_argument("--out")
    sc.add_argument("--box", choices=["ordinances", "posts", "text"])
    p = sub.add_parser("posts"); p.add_argument("action", choices=["account-add", "verify", "mark", "add", "pull", "list", "export"])
    for flag in ("--subject", "--subject-id", "--profile", "--proof", "--office", "--target", "--account", "--status",
                 "--note", "--url", "--text", "--posted-at", "--screenshot", "--topics", "--archived"):
        p.add_argument(flag)
    p.add_argument("--since", default="2016-01-01"); p.add_argument("--out", default="verified_posts.csv")
    p.add_argument("--all", action="store_true")
    sj = sub.add_parser("subjects"); sj.add_argument("action", choices=["list", "research", "import"]); sj.add_argument("file", nargs="?")
    sj.add_argument("--subject"); sj.add_argument("--since", default="2016-01-01"); sj.add_argument("--max-minutes", type=float)
    sj.add_argument("--limit", type=int)
    sj.add_argument("--skip", help="comma list: mentions,votes,sponsorships,posts,statements,articles,leads,social_mentions,connections")
    pk = sub.add_parser("packets"); pk.add_argument("--out")
    ru = sub.add_parser("quarantine"); ru.add_argument("action", choices=["list", "add", "corroborate", "debunk", "retract"])
    for flag in ("--subject", "--url", "--claim", "--author", "--id", "--evidence", "--note"):
        ru.add_argument(flag)
    e = sub.add_parser("export"); e.add_argument("--out"); e.add_argument("--no-packets", action="store_true")
    dv = sub.add_parser("derive"); dv.add_argument("--only"); dv.add_argument("--full", action="store_true"); dv.add_argument("--max-minutes", type=float)
    tr = sub.add_parser("transcripts"); tr.add_argument("--max-minutes", type=float); tr.add_argument("--limit", type=int)
    fi = sub.add_parser("finance"); fi.add_argument("action", nargs="?", default="run", choices=["run", "probe"])
    fi.add_argument("--subject"); fi.add_argument("--since"); fi.add_argument("--max-minutes", type=float); fi.add_argument("--org-limit", type=int)
    sp = sub.add_parser("sponsors"); sp.add_argument("--max-minutes", type=float); sp.add_argument("--limit", type=int)
    pf = sub.add_parser("profiles"); pf.add_argument("--subject"); pf.add_argument("--out")
    bf = sub.add_parser("backfill"); bf.add_argument("action", choices=["status", "next"]); bf.add_argument("--minutes", type=int)
    bf.add_argument("--run-id"); bf.add_argument("--backfill", action="store_true"); bf.add_argument("--github-output")
    rq = sub.add_parser("records"); rq.add_argument("action", choices=["status", "plan", "contacts", "send", "check", "follow-up"])
    rq.add_argument("--limit", type=int); rq.add_argument("--dry-run", action="store_true")
    rq.add_argument("--max-minutes", type=float); rq.add_argument("--show", action="store_true", help="plan: print the first draft")
    dr = sub.add_parser("doctor"); dr.add_argument("--browser", action="store_true", help="also start the headless browser once")
    dr.add_argument("--ci", action="store_true", help="also the checks for a scheduled CI run (encryption key, guard)")
    pu = sub.add_parser("purge"); pu.add_argument("--subject"); pu.add_argument("--expired", action="store_true")
    pu.add_argument("--days", type=float, help="--expired: override privacy.retention_days")
    ca = sub.add_parser("cache"); ca.add_argument("action", choices=["prune"]); ca.add_argument("--days", type=float)
    va = sub.add_parser("vault"); va.add_argument("action", choices=["keygen", "encrypt", "decrypt", "pack", "unpack"])
    va.add_argument("paths", nargs="*"); va.add_argument("--key-env", default="OPENCRAWL_DATA_KEY")
    va.add_argument("--exclude", action="append", help="pack: a path (relative) to leave out; repeatable")
    return ap


COMMANDS = {"init": cmd_init, "window": cmd_window, "seed": cmd_seed, "status": cmd_status, "targets": cmd_targets,
            "discover": cmd_discover, "run": cmd_run, "delta": cmd_delta, "merge": cmd_merge, "search": cmd_search,
            "tasks": cmd_tasks, "blocked": cmd_blocked, "ocr": cmd_ocr, "capture": cmd_capture, "screens": cmd_screens,
            "posts": cmd_posts, "subjects": cmd_subjects, "packets": cmd_packets, "quarantine": cmd_quarantine, "export": cmd_export,
            "records": cmd_records, "derive": cmd_derive, "transcripts": cmd_transcripts, "finance": cmd_finance,
            "sponsors": cmd_sponsors, "profiles": cmd_profiles, "backfill": cmd_backfill, "doctor": cmd_doctor,
            "purge": cmd_purge, "cache": cmd_cache, "vault": cmd_vault}


class _CISafeStream:
    """stdout/stderr inside GitHub Actions: text from the web that reaches the log can't act as a workflow command
    (lines starting with ::) or hide itself with control characters. The crawler never issues workflow commands
    itself; step outputs go through the $GITHUB_OUTPUT file."""

    def __init__(self, stream):
        self._s = stream
        self._at_line_start = True

    def write(self, text):
        from .redact import clean_output

        if not text:
            return 0
        out = clean_output(text)
        if self._at_line_start and out.lstrip().startswith("\u200b"):
            pass
        elif self._at_line_start and out.lstrip().startswith("::"):
            out = "\u200b" + out
        self._at_line_start = out.endswith("\n")
        self._s.write(out)
        return len(text)

    def __getattr__(self, name):
        return getattr(self._s, name)


def main(argv=None):
    import os

    os.umask(0o077)         # databases, state, exports and received records: readable by the owner only
    if os.environ.get("GITHUB_ACTIONS") == "true" and not isinstance(sys.stdout, _CISafeStream):
        sys.stdout, sys.stderr = _CISafeStream(sys.stdout), _CISafeStream(sys.stderr)
    args = build_parser().parse_args(argv)
    cfg = config.load()
    try:
        COMMANDS[args.cmd](cfg, args)
    except BrokenPipeError:  # `opencrawl targets | head`
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


if __name__ == "__main__":
    main()
