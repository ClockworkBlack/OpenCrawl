"""`opencrawl export`: an import-ready JSON bundle of everything crawled, derived and researched.

dist/bundle/
  manifest.json                 what's in the bundle, counts, schema version
  schema.md                     field reference
  targets.json                  every target: id, name, kind, website, portals, tier, crawl status per source,
                                record counts (civic targets also carry FIPS, counties, population...)
  targets/<id>.json             that target's records grouped by kind
  subjects.json                 subject index with record counts
  subjects/<id>.json            a subject's records (primary/reported only; never unverified)
  packets/<id>.json             research packets: opened sources ({title, url, date, ...}), votes by topic, mentions,
                                statements, endorsements, money, posts, leads, quarantined claims, profile
  profiles/<id>.json            profiles: ideology markers, positions by issue, statements, ratings,
                                endorsements, votes, bills, career, money — each item with its sources
  connections.json              the donors / organizations / subjects / offices graph and its patterns
  organizations.json            what each organization is, who runs it, who it funds (sourced)
  screenshots.json              the screenshot inbox: submitted screenshots by box, all needs-review
  verified_posts.json           every post in the verified-posts registry, with status and evidence
  blocked.json                  hosts that refused the crawler and the suggested manual route
only with settings.yaml privacy.export_quarantine: true (unverified claims about people are for review, not to publish):
  quarantine.json               unverified claims, kept apart, each with status + corroboration trail
  quarantine_videos.json        videos from channels not confirmed as a target's official account
civic module, written when there is anything to put in them:
  councils/<id>.json            how often each pair of council members voted the same way (non-unanimous votes)
  elections.json                locally run election results and qualifying lists
  ballot_questions.json         local ballot questions: text, type, amounts, results

Every record carries a stable `id` (hash of its natural key: same id when the content changes, so a site can
upsert) and `hash` (changes with the content). Per-subject files carry no run timestamps, so unchanged data
produces byte-identical files and a downstream repository only sees real changes.
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from . import __version__
from .blocked import BlockList
from .research.quarantine import Quarantine, VideoQuarantine
from .research.subjects import load_subjects
from .scheduler import Progress
from .store import Store
from .targets import all_sources, targets
from .verified_posts import Registry

SCHEMA_VERSION = "1.0"
SLIM = {"rid", "key", "first_seen", "thash"}

SCHEMA_MD = """# OpenCrawl bundle — field reference (schema {version})

