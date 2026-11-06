"""Open records requests: contact discovery, planning, drafting, sending (fake SMTP), replies (fake IMAP),
follow-ups, and PDF intake into the Screenshots box. Nothing is ever sent for real."""
import datetime as dt
import email.utils
import io
from email.message import EmailMessage
from pathlib import Path

import pytest

from opencrawl.civic import openrecords as orr
from opencrawl.seed import _official_email, find_records_contact
from opencrawl.store import Record, Store

J = [{"id": "big", "name": "Bigtown", "kind": "city", "population": 50000,
      "records": {"emails": ["records@bigtown.gov"], "page": "https://bigtown.gov/open-records"}},
     {"id": "co-small", "name": "Small County", "kind": "county", "population": 3000,
      "records": {"emails": ["clerk@smallcounty.co.us"], "page": "https://smallcounty.co.us/clerk"}},
     {"id": "portal", "name": "Portalville", "kind": "town", "population": 9000,
      "records": {"portal": "https://portalville.govqa.us/WEBAPP/_rs/", "page": "https://portalville.gov/records"}},
     {"id": "coded", "name": "Codeburg", "kind": "city", "population": 80000, "portals": {"ecode360": "https://ecode360.com/CO1234"},
      "records": {"emails": ["clerk@codeburg.gov"]}},
     {"id": "nocontact", "name": "Quiet", "kind": "town", "population": 70000}]
ENV = {"ORR_SMTP_HOST": "smtp.example", "ORR_USER": "u", "ORR_PASSWORD": "p", "ORR_FROM": "requests@project.example",
       "ORR_IMAP_HOST": "imap.example"}
CFG = {"openrecords": {"enabled": True, "requester": "Civic Records Project", "daily_limit": 10}}


class FakeSMTP:
    sent = []

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


def test_business_days():
    assert orr.business_days_after(dt.date(2026, 11, 6), 3) == dt.date(2026, 11, 11)   # Fri -> Wed
    assert orr.business_days_after(dt.date(2026, 11, 2), 3) == dt.date(2026, 11, 5)    # Mon -> Thu


def test_plan_skips_crawlable_and_contactless_and_orders_by_size(tmp_path):
    items = orr.plan(J, orr.Tracker(tmp_path))
    assert [i["target"]["id"] for i in items] == ["big", "portal", "co-small"]
    assert [i["how"] for i in items] == ["email", "portal", "email"]


def test_draft_wording():
    law = orr.law_settings({"openrecords": {"law": "the Colorado Open Records Act", "citation": "C.R.S. § 24-72-201 et seq.",
                                            "response_days": 3}})
    subj, body = orr.draft(J[0], "Civic Records Project", "requests@project.example", law)
    assert "[ORR-big]" in subj and "City of Bigtown" in body and "24-72-201" in body and "$25.00" in body
    assert "three business days" in body
    generic = orr.draft(J[0], "X")[1]
    assert "applicable public records law" in generic and "ten business days" in generic
    assert body.rstrip().endswith("Reply to: requests@project.example")
    assert "Small County" in orr.draft(J[1], "X")[1]


def test_disabled_means_dry_run(tmp_path):
    FakeSMTP.sent = []
    r = orr.Requests(tmp_path, {"openrecords": {"enabled": False, "requester": "X"}}, orr.Mailbox(ENV), log=lambda *_: None)
    out = r.send_batch(J, smtp_factory=FakeSMTP)
    assert out["dry_run"] and out["sent"] == 0 and FakeSMTP.sent == [] and "enabled is false" in out["why_not_sending"]
    r2 = orr.Requests(tmp_path, CFG, orr.Mailbox({}), log=lambda *_: None)                    # no mailbox secrets
    assert not r2.enabled and "mailbox" in r2.why_not()


def test_send_limit_cooldown_and_portal(tmp_path):
    FakeSMTP.sent = []
    r = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV), log=lambda *_: None)
    out = r.send_batch(J, limit=1, smtp_factory=FakeSMTP)
    assert out["sent"] == 1 and out["portal_only"] == 1 and len(FakeSMTP.sent) == 1
    m = FakeSMTP.sent[0]
    assert m["To"] == "records@bigtown.gov" and "Civic Records Project" in m["From"]
    t = r.tracker.data["big"]
    assert t["status"] == "sent" and t["due"] and t["message_id"] == m["Message-ID"]
    assert r.tracker.data["portal"]["status"] == "portal_only"
    out2 = r.send_batch(J, limit=1, smtp_factory=FakeSMTP)                                    # daily limit reached
    assert out2["sent"] == 0
    out3 = r.send_batch(J, limit=5, smtp_factory=FakeSMTP)
    assert [x["To"] for x in FakeSMTP.sent] == ["records@bigtown.gov", "clerk@smallcounty.co.us"]   # never twice
    assert out3["sent"] == 1


def test_smtp_error_stops_the_batch(tmp_path):
    class Boom(FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection refused")
    r = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV), log=lambda *_: None)
    out = r.send_batch(J, smtp_factory=Boom)
    assert out["errors"] == 1 and out["sent"] == 0 and r.tracker.data["big"]["status"] == "error"


def _reply(to_msg, body, attach=None, frm="Clerk <records@bigtown.gov>", subject=None):
    m = EmailMessage()
    m["From"], m["Subject"] = frm, subject or "RE: " + to_msg["Subject"]
    m["In-Reply-To"] = to_msg["Message-ID"]
    m["Message-ID"] = email.utils.make_msgid()
    m.set_content(body)
    if attach:
        m.add_attachment(attach, maintype="application", subtype="pdf", filename="Code of Ordinances.pdf")
    return m


