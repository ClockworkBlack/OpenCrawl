"""Subject research at scale: everything said by and about a list of named people or organizations.

A *subject* is anyone or anything you track by name: a candidate, an official, an executive, an organization,
a campaign. Subjects come from config/subjects.yaml (see the README). For each, the researcher collects
statements, articles, mentions in every crawled record, votes and sponsorships (civic), social posts and
connections, grades every item by evidence, and routes unverified material to the quarantine.

Evidence levels
  primary     an official record (a vote, an agenda item, a filing, a document on a target's own site) or the
              subject's own channel (its website, a verified social account)
  reported    a news outlet listed in config/outlets.yaml
  unverified  anything else. Kept in the quarantine (data/quarantine/), never in a
              profile, until corroborated (`opencrawl quarantine`)
  lead        found in a search index but NOT opened (blocked, paywalled, robots,
              redirect-only links). Never cited; listed so a researcher can open it.

Where things come from (cost per subject in brackets)
  mentions        full-text search over every crawled record             [no network]
  votes           roll calls already in the database (civic adapters)    [no network]
  sponsorships    sponsor lists already in the database (civic)          [no network]
  posts           verified social accounts (Bluesky / YouTube / X APIs)  [only if accounts known]
  statements      the subject's own websites                             [only if sites known]
  articles        GDELT news index, several names per query; each article opened and checked
                  for the name AND a place / context word before it counts; only excerpts are kept  [shared]
  leads           Google News RSS (links can't be opened directly)       [1 request]
  social_mentions Bluesky public search: posts by anyone that name the subject. Off unless
                  subjects.bluesky_search is true (it collects what members of the public wrote)  [1-3 requests]
  connections     endorsements on the subject's site; imports/finance/<id>.csv; FEC (federal candidates)

Subject fields (config/subjects.yaml)
  id, name, aliases[]        who; aliases are other spellings or former names
  kind                       person | organization (free text)
  target, targets[]          the target(s) whose records are searched first (and whose names disambiguate news)
  places[]                   words an article must contain besides the name (a city, a state, an employer...)
  websites[], social[]       the subject's own channels (primary evidence; first website = proof page for accounts)
  search_terms[], news_feeds[]   extra news queries / feeds
  priority: true             researched before everyone else
  finance_csv, fec_id        money (civic: campaign-finance exports, FEC candidate id)
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import yaml

from .. import channels, extract, redact
from ..blocked import Blocked
from ..feeds import parse_feed
from ..fetch import Disallowed, Fetcher
from ..names import excerpts, name_regex, name_variants
from ..targets import by_id, load_yaml
from ..store import Record, Store, utcnow
from ..verified_posts import Registry, handle_of, platform_of
from .quarantine import Quarantine

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"
GNEWS = "https://news.google.com/rss/search"
BSKY_SEARCH = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
FEC = "https://api.open.fec.gov/v1"
STEPS = ("mentions", "votes", "sponsorships", "posts", "statements", "articles", "leads", "social_mentions", "connections")
GDELT_WINDOW_DAYS = 89      # the DOC API searches a rolling ~3-month window
GDELT_NAMES_PER_QUERY = 6


# ----------------------------------------------------------------------------- config
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_subjects(root: Path, cfg: dict | None = None, imported: list[dict] | None = None) -> list[dict]:
    """Subjects from config/subjects.yaml plus the set brought in by `opencrawl subjects import`
    (state/subjects_imported.json, or an explicit `imported` list); hand entries win and list fields are unioned."""
    out: dict[str, dict] = {}
    if imported is None:
        from .importers import imported_subjects

        imported = imported_subjects(root)
    for c in imported or []:
        c = dict(c)
        c["id"] = c.get("id") or _slug(c["name"])
        out[c["id"]] = c
    p = root / "config" / "subjects.yaml"
    d = load_yaml(p) or {}
    hand = d.get("subjects") or []
    for c in hand:
        c = dict(c)
        c["id"] = c.get("id") or _slug(c["name"])
        base = out.get(c["id"], {})
        merged = {**base, **{k: v for k, v in c.items() if v not in (None, "", [])}}
        for k in ("websites", "social", "aliases"):  # union list fields
            merged[k] = list(dict.fromkeys((base.get(k) or []) + (c.get(k) or [])))
        out[c["id"]] = merged
    from ..targets import _warn, safe_id

    for sid in [k for k in out if not safe_id(k)]:
        _warn(f"subject id {sid[:60]!r} skipped: use lower-case letters, digits, '.', '_' and '-'")
        out.pop(sid)
    if ((cfg or {}).get("privacy") or {}).get("public_figures_only", False):
        for sid in [k for k, c in out.items() if not is_public_figure(c)]:
            _warn(f"subject {sid!r} skipped: a person is researched only with an office, public_role or title "
                  "(settings.yaml privacy.public_figures_only)")
            out.pop(sid)
    for c in out.values():
        for k in ("aliases", "websites", "social", "news_feeds", "targets", "places", "known_urls"):
            c.setdefault(k, [])
        if not c["targets"] and c.get("target"):
            c["targets"] = [c["target"]]
        c["variants"] = c.get("variants") or name_variants(c["name"], c["aliases"])
    return list(out.values())


def is_public_figure(c: dict) -> bool:
    """Organizations, and people who hold or seek a public role: an office (held or run for), a public_role
    ("spokesperson for X", "CEO of Y") or a title, or `public_figure: true` set on purpose. Research on anyone
    else is skipped only when privacy.public_figures_only is true."""
    kind = str(c.get("kind") or "person").lower()
    if kind not in ("person", "individual", "people"):
        return True
    return bool(c.get("office") or c.get("public_role") or c.get("title") or c.get("public_figure") is True
                or c.get("races"))


def load_outlets(root: Path) -> tuple[set[str], set[str]]:
    p = root / "config" / "outlets.yaml"
    d = load_yaml(p)
    return set(d.get("outlets") or []), set(d.get("exclude_domains") or [])


def domain_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.").removeprefix("m.").removeprefix("amp.")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])  # e.g. example.co.uk
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _in(domain: str, domains: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


PRIMARY_SUFFIXES = (".gov", ".mil")
PRIMARY_DOMAINS = {"legistar.com", "granicus.com", "civicclerk.com", "municode.com", "ecode360.com", "amlegal.com",
                   "boarddocs.com"}


class Classifier:
    """Evidence grade of a URL for one subject. settings.yaml `evidence:` adds primary_suffixes / primary_domains."""

    def __init__(self, root: Path, candidate: dict, target: dict | None, registry: Registry,
                 outlets: tuple[set, set] | None = None, extra_official: set[str] | None = None,
                 evidence_cfg: dict | None = None):
        self.outlets, self.exclude = outlets or load_outlets(root)
        ev = evidence_cfg or {}
        self.suffixes = tuple(ev.get("primary_suffixes") or PRIMARY_SUFFIXES)
        self.official = set(PRIMARY_DOMAINS) | set(ev.get("primary_domains") or [])
        self.official |= extra_official or set()
        if target and target.get("website"):
            self.official.add(domain_of(target["website"]))
        self.own = {domain_of(w) for w in candidate.get("websites", [])}
        self.verified_accounts = {(a["platform"], a.get("handle")) for a in registry.accounts() if a.get("status") == "verified"
                                  and a.get("subject_id") == candidate["id"]}

    def excluded(self, url: str) -> bool:
        return _in(domain_of(url), self.exclude)

    def classify(self, url: str) -> str:
        d = domain_of(url)
        if d.endswith(self.suffixes) or _in(d, self.official) or _in(d, self.own):
            return "primary"
        plat = platform_of(url)
        if plat:
            return "primary" if (plat, handle_of(url)) in self.verified_accounts else "unverified"
        if _in(d, self.outlets):
            return "reported"
        return "unverified"


# ----------------------------------------------------------------------------- researcher
class Researcher:
    def __init__(self, root: Path, cfg: dict, fetcher: Fetcher, store: Store, log=print):
        self.root, self.cfg, self.f, self.store, self.log = root, cfg, fetcher, store, log
        self.registry = Registry(root / "data" / "verified")
        self.quarantine = Quarantine(root)
        self.targets = by_id(root)
        self.outlets = load_outlets(root)
        self.opts = (cfg or {}).get("subjects", {})
        self.evidence_cfg = (cfg or {}).get("evidence", {})
        self._opened: dict[str, dict | None] = {}  # article url -> {"title","text","url","outlet"} or None (couldn't open)
        from collections import OrderedDict

        self._roll: OrderedDict = OrderedDict()     # target -> {person key: [(row, vote)]}, a few at a time
        self._spons: OrderedDict = OrderedDict()    # target -> {person key: [(row, sponsor)]}

    @staticmethod
    def _pk(name: str) -> str:
        return re.sub(r"[^a-z]", "", (name or "").lower())

    def _cached(self, cache, jid: str, build):
        if jid in cache:
            cache.move_to_end(jid)
            return cache[jid]
        cache[jid] = build(jid)
        while len(cache) > 24:          # bounded: research visits many targets in one run
            cache.popitem(last=False)
        return cache[jid]

    def _roll_index(self, jid: str) -> dict:
        """Every roll call in a target, by voter (read once per run, not once per candidate)."""
        def build(j):
            idx: dict[str, list] = {}
            for r in self.store.iter_query("target=? AND kind='vote'", (j,)):
                m = r["meta"]
                row = {"id": r["id"], "url": r["url"], "title": r["title"], "published": r.get("published"),
                       "meta": {k: m.get(k) for k in ("result", "MatterFile", "EventBodyName", "topics", "EventItemId",
                                                      "opened_url", "procedural", "from", "tally", "ordinance_ref")}}
                for v in m.get("votes", []):
                    k = self._pk(v.get("person"))
                    if k:
                        idx.setdefault(k, []).append((row, v))
            return idx
        return self._cached(self._roll, jid, build)

    def _sponsor_index(self, jid: str) -> dict:
        def build(j):
            idx: dict[str, list] = {}
            for r in self.store.iter_query("target=? AND kind IN ('ordinance','matter') AND meta LIKE '%\"sponsors\": [{%'", (j,)):
                m = r["meta"]
                for sp in m.get("sponsors") or []:
                    k = self._pk(sp.get("name"))
                    if k:
                        idx.setdefault(k, []).append((r, sp))
            return idx
        return self._cached(self._spons, jid, build)

    # ------------------------------------------------------------------ plumbing
    def _classifier(self, c: dict) -> Classifier:
        j = self.targets.get(c.get("target") or "")
        extra = {domain_of(self.targets[x]["website"]) for x in c.get("targets", [])
                 if x in self.targets and self.targets[x].get("website")}
        return Classifier(self.root, c, j, self.registry, self.outlets, extra, self.evidence_cfg)

    def _adder(self, c: dict, cls: Classifier, counts: dict):
        sid = f"subj:{c['id']}"

        def add(rec: Record) -> None:
            rec.source_id, rec.target = sid, c.get("target", "")
            rec.meta.setdefault("subject_id", c["id"])
            if cls.excluded(rec.url):
                return
            rec.evidence = rec.evidence or cls.classify(rec.url)
            if rec.evidence == "unverified":
                self.quarantine.add(c["id"], rec.url, rec.title or rec.text[:300], origin=rec.kind, seen_at=rec.published,
                                author=rec.meta.get("author", ""), meta={"via": rec.channel},
                                opened=bool(rec.meta.get("opened")))
                counts["quarantined"] = counts.get("quarantined", 0) + 1
                return
            if self.store.add(rec):
                counts[rec.kind] = counts.get(rec.kind, 0) + 1
        return add

    def _places(self, c: dict) -> list[str]:
        """Words that tie an article to this subject: its `places`, its targets' names, counties (civic), and the
        project's `subjects.places` (a state, a region). Empty = any article naming the subject counts."""
        out = list(c.get("places") or [])
        for jid in c.get("targets", []):
            j = self.targets.get(jid)
            if j:
                out.append(j["name"])
        out += [f"{x} County" for x in c.get("counties", [])]
        out += list(self.opts.get("places") or [])
        return out

    # ------------------------------------------------------------------ single candidate
    def research(self, c: dict, since: str = "2016-01-01", skip: set[str] | None = None) -> dict:
        skip = set(skip or ())
        cls = self._classifier(c)
        counts: dict[str, int] = {}
        add = self._adder(c, cls, counts)
        c.setdefault("variants", name_variants(c["name"], c.get("aliases", [])))
        for name in STEPS:
            if name in skip or (name == "social_mentions" and not self.opts.get("bluesky_search", False)):
                continue
            try:
                if name == "articles":
                    self.articles_batch([c], since, {c["id"]: add})
                else:
                    getattr(self, "_" + name)(c, cls, add, since)
            except (Disallowed, Blocked) as exc:
                self.log(f"  {name}: skipped ({redact.text(exc)[:200]})")
            except Exception as exc:  # noqa: BLE001
                self.log(f"  {name}: error {redact.exc(exc)}")
        self.store.flush()
        self.quarantine.flush()
        return counts

    # ------------------------------------------------------------------ many candidates, budgeted
    def research_many(self, cands: list[dict], since: str, deadline: float, progress, skip: set[str] | None = None,
                      cadence_days: float = 7, recent_cadence_days: float = 3) -> dict:
        """Research due candidates until the deadline. Mentions and votes (no network) run for every due
        candidate first; network steps follow, with news searched for several candidates per query."""
        skip = set(skip or ())
        now = dt.datetime.now(dt.timezone.utc)
        this_year = str(now.year)

        def recent(c) -> bool:      # `last_active`: date of the subject's latest notable activity (a race, a filing)
            return (str(c.get("last_active") or ""))[:4] >= str(now.year - 1)

        def due(c):
            p = progress.get(f"subj:{c['id']}")
            last = p.get("last_ok")
            cad = recent_cadence_days if recent(c) else cadence_days
            return not last or (now - dt.datetime.fromisoformat(last)).total_seconds() > cad * 86400

        inputs = _profile_inputs(self.root, self.store)   # how much the last profile build could already say per subject
        order = sorted((c for c in cands if due(c)), key=lambda c: (
            not c.get("priority"),                                              # priority: true first
            bool(progress.get(f"subj:{c['id']}").get("last_ok")),            # never researched first
            min(inputs.get(c["id"], 0), 2) >= 2,                                # then those with the thinnest profiles
            -int(str(c.get("last_active") or "0000")[:4] >= this_year),
            progress.get(f"subj:{c['id']}").get("last_ok") or ""))
        totals: dict[str, int] = defaultdict(int)
        done = 0
        adders, clss = {}, {}
        for c in order:
            if time.monotonic() > deadline:
                break
            cls = clss[c["id"]] = self._classifier(c)
            counts: dict[str, int] = {}
            add = adders[c["id"]] = self._adder(c, cls, counts)
            for name in ("mentions", "votes", "sponsorships", "posts", "statements", "leads", "social_mentions", "connections"):
                if name in skip or time.monotonic() > deadline:
                    continue
                if name in ("posts", "statements") and not (c.get("websites") or c.get("social")):
                    continue
                if name == "social_mentions" and not self.opts.get("bluesky_search", False):
                    continue
                if name == "leads" and not self.opts.get("google_news", True):
                    continue
                if name == "leads" and self.opts.get("leads_for", "all") == "priority_or_recent" and not (
                        c.get("priority") or recent(c)):
                    continue  # leads are uncitable pointers: spend the request budget where they matter
                try:
                    getattr(self, "_" + name)(c, cls, add, since)
                except (Disallowed, Blocked):
                    pass
                except Exception as exc:  # noqa: BLE001
                    self.log(f"  {redact.subject_id(c['id'])} {name}: {redact.exc(exc)}")
            for k, v in counts.items():
                totals[k] += v
            done += 1
            progress.update(f"subj:{c['id']}", last_ok=utcnow(), status="ok", counts=counts)
            if done % 50 == 0:
                self.store.flush()
                self.quarantine.flush()
                self.log(f"  researched {done}/{len(order)}")
        # news, batched by place: several names per query, each article opened once for all of them
        if "articles" not in skip and self.opts.get("gdelt", True):
            groups = defaultdict(list)
            for c in order[:done]:
                groups[c.get("target") or "-".join(c.get("counties", [])[:1]) or "all"].append(c)
            for _, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                if time.monotonic() > deadline:
                    break
                for i in range(0, len(group), GDELT_NAMES_PER_QUERY):
                    if time.monotonic() > deadline:
                        break
                    chunk = group[i:i + GDELT_NAMES_PER_QUERY]
                    try:
                        n = self.articles_batch(chunk, since, {c["id"]: adders[c["id"]] for c in chunk})
                        totals["article"] += n
                    except (Disallowed, Blocked):
                        break
                    except Exception as exc:  # noqa: BLE001
                        self.log(f"  articles: {redact.exc(exc)}")
        self.store.flush()
        self.quarantine.flush()
        return {"researched": done, "due": len(order), "added": dict(totals)}

    # ------------------------------------------------------------------ official records (no network)
    def _mentions(self, c, cls, add, since):
        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        match = Store.fts_phrase(*variants)
        if not match:
            return
        jids = [j for j in c.get("targets", []) if j]
        where, params = "r.source_id NOT LIKE 'subj:%' AND r.kind NOT IN ('vote')", ()
        if jids:
            where += f" AND r.target IN ({','.join('?' * len(jids))})"
            params = tuple(jids)
        rx = name_regex(variants)
        from ..transcripts import time_at

        for hit in self.store.search(match, where, params, limit=int(self.opts.get("max_mentions", 150))):
            text = hit.get("text") or ""
            ex = excerpts(text, rx, n=2) or [hit.get("snippet", "")]
            meta = {"record_id": hit["id"], "record_kind": hit["kind"], "source": hit["source_id"],
                    "record_jurisdiction": hit.get("target"), "opened": True, "opened_at": hit.get("fetched_at")}
            if hit["kind"] == "transcript":
                first = rx.search(text)
                meta["time"] = time_at(hit, first.start()) if first else None
                meta["video_page"] = hit.get("meta", {}).get("video_page")
                meta["meeting_date"] = hit.get("meta", {}).get("meeting_date")
            add(Record("", "mention", hit["url"], title=(hit.get("title") or "")[:200], text="\n".join(ex)[:1200],
                       published=hit.get("published") or meta.get("meeting_date"), channel="store", evidence="primary", meta=meta))

    def _votes(self, c, cls, add, since):
        jids = [j for j in c.get("targets", []) if j]
        if not jids:
            return
        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        want = {self._pk(v) for v in variants}
        seen_rows: set = set()
        for jid in jids:
            idx = self._roll_index(jid)
            for k in want:
                for r, v in idx.get(k, ()):
                    if r["id"] in seen_rows:
                        continue
                    seen_rows.add(r["id"])
                    m = r["meta"]
                    add(Record("", "vote", r["url"], title=r["title"], text=r["title"], published=r.get("published"),
                               channel="store", evidence="primary",
                               meta={"vote": v.get("vote"), "result": m.get("result"), "matter_file": m.get("MatterFile"),
                                     "body": m.get("EventBodyName"), "topics": m.get("topics", []),
                                     "EventItemId": m.get("EventItemId"), "record_id": r["id"], "opened": True,
                                     "opened_url": m.get("opened_url"), "basis": v.get("basis") or "roll call",
                                     "role": v.get("role", ""), "procedural": bool(m.get("procedural")),
                                     "from": m.get("from") or "legistar", "tally": m.get("tally", ""),
                                     "ordinance_ref": m.get("ordinance_ref", "")}))

    def _sponsorships(self, c, cls, add, since):
        """Ordinances and resolutions this person sponsored (Legistar sponsor lists already in the store)."""
        jids = [j for j in c.get("targets", []) if j]
        if not jids:
            return
        variants = c.get("variants") or name_variants(c["name"], c.get("aliases", []))
        want = {self._pk(v) for v in variants}
        seen_rows: set = set()
        for jid in jids:
            idx = self._sponsor_index(jid)
            for k in want:
                for r, sp in idx.get(k, ()):
                    if r["id"] in seen_rows:
                        continue
                    seen_rows.add(r["id"])
                    m = r["meta"]
                    role = "sponsor" if (sp.get("sequence") in (None, 0, 1)) else "co-sponsor"
                    add(Record("", "sponsorship", r["url"], title=r["title"], text=(r.get("text") or r["title"])[:600],
                               published=r.get("published"), channel="store", evidence="primary",
                               meta={"number": m.get("MatterFile"), "MatterId": m.get("MatterId"), "role": role,
                                     "status": m.get("MatterStatusName"), "type": m.get("MatterTypeName"),
                                     "enacted": bool(m.get("MatterEnactmentDate")), "enacted_date": m.get("MatterEnactmentDate"),
                                     "topics": m.get("topics", []), "record_id": r["id"], "opened": True,
                                     "opened_url": m.get("opened_url") or r["url"]}))

    # ------------------------------------------------------------------ the candidate's own channels
    def _posts(self, c, cls, add, since):
        proof = c["websites"][0] if c.get("websites") else ""
        for url in c.get("social", []):
            if platform_of(url):
                row = self.registry.add_account(c["name"], url, proof, c.get("office", ""), c.get("target", ""))
                self.registry.mark_subject(row["id"], c["id"])
        if proof:
            self.registry.verify_accounts(self.f, only_subject=c["id"])
        self.registry.pull(self.f, since=since, only_subject=c["id"])
        cls.verified_accounts = {(a["platform"], a.get("handle")) for a in self.registry.accounts()
                                 if a.get("status") == "verified" and a.get("subject_id") == c["id"]}
        for p in self.registry.posts():
            if p.get("subject_id") == c["id"] and p["status"] == "verified":
                add(Record("", "post", p["post_url"], title=p["text"][:120], text=p["text"],
                           published=p.get("posted_at"), channel=p.get("source", "manual"), evidence="primary",
                           meta={"platform": p["platform"], "account_id": p["account_id"], "topics": p.get("topics", []),
                                 "opened": True}))

    def _statements(self, c, cls, add, since):
        for i, site in enumerate(c.get("websites", [])):
            src = {"id": f"subj:{c['id']}:site{i}", "url": site, "kind": "statement", "depth": 3, "max_pages": 60,
                   "exclude": ["/tag/", "/author/", "?share", "/feed", "/wp-json", "login", "/cart", "/donate", "/shop"]}
            probes = channels.probe_source(self.f, src)
            chosen = channels.choose(probes)
            if not chosen:
                self.log(f"  statements: no channel for {site}")
                continue
            ctx = channels.CrawlContext(frontier=self.store, page_revisit_days=7)
            for rec in channels.crawl(self.f, src, {"channel": chosen.channel, "endpoint": chosen.endpoint}, {}, ctx=ctx):
                if isinstance(rec, channels.Checkpoint):
                    continue
                rec.kind = "statement"
                rec.evidence = "primary"
                for m in re.finditer(r"endorse[ds]?\s+by\s+(?:the\s+)?([A-Z][^.\n!?\[\]]{3,80})", rec.text or "", re.I):
                    add(Record("", "connection", rec.url, title=f"Endorsement: {m.group(1).strip()}",
                               text=m.group(0), published=rec.published, channel=rec.channel, evidence="primary",
                               meta={"type": "endorsement", "party": m.group(1).strip(), "self_reported": True,
                                     "opened": True}))
                add(rec)

    # ------------------------------------------------------------------ the press
    def _open_article(self, url: str) -> dict | None:
        """Fetch an article once (polite, robots-respecting) and keep its title and text; None if it can't be opened."""
        if url in self._opened:
            return self._opened[url]
        out = None
        try:
            r = self.f.get(url, use_cache=False)      # only excerpts are kept: no full copy in the HTTP cache either
            if r.status == 200 and "html" in r.content_type:
                title, md = extract.html_to_markdown(r.text, r.url)
                out = {"url": r.url, "title": title, "text": md}
            self.store.mark_url(url, r.status, "articles", r.content_type)
        except (Disallowed, Blocked) as exc:
            out = None
            self._opened[url + "#why"] = {"why": type(exc).__name__}
        except Exception:  # noqa: BLE001
            out = None
        self._opened[url] = out
        return out

    def articles_batch(self, group: list[dict], since: str, adders: dict) -> int:
        """One GDELT query for several candidates; open each hit once and attribute it to every candidate whose
        name AND place appear in the article. Unopenable hits whose title names a candidate become leads."""
        floor = max(since[:10], (dt.date.today() - dt.timedelta(days=GDELT_WINDOW_DAYS)).isoformat())
        phrases = []
        for c in group:
            v = (c.get("variants") or name_variants(c["name"], c.get("aliases", [])))[:1]
            phrases += [f'"{x}"' for x in v]
        if not phrases:
            return 0
        q = (phrases[0] if len(phrases) == 1 else "(" + " OR ".join(phrases) + ")") + " sourcecountry:US sourcelang:english"
        r = self.f.get(GDELT, params={"query": q, "mode": "artlist", "maxrecords": 250, "format": "json", "sort": "datedesc",
                                      "startdatetime": floor.replace("-", "") + "000000"}, use_cache=False)
        arts = r.json().get("articles", []) if r.status == 200 and r.content.strip().startswith(b"{") else []
        pats = {c["id"]: name_regex(c.get("variants") or name_variants(c["name"], c.get("aliases", []))) for c in group}
        places = {c["id"]: (re.compile("|".join(re.escape(p) for p in self._places(c)), re.I) if self._places(c) else None)
                  for c in group}
        n = 0
        for a in arts[: int(self.opts.get("max_articles_per_query", 60))]:
            url, title = a.get("url"), a.get("title", "")
            if not url:
                continue
            pub = a.get("seendate", "")
            pub = f"{pub[:4]}-{pub[4:6]}-{pub[6:8]}" if len(pub) >= 8 else None
            page = self._open_article(url)
            for c in group:
                add = adders[c["id"]]
                if page is None:
                    if pats[c["id"]].search(title):
                        why = (self._opened.get(url + "#why") or {}).get("why", "could not be opened")
                        add(Record("", "lead", url, title=title, published=pub, channel="api", evidence="lead",
                                   meta={"outlet": a.get("domain"), "via": "gdelt", "why": why}))
                    continue
                text = page["text"]
                if not pats[c["id"]].search(text) or (places[c["id"]] and not places[c["id"]].search(text)):
                    continue  # name not on the page, or a same-named person elsewhere
                ex = excerpts(text, pats[c["id"]], n=3)
                add(Record("", "article", page["url"], title=page["title"] or title, text="\n".join(ex)[:1800],
                           published=pub, channel="api",
                           meta={"outlet": a.get("domain"), "via": "gdelt", "opened": True}))
                n += 1
        return n

    def _leads(self, c, cls, add, since):
        """Google News RSS: its links are redirect pages that can't be opened directly, so hits are leads only."""
        terms = list(c.get("search_terms") or [])
        if not terms:
            j = self.targets.get(c.get("target") or "")
            hint = j["name"] if j else " ".join((c.get("places") or self.opts.get("places") or [])[:1])
            terms = [f'"{c["name"]}" {hint}'.strip()]
        pat = name_regex(c.get("variants") or name_variants(c["name"], c.get("aliases", [])))
        feeds = [f"{GNEWS}?q={quote_plus(t)}&hl=en-US&gl=US&ceid=US:en" for t in terms[:2]] + list(c.get("news_feeds", []))
        for feed in feeds:
            try:
                r = self.f.get(feed, use_cache=False)
            except (Disallowed, Blocked):
                continue
            if r.status != 200:
                continue
            for it in parse_feed(r.content)[:30]:
                if not pat.search(it.get("title", "")):
                    continue
                outlet_url = it.get("source_url") or it["link"]
                if cls.excluded(outlet_url):
                    continue
                if "news.google.com" in feed:
                    add(Record("", "lead", it["link"], title=it["title"], published=it["published"], channel="feed",
                               evidence="lead", meta={"outlet": it.get("source") or domain_of(outlet_url),
                                                      "why": "news index link (not opened)", "via": "google_news"}))
                else:  # a feed the user configured: items link straight to articles; open before counting
                    page = self._open_article(it["link"])
                    if page and pat.search(page["text"]):
                        add(Record("", "article", page["url"], title=page["title"] or it["title"],
                                   text="\n".join(excerpts(page["text"], pat, n=3))[:1800], published=it["published"],
                                   channel="feed", meta={"outlet": domain_of(page["url"]), "via": "feed", "opened": True}))

    # ------------------------------------------------------------------ other people's social posts
    def _social_mentions(self, c, cls, add, since):
        cursor, n = None, 0
        floor = max(since[:10], (dt.date.today() - dt.timedelta(days=365)).isoformat())
        while n < 200:
            params = {"q": f'"{c["name"]}"', "limit": 100, "since": floor + "T00:00:00Z"}
            if cursor:
                params["cursor"] = cursor
            r = self.f.get(BSKY_SEARCH, params=params, use_cache=False)
            if r.status != 200:
                return
            data = r.json()
            for post in data.get("posts", []):
                rec_ = post.get("record", {})
                handle = post.get("author", {}).get("handle", "")
                rkey = post.get("uri", "").rsplit("/", 1)[-1]
                url = f"https://bsky.app/profile/{handle}/post/{rkey}"
                add(Record("", "social_mention", url, title=rec_.get("text", "")[:120], text=rec_.get("text", ""),
                           published=rec_.get("createdAt"), channel="api",
                           meta={"platform": "bluesky", "author": handle, "likes": post.get("likeCount"),
                                 "uri": post.get("uri"), "opened": True}))
                n += 1
            cursor = data.get("cursor")
            if not cursor:
                return

    # ------------------------------------------------------------------ money and endorsements
    def _connections(self, c, cls, add, since):
        csv_path = self.root / (c.get("finance_csv") or f"imports/finance/{c['id']}.csv")
        if csv_path.exists():
            for row in parse_finance_csv(csv_path):
                add(Record("", "connection", f"file://{csv_path.name}", title=f"Contribution: {row['name']}",
                           text=json.dumps(row), published=row.get("date"), channel="import", evidence="primary",
                           meta={"type": "contribution", **row,
                                 "filing_source": c.get("finance_source") or "campaign finance export (CSV import)"}))
        if c.get("fec_id"):
            hdr = {"X-Api-Key": os.environ.get("FEC_API_KEY") or "DEMO_KEY"}     # api.data.gov reads the key header
            r = self.f.get(f"{FEC}/committees/", params={"candidate_id": c["fec_id"]}, headers=hdr, use_cache=False)
            for com in (r.json().get("results", []) if r.status == 200 else []):
                page = 1
                while page <= 20:
                    rr = self.f.get(f"{FEC}/schedules/schedule_a/", params={
                        "committee_id": com["committee_id"], "per_page": 100, "page": page,
                        "sort": "-contribution_receipt_date", "min_date": since[:10]}, headers=hdr, use_cache=False)
                    if rr.status != 200:
                        break
                    rows = rr.json().get("results", [])
                    for s in rows:
                        add(Record("", "connection", s.get("pdf_url") or f"{FEC}/schedules/schedule_a/?committee_id={com['committee_id']}",
                                   title=f"Contribution: {s.get('contributor_name')}", text=json.dumps({
                                       "name": s.get("contributor_name"), "amount": s.get("contribution_receipt_amount"),
                                       "date": s.get("contribution_receipt_date"), "employer": s.get("contributor_employer"),
                                       "occupation": s.get("contributor_occupation"), "city": s.get("contributor_city"),
                                       "state": s.get("contributor_state")}),
                                   published=s.get("contribution_receipt_date"), channel="api", evidence="primary",
                                   meta={"type": "contribution", "name": s.get("contributor_name"),
                                         "amount": s.get("contribution_receipt_amount"), "employer": s.get("contributor_employer"),
                                         "date": s.get("contribution_receipt_date"), "committee": com["committee_id"],
                                         "filing_source": "FEC", "opened": True}))
                    if len(rows) < 100:
                        break
                    page += 1