## Record (targets/<id>.json, subjects/<id>.json)
| field | meaning |
|---|---|
| id | stable id (hash of source, kind, url and the record's own id): use as the upsert key |
| hash | content hash: changes when the record's content changes |
| version | how many times the content has changed since first seen |
| source_id | crawl source (`example-city:site`, `example-city:legistar`, `subj:example-person`) |
| target | target id (`example-city`), or empty for sources that belong to no target |
| kind | page, document, feed_item, post, article, ocr, transcript, video, ordinance, matter, meeting, agenda_item, vote, official, statement, mention, social_mention, connection, lead... |
| url | the page or API resource that was opened (or a link that resource itself gave); never built from a pattern |
| title, text, markdown | content (`markdown` only when it differs from `text`); articles keep excerpts only |
| published | date from the source when it gave one |
| channel | how it was fetched: api, wp_json, feed, sitemap, html, browser, pdf, ocr, import, store |
| evidence | primary (a target's own record / the subject's own channel), reported (listed news outlet), lead (not opened; never cite). Unverified items are only in the quarantine |
| fetched_at, changed_at | when this content was fetched / last changed |
| meta | kind-specific: adapter ids, vote lists, topics, OCR confidence, outlet, contribution details, opened_url |

## Research packet (packets/<id>.json)
subject_id, external_id, name, targets, note, counts, sources[{{title,url,date,kind,evidence,opened_at,excerpt,known}}],
votes[{{date,item,vote,result,matter,topics,refs}}], vote_topics{{topic:{{yes,no,other,examples}}}}, mentions, articles,
statements, endorsements, finance, posts, social_mentions, leads (not opened), quarantined (unverified; kept apart).
`refs` lists are 0-based indexes into `sources`.

## Profile (profiles/<id>.json)
ideology + ideology_refs, positions[{{topic,stance,date,refs,basis,evidence}}], statements[{{date,text,refs}}],
ratings[{{org,rating,year,refs}}], endorsements[] + endorsement_refs (+ endorsement_items with direction and basis),
votes[{{bill,session,title,vote,date,refs,basis}}], bills[{{number,session,caption,role,refs}}], ordinances_supported /
ordinances_opposed, affiliations, career[{{date,end_date,kind,role,body,how,refs,basis}}],
money{{total_raised,donors,organizations,employers,patterns}}, evidence_count, depth.

## Connections (connections.json)
nodes[{{id,kind,name,attrs}}], edges[{{a,b,type,source,amount,date,role,evidence}}] (contributed, paid, funds, officer_of, employed_by,
committee_of, connected_to, endorsed, serves, sponsored, appointed_to, member_of), patterns[] (shared_donor, employer_cluster,
org_chain, officer_overlap, donor_official, shared_endorser, donor_matter [evidence lead: verify first]).

## Council alignment (councils/<id>.json, civic)
members[], votes_counted, pairs[{{a,b,shared,agree,pct}}] over non-unanimous roll calls.

## Target (targets.json)
id, name, kind, website, website_source, portals{{youtube, facebook, legistar, agenda_center, granicus, civicclerk, boarddocs,
ecode360, amlegal, municode...}}, feeds[], tier (a|b|c), sources[] (crawl status), record_counts{{kind: n}}; civic targets add
fips, counties, county_seat, population, officials[].
"""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


_HEAD = ("id", "hash", "source_id", "target", "kind", "url", "title", "text", "markdown", "published", "channel", "evidence")
_TAIL = ("fetched_at", "changed_at", "version")


def _write_target(path: Path, t: dict, store: Store, counts: dict) -> list[dict]:
    """Stream a target's records to its file kind by kind, copying each record's stored meta JSON through as is
    (no decode and re-encode); returns the vote records' vote lists for the council alignment table."""
    votes: list[dict] = []
    cols = ",".join(_HEAD + ("meta",) + _TAIL)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"target": %s, "name": %s, "exported_at": %s, "records": {' %
                 (json.dumps(t["id"]), json.dumps(t["name"], ensure_ascii=False), json.dumps(_now())))
        first_kind = True
        for kind in sorted(counts):
            if kind == "screenshot":
                continue
            rows = store.raw_iter("target=? AND kind=? AND source_id NOT LIKE 'subj:%'", (t["id"], kind), cols=cols)
            first = True
            for r in rows:
                if first:
                    fh.write(("" if first_kind else ", ") + json.dumps(kind) + ": [")
                    first_kind, first = False, False
                else:
                    fh.write(", ")
                head = {k: r[k] for k in _HEAD}
                if head["markdown"] == head["text"]:
                    head.pop("markdown")
                fh.write(json.dumps(head, ensure_ascii=False)[:-1] + ', "meta": ' + (r["meta"] or "{}") + ", " +
                         json.dumps({k: r[k] for k in _TAIL})[1:])
                if kind == "vote":
                    votes.append({"meta": {"votes": json.loads(r["meta"] or "{}").get("votes", [])}})
            if not first:
                fh.write("]")
        fh.write("}}")
    return votes


def _slim(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k not in SLIM}
    if out.get("markdown") == out.get("text"):
        out.pop("markdown", None)
    out.pop("snippet", None)
    out.pop("score", None)
    return out


def council_alignment(votes: list[dict]) -> dict:
    """Pairwise agreement between members over non-unanimous recorded votes."""
    members, shared, agree, counted = Counter(), Counter(), Counter(), 0
    for r in votes:
        vs = {(v.get("person") or "").strip(): (v.get("vote") or "").strip().lower()
              for v in r.get("meta", {}).get("votes", []) if v.get("person")}
        cast = {p: v for p, v in vs.items() if v in ("yea", "nay", "aye", "no", "yes")}
        norm = {p: ("yes" if v in ("yea", "aye", "yes") else "no") for p, v in cast.items()}
        if len(set(norm.values())) < 2:
            continue  # unanimous: says nothing about alignment
        counted += 1
        members.update(norm.keys())
        for a, b in combinations(sorted(norm), 2):
            shared[(a, b)] += 1
            agree[(a, b)] += norm[a] == norm[b]
    pairs = [{"a": a, "b": b, "shared": n, "agree": agree[(a, b)], "pct": round(100 * agree[(a, b)] / n, 1)}
             for (a, b), n in sorted(shared.items(), key=lambda kv: -kv[1]) if n >= 3]
    return {"members": sorted(members), "votes_counted": counted, "pairs": pairs}


def export(root: Path, out: Path, log=print, store: Store | None = None, cfg: dict | None = None,
           packets: bool = True) -> dict:
    root, out = Path(root), Path(out)
    for sub in ("targets", "subjects", "councils", "packets"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    own = store is None
    store = store or Store(root / "data")
    store.flush()
    progress = Progress(root / "state" / "progress.json")
    src_status = {s["id"]: {k: v for k, v in progress.get(s["id"]).items()
                            if k in ("last_ok", "status", "channel", "mode", "new", "seen")} for s in all_sources(root)}
    by_target_counts = defaultdict(dict)
    for (tid, kind), n in _counts_by_target_kind(store).items():
        by_target_counts[tid][kind] = n
    kinds_present = set(store.counts("kind"))

    # ---- targets
    all_t = targets(root)
    status_by_target = defaultdict(list)
    for sid, st in src_status.items():
        status_by_target[sid.split(":")[0]].append({"id": sid, **st})
    index, total_records = [], Counter()
    for t in all_t:
        counts = by_target_counts.get(t["id"], {})
        if counts:
            votes = _write_target(out / "targets" / f"{_safe(t['id'])}.json", t, store, counts)
            if votes:
                al = council_alignment(votes)
                if al["votes_counted"]:
                    (out / "councils" / f"{_safe(t['id'])}.json").write_text(json.dumps({"target": t["id"], **al}, indent=1))
        total_records.update({k: v for k, v in counts.items()})
        entry = {k: v for k, v in t.items() if k not in ("hints", "site_error", "wikipedia_error", "wikidata_error", "lookup_error")}
        entry["sources"] = status_by_target.get(t["id"], [])
        entry["record_counts"] = counts
        index.append(entry)
    (out / "targets.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- subjects (only those with something found), profiles and packets.
    # Each subject's records are read once and feed all three outputs.
    subjects = load_subjects(root, cfg)
    privacy = (cfg or {}).get("privacy") or {}
    claims = Quarantine(root).all()
    claims_by = defaultdict(list)
    for r in claims:
        claims_by[r["subject_id"]].append(r)
    sindex = []
    subject_sources = set(s for s in store.sources() if s.startswith("subj:"))
    n_packets = n_profiles = 0
    graph_counts = {}
    pb, profiles = None, []
    if packets:
        from .research.packets import file_name, packet_for, write_packet_index
        from .research.profiles import ProfileBuilder

        legis = _load_json(root / "state" / "legis_people.json")
        pb = ProfileBuilder(root, store, cfg, subjects, legis)
    for c in subjects:
        sid = f"subj:{c['id']}"
        records: dict[str, list] = {}
        if sid in subject_sources:
            for kind in store.kinds(sid):
                records[kind] = [r for r in store.read(sid, kind) if r.get("evidence") != "unverified"]
            grouped = {k: [_slim(r) for r in v] for k, v in records.items()}
            counts = {k: len(v) for k, v in grouped.items()}
            total_records.update(counts)
            slim_c = {k: v for k, v in c.items() if k not in ("variants", "known_urls")}
            (out / "subjects" / f"{_safe(c['id'])}.json").write_text(
                json.dumps({"subject": slim_c, "exported_at": _now(), "records": grouped}, ensure_ascii=False), encoding="utf-8")
            sindex.append({**slim_c, "record_counts": counts})
        if pb is None:
            continue
        prof = pb.profile(c, records)
        if prof:
            profiles.append(prof)
        held = claims_by.get(c["id"], []) if privacy.get("export_quarantine") else []
        pk = packet_for(c, records, held, prof) if (records or held) else None
        if pk:
            (out / "packets" / f"{file_name(c)}.json").write_text(
                json.dumps(pk, ensure_ascii=False, indent=1), encoding="utf-8")
            n_packets += 1
    (out / "subjects.json").write_text(json.dumps(sindex, ensure_ascii=False, indent=1), encoding="utf-8")
    if pb is not None:
        from .research.connections import export_graph
        from .research.profiles import write_profiles

        write_packet_index(out / "packets")
        log(f"wrote {n_packets} research packets to {out / 'packets'}")
        pb.finish()
        n_profiles = write_profiles(profiles, out / "profiles", log=log)
        bundle = pb.bundle()
        graph_counts = export_graph(bundle["_graph"], bundle["_patterns"], out / "connections.json")
        orgs = [_slim(r) for r in store.iter_query("kind='organization'")]
        (out / "organizations.json").write_text(json.dumps({"note": "What each organization in the money and endorsement "
                                                            "records is (registries and its own site), who runs it and who it "
                                                            "funds; every field carries its source url.", "organizations": orgs},
                                                           ensure_ascii=False), encoding="utf-8")

    # ---- civic: elections, qualifying lists, ballot questions, legislation (when there is any)
    civic: dict = {}
    t_by_id = {t["id"]: t for t in all_t}
    if kinds_present & {"election_result", "qualifying"}:
        from .civic.elections import export_elections, export_qualifying

        els, qual = export_elections(store, t_by_id), export_qualifying(store)
        (out / "elections.json").write_text(json.dumps({"note": "Locally run election results; contests of type "
                                                        "'question' are ballot questions.", "elections": els,
                                                        "qualifying": qual}, ensure_ascii=False), encoding="utf-8")
        civic["elections"], civic["qualifying"] = len(els), len(qual)
    if kinds_present & {"ballot_question", "election_result"}:
        from .civic.ballot import export_questions

        qs = export_questions(store, t_by_id)
        (out / "ballot_questions.json").write_text(json.dumps({"note": "Local ballot questions: text (sample ballots, notices, "
                                                               "resolutions) paired with results where both were found.",
                                                               "questions": qs}, ensure_ascii=False), encoding="utf-8")
        civic["ballot_questions"] = len(qs)

    # ---- the screenshot inbox (items grouped by box)
    from .screens import Inbox

    box = Inbox.box_export(store, include_text=privacy.get("export_screenshot_text", True))
    (out / "screenshots.json").write_text(json.dumps({"note": "Screenshots people submitted, read by OCR. Each item "
                                                      "needs review: open the original page or post before citing it.",
                                                      "boxes": box}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- posts, quarantine (opt-in), blocked
    posts = Registry(root / "data" / "verified").posts()
    (out / "verified_posts.json").write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
    videos = VideoQuarantine(root).all()
    for name in ("quarantine.json", "quarantine_videos.json"):
        (out / name).unlink(missing_ok=True)
    if privacy.get("export_quarantine"):
        (out / "quarantine.json").write_text(json.dumps({
            "note": "Unverified claims, kept apart from every record. Never cite one as fact.", "claims": claims},
            ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "quarantine_videos.json").write_text(json.dumps({
            "note": "Videos from YouTube channels not confirmed as a target's official account (reason per row). "
                    "Never cite these; confirm the channel by hand first.", "videos": videos},
            ensure_ascii=False, indent=1), encoding="utf-8")
    blocked = BlockList(root / "state" / "blocked.json").data
    (out / "blocked.json").write_text(json.dumps(blocked, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "schema.md").write_text(SCHEMA_MD.format(version=SCHEMA_VERSION), encoding="utf-8")

    manifest = {
        "schema_version": SCHEMA_VERSION, "opencrawl_version": __version__, "generated_at": _now(),
        "targets": len(index), "targets_with_website": sum(1 for t in index if t.get("website")),
        "targets_with_records": sum(1 for t in index if t["record_counts"]),
        "councils_with_vote_alignment": len(list((out / "councils").glob("*.json"))),
        "subjects_with_records": len(sindex), "research_packets": n_packets, "profiles": n_profiles,
        "connections": graph_counts,
        "verified_posts": sum(1 for p in posts if p["status"] == "verified"),
        "quarantine": {"claims": dict(Counter(r["status"] for r in claims)), "videos": len(videos),
                       "exported": bool(privacy.get("export_quarantine"))},
        "blocked_hosts": len(blocked), "screenshots": {k: len(v) for k, v in box.items()},
        **civic,
        "records_by_kind": dict(total_records), "records_total": store.total(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if own:
        store.close()
    log(json.dumps(manifest, indent=1))
    return manifest


def _safe(name: str) -> str:
    """A file name for an id: ids are validated when loaded (targets.safe_id), this is the last line of defense."""
    from .targets import safe_id

    if not safe_id(name):
        raise ValueError(f"unsafe id for a file name: {name[:60]!r}")
    return name


def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _counts_by_target_kind(store: Store) -> dict:
    with store._lock:
        rows = store.db.execute("SELECT target, kind, COUNT(*) FROM records WHERE target<>'' AND "
                                "source_id NOT LIKE 'subj:%' GROUP BY target, kind").fetchall()
    return {(r[0], r[1]): r[2] for r in rows}
