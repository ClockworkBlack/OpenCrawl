"""`opencrawl export`: an import-ready JSON bundle of everything crawled, derived and researched.

dist/bundle/
  manifest.json                 what's in the bundle, counts, schema version
  schema.md                     field reference
  targets.json                  every target: id, name, kind, website, portals, tier, crawl status per source,
                                record counts (civic targets also carry FIPS, counties, population...)
  targets/<id>.json             that target's records grouped by kind
  subjects.json                 subject index with record counts
  subjects/<id>.json            a subject's records (primary/reported only; never unverified)
  packets/<id>.json             research packets: opened sources in {t,u,d} form, votes by topic, mentions,
                                statements, endorsements, money, posts, leads, rumors, profile (research module)
  profiles/<id>.json            profile supplements: ideology markers, positions by issue, statements, ratings,
                                endorsements, votes, bills, career, money — each item with its source
  connections.json              the donors / organizations / subjects / offices graph and its patterns
  organizations.json            what each organization is, who runs it, who it funds (sourced)
  screenshots.json              the Screenshots box: submitted screenshots by box, all needs-review
  verified_posts.json           every post in the verified-posts registry, with status and evidence
  rumor_mill.json               unverified items, kept apart, each with status + corroboration trail
  rumor_mill_video.json         videos from channels not confirmed as a target's official account
  blocked.json                  hosts that refused the crawler and the suggested manual route
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
from .research.subjects import RumorMill, load_subjects
from .scheduler import Progress
from .store import Store
from .targets import all_sources, targets
from .verified_posts import Registry

SCHEMA_VERSION = "3.0"
SLIM = {"rid", "key", "first_seen", "thash"}

SCHEMA_MD = """# OpenCrawl bundle — field reference (schema {version})

