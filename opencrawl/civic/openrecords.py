"""Public records requests (FOIA / state open-records laws) for codes of ordinances, sent from a project mailbox.

The law cited, its response deadline and the request letter come from settings.yaml `openrecords:` (law, citation,
response_days, template); the defaults ask under "the applicable public records law". The Colorado example
project sets the Colorado Open Records Act, its citation and three working days.

plan       which jurisdictions to ask: the code isn't crawlable (no eCode360 / American Legal source and no
           ordinance records yet), a records email was found on an official page, and no request in the
           cool-down period. Largest first.
draft      one request per jurisdiction, citing the configured law, signed with the project name.
send       a small daily batch over SMTP. Off unless `openrecords.enabled` is true AND the mailbox secrets
           exist. Never to a guessed address; never twice inside the cool-down; stops for the day on a bounce
           or SMTP error.
check      reads replies over IMAP (matched by the [ORR-<id>] tag in the subject or the thread headers):
           acknowledgement, cost estimate, records attached (PDFs go to imports/screens/ordinances/ and into the
           Screenshots box) or linked, bounce, denial.
follow-up  one polite follow-up in the same thread when the response deadline has passed by a few days.

Mailbox secrets (environment): ORR_SMTP_HOST, ORR_SMTP_PORT (465 SSL or 587 STARTTLS), ORR_USER, ORR_PASSWORD,
ORR_FROM (the project address), ORR_IMAP_HOST (optional, for replies; port 993).
Tracker: data/openrecords/requests.json (committed, so the history is visible in the repo).
"""
from __future__ import annotations

import datetime as dt
import email
import email.utils
import imaplib
import json
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.header import decode_header, make_header
from pathlib import Path

SUBJECT = "Public Records Request: current Code of Ordinances [ORR-{jid}]"
BODY = """To the Records Custodian, {name}:

Under {law}{citation}, I request an electronic copy of:

1. The current, complete Code of Ordinances of {name}, including the charter and all codified ordinances; and
2. Any ordinances adopted since the most recent codification supplement that are not yet included in it.

An electronic copy (PDF or Word) by email is preferred, or a link to download it. If the code is already
available online in full, a link to that copy will satisfy this request.

If the cost of fulfilling this request will exceed {cost_cap}, please send an estimate before proceeding.

{deadline_line}Thank you for your help.

{requester}
{reply_line}"""
FOLLOW_UP = """To the Records Custodian, {name}:

I am following up on the public records request below, sent {sent_on}, for an electronic copy of the current
Code of Ordinances of {name}. I have not yet received a response. Could you let me know its status?

Thank you,
{requester}
"""
LAW_DEFAULTS = {"law": "the applicable public records law", "citation": "", "response_days": 10, "cost_cap": "$25.00",
                "subject": SUBJECT, "template": BODY}


def law_settings(cfg: dict | None) -> dict:
    """The request's legal framing from settings.yaml `openrecords:`."""
    o = ((cfg or {}).get("openrecords") or {})
    out = dict(LAW_DEFAULTS)
    out.update({k: v for k, v in o.items() if k in LAW_DEFAULTS and v not in (None, "")})
    return out
STATUSES = ("planned", "sent", "acknowledged", "cost_estimate", "fulfilled", "partial", "denied", "bounced",
            "followed_up", "portal_only", "error")


def business_days_after(d: dt.date, n: int) -> dt.date:
    """n business days after d (weekends skipped; state holidays not counted, so this can be a day early)."""
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Tracker:
    def __init__(self, root: Path):
        self.path = root / "data" / "openrecords" / "requests.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        tmp.replace(self.path)

    def get(self, jid: str) -> dict:
        return self.data.setdefault(jid, {"target": jid, "history": []})

    def log(self, jid: str, status: str, note: str = "", **fields):
        r = self.get(jid)
        r.update(fields)
        r["status"] = status
        r["updated_at"] = _now()
        r["history"].append({"at": r["updated_at"], "status": status, "note": note[:500]})


def crawlable_code(j: dict, store=None) -> bool:
    if any(j.get("portals", {}).get(k) for k in ("ecode360", "amlegal")):
        return True
    if store is not None:
        with store._lock:
            n = store.db.execute("SELECT COUNT(*) FROM records WHERE target=? AND kind='ordinance'", (j["id"],)).fetchone()[0]
        return n >= 25  # a real code, not a stray ordinance or two
    return False


