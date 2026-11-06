"""Pipeline benchmark (offline): the local steps of a nightly merge job on a realistically sized database.

Builds a synthetic store around a generated subject list (fictional people spread over the targets of the
Colorado example registry): council roll calls and minutes for N targets, transcripts, ordinances with sponsors,
appointments, subject statements/articles, campaign contributions and organizations. Then times each step:
research (no-network steps), finance entity resolution, the connections graph and patterns, profile supplements
for every subject, and the export.

usage: python scripts/bench_pipeline.py [--targets 30] [--subjects 4000] [--votes 3000] [--contribs 60000] [--keep DIR]
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from opencrawl.research.subjects import Researcher, load_subjects  # noqa: E402
from opencrawl.store import Record, Store  # noqa: E402

WORDS = ("council budget zoning ordinance water sewer park road police fire tax millage permit variance hearing "
         "resolution annexation contract library mayor clerk motion second approved tabled public comment data center "
         "short-term rental housing sidewalk paving stormwater").split()
STANCE = ["I will fight to lower property taxes and keep the millage rate flat.",
          "I oppose the proposed data center until a water study is done.",
          "I support raising teacher pay and funding public schools.",
          "We must expand sidewalks and fix our roads before new development.",
          "I believe public safety means more officers and a new fire station."]


class Timer:
    def __init__(self):
        self.rows = []

    def __call__(self, label):
        t = self

        class _T:
            def __enter__(self):
                self.t0 = time.perf_counter()

            def __exit__(self, *a):
                t.rows.append((label, time.perf_counter() - self.t0))
                print(f"{label:<48} {t.rows[-1][1]:8.2f} s", flush=True)
        return _T()


FIRST = "Alex Pat Casey Riley Jordan Morgan Taylor Avery Quinn Dana Kim Lee Sam Jamie Drew Robin Blake Cameron Reese Devon".split()
LAST = "Local Example Rivera Vale Post Reed Hill Bass Ward Third Stone Field Brook Lane Cross Marsh Pike Dale Frost Hale".split()


def generated_subjects(targets: list[dict], n: int, rnd: random.Random) -> list[dict]:
    """n fictional subjects, each tied to one target (a council member or candidate there)."""
    out = []
    for i in range(n):
        t = targets[i % len(targets)]
        name = f"{FIRST[i % len(FIRST)]} {LAST[(i // len(FIRST)) % len(LAST)]}" + (f" {i // (len(FIRST) * len(LAST)) + 1}" if i >= len(FIRST) * len(LAST) else "")
        out.append({"id": f"s{i:05d}", "name": name, "target": t["id"], "targets": [t["id"]], "office": f"Council Member - {t['name']}",
                    "priority": i % 50 == 0, "last_race_date": f"{2019 + i % 7}-11-05"})
    return out


def build(root: Path, n_jur: int, votes_per: int, n_contribs: int, seed: int = 7, n_subjects: int = 4000) -> dict:
    import yaml

    rnd = random.Random(seed)
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "state").mkdir(parents=True, exist_ok=True)
    example = ROOT / "examples" / "colorado-civic" / "config"
    for f in ("outlets.yaml", "topics.yaml", "issues.yaml", "finance.yaml", "organizations.yaml", "overrides.yaml"):
        shutil.copy(ROOT / "config" / f, root / "config" / f)
    for f in ("jurisdictions.yaml", "school_districts.yaml", "press.yaml", "guides.yaml"):
        shutil.copy(example / f, root / "config" / f)
    from opencrawl.targets import targets as all_targets

    tg = [t for t in all_targets(root) if t.get("kind") in ("city", "town", "county", "consolidated")]
    (root / "config" / "subjects.yaml").write_text(yaml.safe_dump({"subjects": generated_subjects(tg, n_subjects, rnd)}, sort_keys=False))
    cands = load_subjects(root, {})
    by_jur: dict[str, list[dict]] = {}
    for c in cands:
        if c.get("target"):
            by_jur.setdefault(c["target"], []).append(c)
    jurs = sorted(by_jur, key=lambda j: -len(by_jur[j]))[:n_jur]
    store = Store(root / "data")
    n = 0
    t0 = time.perf_counter()
    researched = []
    for jid in jurs:
        members = by_jur[jid][:7] if len(by_jur[jid]) >= 7 else by_jur[jid]
        names = [m["name"] for m in members]
        researched += by_jur[jid][:60]
        sid = f"{jid}:legistar"
        base = f"https://webapi.legistar.com/v1/{jid}"
        recs = []
        for i in range(votes_per):
            title = " ".join(rnd.choices(WORDS, k=12)).capitalize()
            date = f"20{rnd.randint(16, 26)}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"
            votes = [{"person": nm, "person_id": k, "vote": rnd.choice(["Yea", "Yea", "Yea", "Nay"])} for k, nm in enumerate(names)]
            recs.append(Record(sid, "vote", f"https://{jid}.legistar.com/MeetingDetail.aspx?ID={i // 20}", title=title, text=title,
                               published=date, channel="api", target=jid, evidence="primary",
                               meta={"EventId": i // 20, "EventItemId": i, "EventBodyName": "City Council", "MatterFile": f"{date[:4]}-{i}",
                                     "result": "Pass", "votes": votes, "topics": ["Budget and taxes"], "opened": True}))
            recs.append(Record(f"{jid}:site", "agenda_item", f"https://{jid}.example/minutes/{i // 15}.pdf", title=title,
                               text=f"Motion by {names[0]}, seconded by {names[1 % len(names)]}, to approve {title}. Motion carried.",
                               published=date, channel="derived", target=jid, evidence="primary",
                               key=f"{jid}:site|minutes|https://{jid}.example/minutes/{i // 15}.pdf|{i}|item",
                               meta={"from": "minutes", "present": names, "mover": names[0], "result": "passed", "opened": True}))
        for i in range(votes_per // 6):
            sp = [{"name": rnd.choice(names), "person_id": 1, "sequence": 1}]
            recs.append(Record(sid, "ordinance", f"{base}/matters/{i}", title="An ordinance amending " + " ".join(rnd.choices(WORDS, k=6)),
                               text="ordinance", published="2024-01-01", channel="api", target=jid, evidence="primary",
                               meta={"MatterId": i, "MatterFile": f"ORD-{i}", "MatterTypeName": "Ordinance", "provider": "legistar",
                                     "sponsors": sp, "sponsors_modified": "x", "opened": True}))
        for i in range(max(10, votes_per // 50)):
            lines = []
            for k in range(400):
                who = rnd.choice(names + ["MAYOR", "CLERK", "PUBLIC SPEAKER"])
                lines.append(f"[00:{k // 60:02d}:{k % 60:02d}] {who.upper()}: {rnd.choice(STANCE) if rnd.random() < .2 else ' '.join(rnd.choices(WORDS, k=14))}")
            recs.append(Record(f"{jid}:granicus", "transcript", f"https://{jid}.granicus.com/TranscriptViewer.php?clip_id={i}", title="Transcript",
                               text="\n".join(lines), published="2025-05-01", target=jid, evidence="primary",
                               meta={"body": "City Council", "meeting_date": "2025-05-01"}))
        for i in range(20):
            who = rnd.choice(by_jur[jid])
            recs.append(Record("press:governor", "appointment", f"https://gov.example/pr/{jid}/{i}", title=f"{who['name']} appointed",
                               text=f"{who['name']} appointed to the Board", published="2025-01-01", channel="derived", evidence="primary",
                               meta={"person": who["name"], "body": "Planning Commission", "level": jid, "opened": True}))
        n += store.add_many(recs)
    for c in researched[:1500]:
        sid = f"subj:{c['id']}"
        recs = []
        for k in range(5):
            recs.append(Record(sid, "statement", f"https://{c['id']}.example/p{k}", title="Issues",
                               text=" ".join(rnd.sample(STANCE, 3)) + " " + " ".join(rnd.choices(WORDS, k=300)),
                               target=c.get("target", ""), evidence="primary", meta={"subject_id": c["id"], "opened": True}))
            recs.append(Record(sid, "article", f"https://news.example/{c['id']}/{k}", title="Coverage",
                               text=f"{c['name']} said she would vote against the rezoning. \"{rnd.choice(STANCE)}\" {c['name'].split()[-1]} told the paper. "
                                    + " ".join(rnd.choices(WORDS, k=200)), target=c.get("target", ""), evidence="reported",
                               meta={"subject_id": c["id"], "outlet": "news.example", "opened": True}))
        n += store.add_many(recs)
    # money: contributions to the researched candidates; donors, employers, PACs
    donors = [f"{rnd.choice(['John','Mary','Pat','Kim','Lee','Sam','Ann','Bob','Sue','Tom'])} {rnd.choice(['Smith','Jones','Brown','Davis','Miller','Wilson','Moore','Taylor','Anderson','Thomas','Jackson','White','Harris','Martin'])}{k}" for k in range(15000)]
    employers = [f"{rnd.choice(WORDS).title()} {rnd.choice(['Development','Construction','Law','Realty','Engineering'])}, LLC" for _ in range(800)]
    pacs = [f"Mountain {rnd.choice(WORDS).title()} {rnd.choice(['PAC','Political Action Committee','Association PAC'])} {k}" for k in range(1500)]
    recips = researched[:1500]
    recs = []
    for k in range(n_contribs):
        c = rnd.choice(recips)
        org = rnd.random() < 0.15
        name = rnd.choice(pacs) if org else rnd.choice(donors)
        date = f"20{rnd.randint(18, 26)}-{rnd.randint(1, 12):02d}-{rnd.randint(1, 28):02d}"
        recs.append(Record(f"subj:{c['id']}", "contribution", f"file://bulk{k % 40}.csv", title=f"Contribution: {name}",
                           text="{}", published=date, channel="import", evidence="primary",
                           key=f"subj:{c['id']}|contribution|bulk|{k}",
                           meta={"type": "contribution", "name": name, "amount": round(rnd.uniform(25, 2500), 2), "date": date,
                                 "employer": "" if org else rnd.choice(employers), "occupation": "", "city": "Rifle", "state": "CO",
                                 "recipient": c["name"], "recipient_kind": "candidate", "donor_is_org": org, "subject_id": c["id"],
                                 "filing_source": "bench", "opened": True}))
        if len(recs) >= 5000:
            n += store.add_many(recs)
            recs = []
    n += store.add_many(recs)
    store.flush()
    print(f"built {n} records in {time.perf_counter() - t0:.1f} s ({len(cands)} candidates, {len(jurs)} targets)", flush=True)
    return {"store": store, "cands": cands, "researched": researched, "jurs": jurs}


def run(root: Path, data: dict, timer: Timer):
    store, cands, researched = data["store"], data["cands"], data["researched"]
    from opencrawl.fetch import Fetcher

    f = Fetcher("Bench/1", root / "state", min_interval=0)
    r = Researcher(root, {}, f, store, log=lambda *_: None)
    sample = researched[:400]
    with timer(f"research: votes + sponsorships, {len(sample)} candidates"):
        for c in sample:
            cls = r._classifier(c)
            counts = {}
            add = r._adder(c, cls, counts)
            r._votes(c, cls, add, "2016-01-01")
            r._sponsorships(c, cls, add, "2016-01-01")
        store.flush()
    with timer(f"research: mentions (FTS), {len(sample[:150])} candidates"):
        for c in sample[:150]:
            cls = r._classifier(c)
            r._mentions(c, cls, r._adder(c, cls, {}), "2016-01-01")
        store.flush()
    from opencrawl.research.finance import Finance

    fin = Finance(root, {}, None, store, log=lambda *_: None)
    with timer("finance: entity resolution over contributions"):
        fin.resolve_all(cands)
    from opencrawl.research import connections
    from opencrawl.research.entities import EntityIndex

    ix = EntityIndex(root / "state" / "entities.json", store=store)
    with timer("connections: build graph"):
        g = connections.build(store, ix)
    with timer("connections: patterns"):
        pats = connections.patterns(g, store)
    print(f"   graph: {len(g.nodes)} nodes, {len(g.edges)} edges, {len(pats)} patterns")
    with timer(f"connections: money_for, {len(researched[:300])} candidates"):
        for c in researched[:300]:
            connections.money_for(g, store, ix, c["id"], pats)
    from opencrawl.research.profiles import build_profiles, write_profiles

    with timer(f"profiles: build all ({len(cands)} candidates)"):
        profs, _ = build_profiles(root, store, {}, cands)
    with timer("profiles: write"):
        write_profiles(profs, root / "dist" / "profiles", log=lambda *_: None)
    from opencrawl.export import export

    with timer("export (full bundle)"):
        export(root, root / "dist" / "bundle", log=lambda *_: None, store=store, cfg={})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", type=int, default=30)
    ap.add_argument("--votes", type=int, default=3000)
    ap.add_argument("--contribs", type=int, default=60000)
    ap.add_argument("--subjects", type=int, default=4000)
    ap.add_argument("--keep")
    a = ap.parse_args()
    root = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="opencrawl-bench-"))
    reuse = (root / "data" / "crawl.db").exists()
    timer = Timer()
    if reuse:
        data = {"store": Store(root / "data"), "cands": load_subjects(root, {})}
        by = {}
        for c in data["cands"]:
            if c.get("target"):
                by.setdefault(c["target"], []).append(c)
        jurs = sorted(by, key=lambda j: -len(by[j]))[:a.targets]
        data["researched"] = [c for j in jurs for c in by[j][:60]]
        print(f"reusing {root}")
    else:
        data = build(root, a.targets, a.votes, a.contribs, n_subjects=a.subjects)
    run(root, data, timer)
    print("\n| step | seconds |\n|---|---|")
    for label, s in timer.rows:
        print(f"| {label} | {s:.2f} |")
    data["store"].close()
    if not a.keep:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
