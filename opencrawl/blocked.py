"""What happens when a site won't let crawlers in.

Some hosts (Municode's code library, for one) sit behind bot protection that
answers every automated request with a challenge page. The crawler does not
try to defeat that; it would be unauthorized access, and it would get the
project's addresses banned everywhere the same protection is used. Instead:

  1. the host goes on a block list (state/blocked.json) for `ttl_days`, so the
     crawler stops hitting it immediately and spends the budget elsewhere;
  2. each blocked source is re-routed to the next best place the same records
     live (ALTERNATIVES below), when one is known;
  3. what can't be re-routed becomes a manual task (`opencrawl tasks`): a short
     list of URLs to capture by hand and feed to `opencrawl ocr`, plus (for
     government targets) a ready-to-send public records request for the clerk,
     since a code of ordinances is a public record the government must provide.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path
from urllib.parse import urlsplit

CHALLENGE_MARKERS = (
    "cf-chl", "challenge-platform", "cf-browser-verification", "Access blocked",
    "Just a moment...", "__cf_chl", "Attention Required! | Cloudflare", "px-captcha", "_Incapsula_Resource",
    "distil_r_captcha", "Request unsuccessful. Incapsula", "awswaf", "DataDome",
)

# where else the same records live, by blocked host
ALTERNATIVES = {
    "library.municode.com": [
        "the target's own website (many post the code or recent ordinances as PDFs; the :site source picks those up)",
        "eCode360 or American Legal, if the target also publishes there (the seed step records those links)",
        "an open records request to the city/county clerk for the current code (template in data/manual_tasks.md)",
        "manual capture: open the Municode page in a browser, save/print to PDF, run `opencrawl ocr` on it",
    ],
    "api.municode.com": ["same as library.municode.com"],
    "query.wikidata.org": ["run `opencrawl seed` from the crawler itself (its honest User-Agent is what Wikimedia asks for); "
                           "requests through shared/rotating proxies are refused"],
}

RECORDS_REQUEST = """Subject: Public Records Request - current Code of Ordinances ({name})

To the Records Custodian / Clerk, {name}:

Under {law}{citation}, I request an electronic copy of the current, complete
Code of Ordinances of {name}, including the charter, all codified ordinances
and any ordinances adopted since the last codification supplement. A PDF or
Word file is preferred; a link to a download is fine. If any part of this
request will cost more than $25 to fulfill, please let me know before
proceeding.

{deadline_line}Thank you.