def plan(jurisdictions: list[dict], tracker: Tracker, store=None, cooldown_days: int = 180) -> list[dict]:
    """Jurisdictions due a request, largest first, each with how it would be sent."""
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for j in sorted(jurisdictions, key=lambda j: -(j.get("population") or 0)):
        if j.get("enabled") is False or crawlable_code(j, store):
            continue
        t = tracker.data.get(j["id"], {})
        if t.get("sent_at") and (now - dt.datetime.fromisoformat(t["sent_at"])).days < cooldown_days:
            continue
        if t.get("status") in ("fulfilled", "denied") and t.get("updated_at") and \
                (now - dt.datetime.fromisoformat(t["updated_at"])).days < cooldown_days:
            continue
        rc = j.get("records") or {}
        if rc.get("emails"):
            out.append({"target": j, "to": rc["emails"][0], "how": "email", "found_on": rc.get("page")})
        elif rc.get("portal"):
            out.append({"target": j, "to": rc["portal"], "how": "portal", "found_on": rc.get("page")})
    return out


def draft(j: dict, requester: str, reply_to: str = "", law: dict | None = None) -> tuple[str, str]:
    kind = j.get("kind", "city")
    name = j["name"] if kind == "county" else (j["name"] if j["name"].lower().startswith(("city", "town"))
                                               else f"the City of {j['name']}" if kind != "town"
                                               else f"the Town of {j['name']}")
    if kind == "consolidated":
        name = j.get("official_name") or f"{j['name']}-{(j.get('consolidated_with') or '').replace(' County', '')} " \
                                           "consolidated government"
    law = law or dict(LAW_DEFAULTS)
    days = int(law.get("response_days") or 0)
    words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 7: "seven", 10: "ten", 14: "fourteen", 20: "twenty", 30: "thirty"}
    deadline_line = (f"The law asks for a response within {words.get(days, str(days))} business day{'s' if days != 1 else ''}. "
                     if days else "")
    subject = law["subject"].format(jid=j["id"], name=name)
    body = law["template"].format(name=name, requester=requester, law=law["law"],
                                  citation=f", {law['citation']}" if law.get("citation") else "",
                                  cost_cap=law.get("cost_cap") or "$25.00", deadline_line=deadline_line,
                                  reply_line=f"Reply to: {reply_to}" if reply_to else "")
    return subject, body.strip() + "\n"


class Mailbox:
    def __init__(self, env: dict | None = None):
        e = env or os.environ
        self.smtp_host, self.user, self.password = e.get("ORR_SMTP_HOST"), e.get("ORR_USER"), e.get("ORR_PASSWORD")
        self.port = int(e.get("ORR_SMTP_PORT") or 465)
        self.sender = e.get("ORR_FROM") or self.user
        self.imap_host = e.get("ORR_IMAP_HOST")

    @property
    def can_send(self) -> bool:
        return bool(self.smtp_host and self.user and self.password and self.sender)

    def send(self, msg: EmailMessage, smtp_factory=None):
        ctx = ssl.create_default_context()
        if smtp_factory:
            s = smtp_factory()
        elif self.port == 465:
            s = smtplib.SMTP_SSL(self.smtp_host, self.port, context=ctx, timeout=60)
        else:
            s = smtplib.SMTP(self.smtp_host, self.port, timeout=60)
            s.starttls(context=ctx)
        try:
            s.login(self.user, self.password)
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass

    def fetch_replies(self, since: dt.date, imap_factory=None) -> list[email.message.Message]:
        if not (self.imap_host or imap_factory):
            return []
        m = imap_factory() if imap_factory else imaplib.IMAP4_SSL(self.imap_host, 993)
        try:
            m.login(self.user, self.password)
            m.select("INBOX", readonly=True)
            typ, ids = m.search(None, "SINCE", since.strftime("%d-%b-%Y"))
            out = []
            for i in (ids[0].split() if ids and ids[0] else [])[-500:]:
                typ, data = m.fetch(i, "(RFC822)")
                if data and data[0]:
                    out.append(email.message_from_bytes(data[0][1]))
            return out
        finally:
            try:
                m.logout()
            except Exception:  # noqa: BLE001
                pass


def _decode(s) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:  # noqa: BLE001
        return s or ""


def _text(msg: email.message.Message) -> str:
    parts = []
    for p in (msg.walk() if msg.is_multipart() else [msg]):
        if p.get_content_type() == "text/plain" and not p.get_filename():
            try:
                parts.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(parts)