class FakeIMAP:
    msgs = []

    def login(self, u, p):
        pass

    def select(self, *a, **k):
        pass

    def search(self, *a):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(FakeIMAP.msgs)))]

    def fetch(self, i, what):
        return "OK", [(b"", FakeIMAP.msgs[int(i) - 1].as_bytes())]

    def logout(self):
        pass


def _pdf():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "PDF")
    return buf.getvalue()


def test_replies_attachments_bounces_and_follow_ups(tmp_path):
    FakeSMTP.sent = []
    r = orr.Requests(tmp_path, CFG, orr.Mailbox(ENV), log=lambda *_: None)
    r.send_batch(J, smtp_factory=FakeSMTP)
    to_big, to_small = FakeSMTP.sent
    FakeIMAP.msgs = [_reply(to_big, "Attached is our current code.", attach=_pdf()),
                     _reply(to_small, "Delivery failed: mailbox unavailable", frm="Mail Delivery Subsystem <MAILER-DAEMON@x>",
                            subject="Undeliverable: " + to_small["Subject"]),
                     _reply(to_big, "unrelated", frm="someone@else", subject="Lunch?")]
    FakeIMAP.msgs[2].replace_header("In-Reply-To", "<nothing@x>")
    out = r.check_replies(imap_factory=FakeIMAP, attachments_dir=tmp_path / "inbox")
    assert out == {"checked": 3, "matched": 2, "attachments": 1}
    assert r.tracker.data["big"]["status"] == "fulfilled" and r.tracker.data["co-small"]["status"] == "bounced"
    saved = list((tmp_path / "inbox").iterdir())
    assert [p.name for p in saved] == ["big__Code_of_Ordinances.pdf"]
    assert r.check_replies(imap_factory=FakeIMAP, attachments_dir=tmp_path / "inbox")["matched"] == 0   # not twice
    # cost estimate classification
    FakeSMTP.sent = []
    r2 = orr.Requests(tmp_path / "b", CFG, orr.Mailbox(ENV), log=lambda *_: None)
    r2.send_batch(J[:1], smtp_factory=FakeSMTP)
    FakeIMAP.msgs = [_reply(FakeSMTP.sent[0], "The estimated cost for copies is $45.00. Please confirm.")]
    r2.check_replies(imap_factory=FakeIMAP)
    assert r2.tracker.data["big"]["status"] == "cost_estimate"
    # follow-up after the deadline, once, in the same thread
    r3 = orr.Requests(tmp_path / "c", CFG, orr.Mailbox(ENV), log=lambda *_: None)
    FakeSMTP.sent = []
    r3.send_batch(J[:1], smtp_factory=FakeSMTP)
    r3.tracker.data["big"]["sent_at"] = "2026-10-01T12:00:00+00:00"
    assert r3.follow_ups(smtp_factory=FakeSMTP)["sent"] == 1
    fu = FakeSMTP.sent[-1]
    assert fu["Subject"].startswith("Re: ") and fu["In-Reply-To"] == r3.tracker.data["big"]["message_id"]
    assert r3.follow_ups(smtp_factory=FakeSMTP)["sent"] == 0


def test_official_email_and_contact_discovery():
    assert _official_email("records@bigtown.gov", "https://www.bigtown.gov/")
    assert _official_email("clerk@cityofx.com", "https://www.cityofx.com/")
    assert not _official_email("someone@gmail.com", "https://www.cityofx.com/")

    class F:
        def get(self, url, **k):
            class R:
                status, content_type, url_ = 200, "text/html", url
            r = R()
            r.url = url
            r.text = ("<html><body>Contact the clerk: <a href='mailto:records@cityofx.com'>records</a> "
                      "or webmaster@gmail.com. Also cityclerk@cityofx.com</body></html>")
            return r
    home = "<html><body><a href='/open-records'>Open Records Requests</a> <a href='/parks'>Parks</a></body></html>"
    rc = find_records_contact(F(), home, "https://www.cityofx.com/")
    assert rc["emails"] == ["records@cityofx.com", "cityclerk@cityofx.com"] and rc["page"].endswith("/open-records")
    home2 = "<html><body><a href='https://cityofy.govqa.us/WEBAPP/_rs/'>Public Records Request</a></body></html>"
    assert find_records_contact(F(), home2, "https://cityofy.gov/")["portal"].startswith("https://cityofy.govqa.us")


def test_pdf_goes_into_ordinances_box(tmp_path):
    pytest.importorskip("pdfplumber")
    from opencrawl import screens
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")
    d = tmp_path / "imports" / "screens"
    d.mkdir(parents=True)
    c = canvas.Canvas(str(d / "rifle__code.pdf"))
    c.drawString(72, 720, "CODE OF ORDINANCES")
    c.drawString(72, 700, "Sec. 18-3. Short-term rentals. No dwelling may be rented without a permit.")
    c.save()
    store = Store(tmp_path / "data")
    jur = [{"id": "rifle", "name": "Rifle", "kind": "city"}]
    screens.configure({"state_abbr": "CO", "state_name": "Colorado"})
    stats = screens.Inbox(tmp_path, store, [], log=lambda *_: None, targets=jur).ingest()
    box = screens.Inbox.box_export(store)
    assert stats["new"] == 1 and len(box["ordinances"]) == 1
    item = box["ordinances"][0]
    assert item["targets"] == ["rifle"] and "Short-term rentals" in item["text"]
    store.close()