## Record (targets/<id>.json, subjects/<id>.json)
| field | meaning |
|---|---|
| id | stable id (hash of source, kind, url and the record's own id): use as the upsert key |
| hash | content hash: changes when the record's content changes |
| version | how many times the content has changed since first seen |
| source_id | crawl source (`springfield:site`, `springfield:legistar`, `subj:jane-doe`) |
| target | target id (`springfield`), or empty for sources that belong to no target |
| kind | page, document, feed_item, post, article, ocr, transcript, video, ordinance, matter, meeting, agenda_item, vote, official, statement, mention, social_mention, connection, lead... |
| url | the page or API resource that was opened (or a link that resource itself gave); never built from a pattern |
| title, text, markdown | content (`markdown` only when it differs from `text`); articles keep excerpts only |
| published | date from the source when it gave one |
| channel | how it was fetched: api, wp_json, feed, sitemap, html, browser, pdf, ocr, import, store |
| evidence | primary (a target's own record / the subject's own channel), reported (listed news outlet), lead (not opened; never cite). Unverified items are only in rumor_mill.json |
| fetched_at, changed_at | when this content was fetched / last changed |
| meta | kind-specific: adapter ids, vote lists, topics, OCR confidence, outlet, contribution details, opened_url |

## Research packet (packets/<id>.json)
key, subject_id, name, targets, note, counts, sources[{{t,u,d,kind,evidence,opened_at,excerpt}}] (t title, u url, d date),
votes[{{d,item,vote,result,matter,topics,src}}], vote_topics{{topic:{{yes,no,other,examples}}}}, mentions, articles,
statements, endorsements, finance, posts, social_mentions, leads (not opened), rumors (unverified; kept apart).
`src` lists are 0-based indexes into `sources`.

## Profile supplement (profiles/<id>.json)
ideology + isrc, positions[{{topic,stance,d,src,basis,evidence}}], statements[{{d,text,src}}], ratings[{{org,rating,year,src}}],
endorsements[] + esrc (+ endorsement_items with direction and basis), votes[{{bill,session,title,vote,d,src,basis}}],
bills[{{number,session,caption,role,src}}], ordinances_supported / ordinances_opposed, affiliations,
career[{{d,to,kind,role,body,how,src,basis}}], money{{total_raised,donors,organizations,employers,patterns}}, inference_inputs, depth.

## Connections (connections.json)
nodes[{{id,kind,name,attrs}}], edges[{{a,b,type,src,amount,date,role,evidence}}] (contributed, paid, funds, officer_of, employed_by,
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
            votes = _write_target(out / "targets" / f"{t['id']}.json", t, store, counts)
            if votes:
                al = council_alignment(votes)
                if al["votes_counted"]:
                    (out / "councils" / f"{t['id']}.json").write_text(json.dumps({"target": t["id"], **al}, indent=1))
        total_records.update({k: v for k, v in counts.items()})
        entry = {k: v for k, v in t.items() if k not in ("hints", "site_error", "wikipedia_error", "wikidata_error", "lookup_error")}
        entry["sources"] = status_by_target.get(t["id"], [])
        entry["record_counts"] = counts
        index.append(entry)
    (out / "targets.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- subjects (only those with something found), profile supplements and packets.
    # Each subject's records are read once and feed all three outputs.
    subjects = load_subjects(root, cfg)
    rm = RumorMill(root)
    rumors = rm.all()
    rumors_by = defaultdict(list)
    for r in rumors:
        rumors_by[r["subject_id"]].append(r)
    sindex = []
    subject_sources = set(s for s in store.sources() if s.startswith("subj:"))
    n_packets = n_profiles = 0
    graph_counts = {}
    pb, profiles = None, []
    if packets:
        from .research.packets import packet_for, slug, write_packet_index
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
            slim_c = {k: v for k, v in c.items() if k not in ("variants", "profile_urls")}
            (out / "subjects" / f"{c['id']}.json").write_text(
                json.dumps({"subject": slim_c, "exported_at": _now(), "records": grouped}, ensure_ascii=False), encoding="utf-8")
            sindex.append({**slim_c, "record_counts": counts})
        if pb is None:
            continue
        prof = pb.profile(c, records)
        if prof:
            profiles.append(prof)
        pk = packet_for(c, records, rumors_by.get(c["id"], []), prof) if (records or rumors_by.get(c["id"])) else None
        if pk:
            (out / "packets" / f"{slug(c.get('key') or c['id'])}.json").write_text(
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
        (out / "elections.json").write_text(json.dumps({"note": "Locally run election results (eids prefixed x); contests "
                                                        "with sub 'question' are ballot questions.", "elections": els,
                                                        "qualifying": qual}, ensure_ascii=False), encoding="utf-8")
        civic["elections"], civic["qualifying"] = len(els), len(qual)
    if kinds_present & {"ballot_question", "election_result"}:
        from .civic.ballot import export_questions

        qs = export_questions(store, t_by_id)
        (out / "ballot_questions.json").write_text(json.dumps({"note": "Local ballot questions: text (sample ballots, notices, "
                                                               "resolutions) paired with results where both were found.",
                                                               "questions": qs}, ensure_ascii=False), encoding="utf-8")
        civic["ballot_questions"] = len(qs)

    # ---- the Screenshots box (inbox items, grouped by box)
    from .screens import Inbox

    box = Inbox.box_export(store)
    (out / "screenshots.json").write_text(json.dumps({"note": "Screenshots people submitted, read by OCR. Each item "
                                                      "needs review: open the original page or post before citing it.",
                                                      "boxes": box}, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---- posts, rumors, blocked
    posts = Registry(root / "data" / "verified").posts()
    (out / "verified_posts.json").write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "rumor_mill.json").write_text(json.dumps(rumors, ensure_ascii=False, indent=1), encoding="utf-8")
    from .research.rumor_video import VideoRumorMill

    rumor_videos = VideoRumorMill(root).all()
    (out / "rumor_mill_video.json").write_text(json.dumps({
        "note": "Rumor Mill - Video: videos from YouTube channels not confirmed as a target's official account "
                "(reason per row). Never cite these; confirm the channel by hand first.",
        "videos": rumor_videos}, ensure_ascii=False, indent=1), encoding="utf-8")
    blocked = BlockList(root / "state" / "blocked.json").data
    (out / "blocked.json").write_text(json.dumps(blocked, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "schema.md").write_text(SCHEMA_MD.format(version=SCHEMA_VERSION), encoding="utf-8")

    manifest = {
        "schema_version": SCHEMA_VERSION, "opencrawl_version": __version__, "generated_at": _now(),
        "targets": len(index), "targets_with_website": sum(1 for t in index if t.get("website")),
        "targets_with_records": sum(1 for t in index if t["record_counts"]),
        "councils_with_vote_alignment": len(list((out / "councils").glob("*.json"))),
        "subjects_with_records": len(sindex), "research_packets": n_packets, "profile_supplements": n_profiles,
        "connections": graph_counts,
        "verified_posts": sum(1 for p in posts if p["status"] == "verified"),
        "rumor_mill": dict(Counter(r["status"] for r in rumors)), "blocked_hosts": len(blocked),
        "screenshots": {k: len(v) for k, v in box.items()}, "rumor_mill_videos": len(rumor_videos),
        **civic,
        "records_by_kind": dict(total_records), "records_total": store.total(),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    if own:
        store.close()
    log(json.dumps(manifest, indent=1))
    return manifest


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