class Requests:
    def __init__(self, root: Path, cfg: dict, mailbox: Mailbox | None = None, log=print):
        self.root, self.log = root, log
        self.opts = (cfg or {}).get("openrecords", {})
        self.tracker = Tracker(root)
        self.mailbox = mailbox or Mailbox()
        self.requester = self.opts.get("requester") or (cfg or {}).get("requester") or ""
        self.law = law_settings(cfg)

    @property
    def enabled(self) -> bool:
        return bool(self.opts.get("enabled")) and self.mailbox.can_send and bool(self.requester) and \
            not self.requester.startswith("[")

    def why_not(self) -> str:
        if not self.opts.get("enabled"):
            return "openrecords.enabled is false in config/settings.yaml"
        if not self.requester or self.requester.startswith("["):
            return "openrecords.requester (the name requests are signed with) is not set"
        if not self.mailbox.can_send:
            return "mailbox secrets missing (ORR_SMTP_HOST, ORR_USER, ORR_PASSWORD, ORR_FROM)"
        return ""

    def send_batch(self, jurisdictions: list[dict], store=None, limit: int | None = None, dry_run: bool = False,
                   smtp_factory=None) -> dict:
        limit = int(limit if limit is not None else self.opts.get("daily_limit", 10))
        cooldown = int(self.opts.get("cooldown_days", 180))
        today = dt.datetime.now(dt.timezone.utc).date().isoformat()  # sent_at is UTC; compare like with like
        sent_today = sum(1 for t in self.tracker.data.values() if (t.get("sent_at") or "")[:10] == today)
        due = plan(jurisdictions, self.tracker, store, cooldown)
        out = {"due": len(due), "sent": 0, "portal_only": 0, "dry_run": dry_run or not self.enabled, "errors": 0}
        if not dry_run and not self.enabled:
            out["why_not_sending"] = self.why_not()
        for item in due:
            j = item["target"]
            if item["how"] == "portal":
                if self.tracker.data.get(j["id"], {}).get("status") != "portal_only":
                    self.tracker.log(j["id"], "portal_only", f"submit by hand at {item['to']}", portal=item["to"],
                                     found_on=item["found_on"])
                out["portal_only"] += 1
                continue
            if sent_today + out["sent"] >= limit:
                break
            subject, body = draft(j, self.requester, self.mailbox.sender or "", self.law)
            if out["dry_run"]:
                self.log(f"[dry run] {j['id']} -> {item['to']} : {subject}")
                continue
            msg = EmailMessage()
            msg["From"] = email.utils.formataddr((self.requester, self.mailbox.sender))
            msg["To"] = item["to"]
            msg["Subject"] = subject
            msg["Date"] = email.utils.formatdate(localtime=False)
            mid = email.utils.make_msgid(domain=(self.mailbox.sender or "orr.local").split("@")[-1])
            msg["Message-ID"] = mid
            msg.set_content(body)
            try:
                self.mailbox.send(msg, smtp_factory)
            except Exception as exc:  # noqa: BLE001 — stop for today; don't hammer a failing server
                self.tracker.log(j["id"], "error", f"{type(exc).__name__}: {exc}")
                out["errors"] += 1
                break
            sent = dt.datetime.now(dt.timezone.utc)
            self.tracker.log(j["id"], "sent", f"to {item['to']}", to=item["to"], found_on=item["found_on"],
                             subject=subject, message_id=mid, sent_at=sent.replace(microsecond=0).isoformat(),
                             due=business_days_after(sent.date(), int(self.law.get("response_days") or 10)).isoformat(),
                             name=j["name"])
            out["sent"] += 1
            self.log(f"sent {j['id']} -> {item['to']}")
        self.tracker.save()
        return out

    def check_replies(self, imap_factory=None, attachments_dir: Path | None = None) -> dict:
        sent = {t["message_id"]: jid for jid, t in self.tracker.data.items() if t.get("message_id")}
        if not sent:
            return {"checked": 0}
        since = min(dt.date.fromisoformat(t["sent_at"][:10]) for t in self.tracker.data.values() if t.get("sent_at"))
        msgs = self.mailbox.fetch_replies(since - dt.timedelta(days=1), imap_factory)
        attachments_dir = attachments_dir or self.root / "imports" / "screens" / "ordinances"
        out = {"checked": len(msgs), "matched": 0, "attachments": 0}
        for m in msgs:
            subj = _decode(m.get("Subject"))
            refs = f"{m.get('In-Reply-To', '')} {m.get('References', '')}"
            tag = re.search(r"\[ORR-([a-z0-9-]+)\]", subj)
            jid = tag.group(1) if tag else next((v for k, v in sent.items() if k in refs), None)
            frm = _decode(m.get("From")).lower()
            body = _text(m)
            if not jid and ("mailer-daemon" in frm or "postmaster" in frm):
                jid = next((v for k, v in sent.items() if k in body), None)
            if not jid or jid not in self.tracker.data:
                continue
            mid = m.get("Message-ID", "")
            t = self.tracker.data[jid]
            if mid and mid in {r.get("message_id") for r in t.get("replies", [])}:
                continue
            out["matched"] += 1
            files = []
            for p in m.walk():
                fn = p.get_filename()
                if fn and fn.lower().endswith((".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg")):
                    attachments_dir.mkdir(parents=True, exist_ok=True)
                    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{jid}__{_decode(fn)}")[:150]
                    (attachments_dir / safe).write_bytes(p.get_payload(decode=True) or b"")
                    files.append(safe)
            links = re.findall(r"https?://[^\s<>\")]+", body)[:10]
            low = body.lower()
            if "mailer-daemon" in frm or "postmaster" in frm or "undeliverable" in subj.lower() or "delivery status" in subj.lower():
                status = "bounced"
            elif files:
                status = "fulfilled"
            elif re.search(r"\$\s?\d", body) and re.search(r"cost|fee|estimate|charge|invoice", low):
                status = "cost_estimate"
            elif re.search(r"\b(denied|deny|exempt|no responsive records|do not have)\b", low):
                status = "denied"
            elif links and re.search(r"code|ordinance|download|link", low):
                status = "partial"
            else:
                status = "acknowledged"
            t.setdefault("replies", []).append({"at": _now(), "from": frm[:120], "subject": subj[:200], "message_id": mid,
                                                "status": status, "files": files, "links": links, "excerpt": body[:800]})
            self.tracker.log(jid, status, f"reply from {frm[:80]}", **({"files": t.get("files", []) + files} if files else {}))
            out["attachments"] += len(files)
        self.tracker.save()
        return out

    def follow_ups(self, smtp_factory=None, after_business_days: int | None = None) -> dict:
        n = int(after_business_days if after_business_days is not None else self.opts.get("follow_up_after_business_days", 5))
        today = dt.datetime.now(dt.timezone.utc).date()
        out = {"due": 0, "sent": 0}
        for jid, t in self.tracker.data.items():
            if t.get("status") != "sent" or t.get("followed_up_at") or not t.get("sent_at"):
                continue
            if today < business_days_after(dt.date.fromisoformat(t["sent_at"][:10]), n):
                continue
            out["due"] += 1
            if not self.enabled:
                continue
            msg = EmailMessage()
            msg["From"] = email.utils.formataddr((self.requester, self.mailbox.sender))
            msg["To"] = t["to"]
            msg["Subject"] = "Re: " + t["subject"]
            msg["In-Reply-To"] = msg["References"] = t["message_id"]
            msg["Message-ID"] = email.utils.make_msgid(domain=(self.mailbox.sender or "orr.local").split("@")[-1])
            msg.set_content(FOLLOW_UP.format(name=t.get("name", jid), sent_on=t["sent_at"][:10], requester=self.requester))
            try:
                self.mailbox.send(msg, smtp_factory)
            except Exception as exc:  # noqa: BLE001
                self.tracker.log(jid, "sent", f"follow-up failed: {type(exc).__name__}: {exc}")
                break
            self.tracker.log(jid, "followed_up", "no response after the deadline", followed_up_at=_now())
            out["sent"] += 1
        self.tracker.save()
        return out

    def summary(self) -> dict:
        from collections import Counter

        today = dt.datetime.now(dt.timezone.utc).date().isoformat()
        overdue = [jid for jid, t in self.tracker.data.items() if t.get("status") == "sent" and (t.get("due") or "9") < today]
        return {"by_status": dict(Counter(t.get("status") for t in self.tracker.data.values())), "overdue": overdue,
                "portal_only": [(jid, t.get("portal")) for jid, t in self.tracker.data.items() if t.get("status") == "portal_only"]}


def discover_contacts(fetcher, jurisdictions: list[dict], state_path: Path, deadline: float | None = None,
                      log=print) -> int:
    """Fill in records contacts for jurisdictions already seeded (one or two requests each)."""
    import time

    from ..blocked import Blocked
    from ..fetch import Disallowed
    from .seed import find_records_contact

    disc = json.loads(state_path.read_text()) if state_path.exists() else {}
    n = 0
    for j in sorted(jurisdictions, key=lambda j: -(j.get("population") or 0)):
        d = disc.get(j["id"])
        if not d or not d.get("website") or "records" in d or (deadline and time.monotonic() > deadline):
            continue
        try:
            r = fetcher.get(d["website"])
            rc = find_records_contact(fetcher, r.text, r.url) if r.status == 200 and "html" in r.content_type else None
        except (Disallowed, Blocked, Exception):  # noqa: BLE001
            rc = None
        d["records"] = rc or {}
        n += 1
        if rc:
            log(f"{j['id']}: {rc.get('emails') or rc.get('portal')}")
        if n % 20 == 0:
            state_path.write_text(json.dumps(disc, indent=1, ensure_ascii=False))
    state_path.write_text(json.dumps(disc, indent=1, ensure_ascii=False))
    return n