{requester}
"""


class Blocked(Exception):
    def __init__(self, host: str, reason: str):
        super().__init__(f"{host}: {reason}")
        self.host, self.reason = host, reason


def looks_blocked(status: int, body: bytes | str) -> str | None:
    """Reason string if this response is a bot-protection challenge, else None."""
    text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else body
    head = text[:6000]
    if status in (401, 403, 429, 503) and any(m in head for m in CHALLENGE_MARKERS):
        return f"HTTP {status} bot-protection challenge"
    if status == 403 and len(head) < 2000 and re.search(r"access (denied|blocked)|forbidden", head, re.I):
        return "HTTP 403 access denied"
    return None


class BlockList:
    def __init__(self, path: Path, ttl_days: int = 7):
        self.path = Path(path)
        self.ttl = dt.timedelta(days=ttl_days)
        self._lock = threading.Lock()
        self.data: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}

    @staticmethod
    def host(url: str) -> str:
        return urlsplit(url).netloc.lower()

    def is_blocked(self, url: str) -> dict | None:
        h = self.host(url)
        rec = self.data.get(h)
        if not rec:
            return None
        if dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(rec["last_seen"]) > self.ttl:
            return None  # expired: try again
        return rec

    def add(self, url: str, reason: str, source_id: str | None = None) -> dict:
        h = self.host(url)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        with self._lock:
            rec = self.data.setdefault(h, {"first_seen": now, "count": 0, "urls": [], "sources": []})
            rec.update(reason=reason, last_seen=now, count=rec["count"] + 1)
            if url not in rec["urls"]:
                rec["urls"] = (rec["urls"] + [url])[-20:]
            if source_id and source_id not in rec["sources"]:
                rec["sources"].append(source_id)
            rec["alternatives"] = ALTERNATIVES.get(h.removeprefix("www."), [
                "check whether the site offers an API, RSS feed or bulk download (the discover step tries these)",
                "manual capture + `opencrawl ocr`, or an open records request (template in data/manual_tasks.md)"])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=2))
        return rec

    def note_source(self, host: str, source_id: str):
        with self._lock:
            rec = self.data.get(host)
            if rec is not None and source_id not in rec["sources"]:
                rec["sources"].append(source_id)
                self.path.write_text(json.dumps(self.data, indent=2))

    def clear(self, host: str):
        with self._lock:
            if self.data.pop(host, None) is not None:
                self.path.write_text(json.dumps(self.data, indent=2))


def records_request_template(name: str = "<jurisdiction>", requester: str = "[your name / organization]",
                             law: dict | None = None) -> str:
    """The request letter with the configured law filled in (settings.yaml `openrecords:` law / citation / response_days)."""
    law = law or {}
    days = int(law.get("response_days") or 0)
    deadline_line = f"The law asks for a response within {days} business day{'s' if days != 1 else ''}. " if days else ""
    return RECORDS_REQUEST.format(name=name, requester=requester, law=law.get("law") or "the applicable public records law",
                                  citation=f" ({law['citation']})" if law.get("citation") else "", deadline_line=deadline_line)


def manual_tasks_md(blocklist: BlockList, targets: list[dict], requester: str = "[your name / organization]",
                    law: dict | None = None) -> str:
    """Markdown checklist of what needs a human: blocked code libraries, targets with no website, etc."""
    jurisdictions = [t for t in targets if t.get("fips") or t.get("kind") in
                     ("county", "city", "town", "consolidated", "school_district", "village", "borough", "township")]
    lines = ["# Manual tasks", "", "Generated by `opencrawl tasks`. Things the crawler can't do by itself.", ""]
    if blocklist.data:
        lines += ["## Blocked hosts", ""]
        for host, rec in sorted(blocklist.data.items()):
            lines.append(f"### {host}")
            lines.append(f"- reason: {rec['reason']} (seen {rec['count']}x, last {rec['last_seen'][:10]})")
            lines.append("- what to do instead:")
            lines += [f"  - {a}" for a in rec.get("alternatives", [])]
            if rec.get("sources"):
                lines.append("- affected sources: " + ", ".join(rec["sources"]))
            if rec.get("urls"):
                lines.append("- capture by hand, then `opencrawl ocr <file> --source <target> --kind document --url <url>`:")
                lines += [f"  - {u}" for u in rec["urls"]]
            lines.append("")
    municode_only = [j for j in jurisdictions
                     if not any(j.get("portals", {}).get(k) for k in ("ecode360", "amlegal")) and j.get("municode")]
    if municode_only:
        lines += ["## Codes of ordinances (Municode)", "",
                  f"Municode blocks crawlers. {len(municode_only)} jurisdictions have a confirmed Municode code link and no "
                  "other code library on record. Capture by hand (`opencrawl ocr`) or send the records request below. "
                  "Jurisdictions not listed here have no confirmed code link yet; `opencrawl seed` records one when the "
                  "official website links to it.", ""]
        for j in sorted(municode_only, key=lambda x: -(x.get("population") or 0)):
            lines.append(f"- [ ] {j['name']} (pop. {j.get('population') or '?'}): {j['municode']}")
        lines.append("")
    no_site = [t for t in targets if not t.get("website") and t.get("enabled") is not False]
    if no_site and not any(t.get("checked_at") for t in targets):
        lines += ["## No websites yet", "", f"None of the {len(targets)} targets has been seeded: run `opencrawl seed` first.", ""]
    elif no_site:
        lines += ["## No website found", "", "Add one in config/overrides.yaml (or targets.yaml) if it exists.", ""]
        lines += [f"- [ ] {t['name']} ({t.get('kind', 'target')}" + (f", pop. {t.get('population') or '?'}" if t.get("population") else "") + ")"
                  for t in no_site]
        lines.append("")
    if jurisdictions:
        lines += ["## Public records request template", "", "```", records_request_template(requester=requester, law=law), "```"]
    return "\n".join(lines)