def _profile_inputs(root: Path, store: Store | None = None) -> dict:
    """{subject id: evidence count} from the last `opencrawl profiles` / export (kept in the database)."""
    try:
        raw = store.kv_get("profile_inputs") if store is not None else None
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def parse_finance_csv(path: Path) -> list[dict]:
    """Tolerant reader for campaign-finance exports: finds the contributor, amount, date, employer columns by name."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower(): c for c in reader.fieldnames or []}

        def col(*keys):
            for k in keys:
                for lc, orig in cols.items():
                    if k in lc:
                        return orig
            return None

        name_c, amt_c, date_c = col("contributor name", "contributor", "name", "payee"), col("amount", "total"), col("date")
        emp_c, occ_c, city_c, type_c = col("employer"), col("occupation"), col("city"), col("type", "category")
        for r in reader:
            if not name_c or not r.get(name_c):
                continue
            amt = re.sub(r"[^\d.\-]", "", r.get(amt_c, "") or "") if amt_c else ""
            rows.append({"name": r.get(name_c, "").strip(), "amount": float(amt) if amt else None,
                         "date": (r.get(date_c) or "").strip() if date_c else None,
                         "employer": (r.get(emp_c) or "").strip() if emp_c else "",
                         "occupation": (r.get(occ_c) or "").strip() if occ_c else "",
                         "city": (r.get(city_c) or "").strip() if city_c else "",
                         "kind": (r.get(type_c) or "").strip() if type_c else ""})
    return rows
