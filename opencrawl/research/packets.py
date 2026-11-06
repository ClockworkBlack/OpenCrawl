"""Research packets: one JSON file per subject with everything the crawler found, every item pointing at the
source it rests on.

packets/<id>.json   sources[{title, url, date, kind, evidence, opened_at, excerpt, known}], every one opened by the
                    crawler; votes grouped by topic; mentions in crawled records; statements, endorsements,
                    contributions, posts, social mentions; leads (not opened, never to be cited); quarantined
                    claims (unverified, kept apart). `refs` lists are 0-based indexes into `sources`. `known` marks a
                    source already listed in the subject's `known_urls` (what you have already), so new ones stand out.
packets/index.json  the packet ids and the note below.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

PACKET_NOTE = ("Every entry under sources was opened by the crawler (fetched with HTTP 200 on opened_at) and the "
               "excerpt is text from that page. leads were NOT opened: open one before citing it. quarantined "
               "entries are unverified claims kept apart; never cite them as fact. vote_topics are counts of "
               "recorded votes, not stated positions.")


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:100]


def file_name(c: dict) -> str:
    """The packet/profile file name for a subject: its external id or id, made safe for a path."""
    return slug(c.get("external_id") or "") or slug(c["id"]) or "subject"


def norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower()).split("#")[0].split("?")[0].rstrip("/")


# --------------------------------------------------------------------------- packets
def build_packet(c: dict, records: dict[str, list], quarantined: list[dict], known_urls: set[str]) -> dict:
    sources: list[dict] = []
    index: dict[str, int] = {}

    def src(r: dict, excerpt: str = "") -> int:
        u = r.get("url") or ""
        k = norm_url(u)
        if k in index:
            i = index[k]
            if excerpt and not sources[i].get("excerpt"):
                sources[i]["excerpt"] = excerpt
            return i
        index[k] = len(sources)
        sources.append({"title": (r.get("title") or "").strip()[:200] or u, "url": u,
                        "date": (r.get("published") or "")[:10], "kind": r["kind"],
                        "evidence": r.get("evidence") or "primary", "opened_at": (r.get("fetched_at") or "")[:10],
                        "excerpt": excerpt[:600], "known": k in known_urls})
        return index[k]

    def d10(r):
        return (r.get("published") or r.get("fetched_at") or "")[:10]

    votes, topics = [], defaultdict(lambda: {"yes": 0, "no": 0, "other": 0, "examples": []})
    for r in sorted(records.get("vote", []), key=d10, reverse=True)[:300]:
        m = r.get("meta", {})
        i = src(r)
        v = (m.get("vote") or "").lower()
        bucket = "yes" if v in ("yea", "yes", "aye", "for") else "no" if v in ("nay", "no", "against") else "other"
        for t in m.get("topics", []):
            topics[t][bucket] += 1
            if len(topics[t]["examples"]) < 5:
                topics[t]["examples"].append(len(votes))
        votes.append({"date": d10(r), "item": r.get("title", "")[:300], "vote": m.get("vote"), "result": m.get("result"),
                      "matter": m.get("matter_file"), "topics": m.get("topics", []), "refs": [i]})
    mentions = [{"date": d10(r), "text": (r.get("text") or "")[:600], "record_kind": r.get("meta", {}).get("record_kind"),
                 "refs": [src(r, r.get("text") or "")]} for r in sorted(records.get("mention", []), key=d10, reverse=True)[:60]]
    articles = [{"date": d10(r), "outlet": r.get("meta", {}).get("outlet"), "refs": [src(r, (r.get("text") or ""))]}
                for r in sorted(records.get("article", []), key=d10, reverse=True)[:60]]
    statements = [{"date": d10(r), "text": (r.get("text") or "")[:600], "refs": [src(r, (r.get("text") or "")[:600])]}
                  for r in sorted(records.get("statement", []), key=d10, reverse=True)[:40]]
    posts = [{"date": d10(r), "text": (r.get("text") or "")[:600], "platform": r.get("meta", {}).get("platform"),
              "refs": [src(r, (r.get("text") or "")[:600])]} for r in sorted(records.get("post", []), key=d10, reverse=True)[:60]]
    endorsements, finance = [], []
    for r in records.get("connection", []):
        m = r.get("meta", {})
        if m.get("type") == "endorsement":
            endorsements.append({"name": m.get("party"), "refs": [src(r)]})
        elif m.get("type") == "contribution":
            finance.append({"name": m.get("name"), "amount": m.get("amount"), "date": m.get("date"),
                            "employer": m.get("employer"), "refs": [src(r)]})
    social = [{"date": d10(r), "text": (r.get("text") or "")[:400], "author": r.get("meta", {}).get("author"),
               "refs": [src(r, (r.get("text") or "")[:400])]} for r in records.get("social_mention", [])[:30]]
    leads = [{"title": r.get("title", "")[:200], "url": r.get("url"), "outlet": r.get("meta", {}).get("outlet"),
              "date": d10(r), "why": r.get("meta", {}).get("why", "not opened")} for r in records.get("lead", [])[:40]]
    return {
        "subject_id": c["id"], "external_id": c.get("external_id") or "", "name": c["name"],
        "aliases": c.get("aliases", []), "targets": c.get("targets") or ([c["target"]] if c.get("target") else []),
        "note": PACKET_NOTE,
        "counts": {"sources": len(sources), "new_sources": sum(1 for s in sources if not s["known"]),
                   "votes": len(votes), "mentions": len(mentions), "articles": len(articles), "leads": len(leads),
                   "quarantined": len(quarantined)},
        "sources": sources, "votes": votes, "vote_topics": dict(topics), "mentions": mentions, "articles": articles,
        "statements": statements, "endorsements": endorsements, "finance": finance, "posts": posts,
        "social_mentions": social, "leads": leads,
        "quarantined": [{"id": x["id"], "claim": x["claim"], "url": x["url"], "status": x["status"],
                         "first_seen": x["first_seen"][:10], "opened": x.get("opened", False)} for x in quarantined],
    }


def packet_for(c: dict, grouped: dict, quarantined: list[dict], profile: dict | None = None) -> dict | None:
    """One subject's research packet from records already grouped by kind (unverified ones excluded by the caller);
    None when there is nothing to put in it."""
    if not any(grouped.values()) and not quarantined:
        return None
    pk = build_packet(c, grouped, quarantined, set(c.get("known_urls") or []))
    if profile:
        pk["profile"] = {k: v for k, v in profile.items() if k not in ("name", "subject_id", "external_id", "has_new")}
        pk["counts"]["evidence_count"] = profile.get("evidence_count", 0)
    return pk


def write_packet_index(out_dir: Path) -> None:
    index = sorted((p.stem for p in out_dir.glob("*.json") if p.name != "index.json"))
    (out_dir / "index.json").write_text(json.dumps({"packets": index, "note": PACKET_NOTE}, indent=1), encoding="utf-8")


def write_packets(store, subjects: list[dict], quarantined: list[dict], out_dir: Path, log=print,
                  profiles: dict | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    by_subject = defaultdict(list)
    for r in quarantined:
        by_subject[r["subject_id"]].append(r)
    n = 0
    for c in subjects:
        sid = f"subj:{c['id']}"
        grouped = defaultdict(list)
        for kind in store.kinds(sid):
            grouped[kind] = [r for r in store.read(sid, kind) if r.get("evidence") != "unverified"]
        if not any(grouped.values()) and not by_subject.get(c["id"]):
            continue
        pk = build_packet(c, grouped, by_subject.get(c["id"], []), set(c.get("known_urls") or []))
        if profiles and c["id"] in profiles:
            pk["profile"] = {k: v for k, v in profiles[c["id"]].items() if k not in ("name", "subject_id", "external_id")}
            pk["counts"]["evidence_count"] = profiles[c["id"]].get("evidence_count", 0)
        name = file_name(c)
        (out_dir / f"{name}.json").write_text(json.dumps(pk, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    index = sorted((p.stem for p in out_dir.glob("*.json") if p.name != "index.json"))
    (out_dir / "index.json").write_text(json.dumps({"packets": index, "note": PACKET_NOTE}, indent=1), encoding="utf-8")
    log(f"wrote {n} research packets to {out_dir}")
    return n
