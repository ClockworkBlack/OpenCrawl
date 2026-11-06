"""BoardDocs adapter against a local stub of a school board site: committees from the Public page, meetings
and agendas from the POST endpoints, item permalinks, a minutes attachment that feeds the minutes deriver."""
import http.server
import io
import json
import threading
from urllib.parse import parse_qs

import pytest
import requests

from opencrawl import channels, derive
from opencrawl.adapters.boarddocs import BoardDocs, base_of
from opencrawl.store import Store

PUBLIC = """<html><head><title>Testville Board of Education</title></head><body>
<ul id="committees"><li class="committee" id="A1B2C3D4E5">Board of Education</li></ul>
<script>var current_committee_id = "A1B2C3D4E5";</script></body></html>"""

MEETINGS = [{"unique": "M2026OCT01", "name": "Regular Board Meeting", "numberdate": "20261013", "current": 1},
            {"unique": "M2015JAN01", "name": "Regular Board Meeting", "numberdate": "20170112", "current": 0}]

AGENDA = """<div class="agenda">
<h3 class="category">1. Call to Order</h3>
<a id="ITEM00001" class="item">Pledge of Allegiance</a>
<h3 class="category">5. Action Items</h3>
<a id="ITEM00002" class="item">Approval of FY27 Millage Rate (Action)</a>
<a id="ITEM00003" class="item">Approval of Minutes of September 8, 2026</a>
</div>"""

ITEM2 = """<html><head><title>BoardDocs</title></head><body><h1>Approval of FY27 Millage Rate</h1>
<div class="type">Action</div><main><p>The Superintendent recommends the Board approve a millage rate of 18.5 mills for FY27, a rollback from 18.9 mills.</p>
<p>Attachments: <a href="/co/testville/Board.nsf/files/F0000001/$file/Millage%20Resolution.pdf">Millage Resolution.pdf</a></p></main></body></html>"""
ITEM3 = """<html><body><h1>Approval of Minutes of September 8, 2026</h1><main><p>Minutes attached.</p>
<a href="/co/testville/Board.nsf/files/F0000002/$file/Minutes%20September%208%202026.pdf">Minutes September 8 2026.pdf</a></main></body></html>"""

MINUTES_TEXT = ["TESTVILLE BOARD OF EDUCATION MINUTES September 8, 2026",
                "Members present: Chair Dana Reed, Pat Local, Kim Post, Lee Ward, Sam Third.",
                "",
                "Motion by Local, seconded by Post, to adopt the FY27 budget. Motion carried 4-0."]


def _pdf(lines):
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 800
    for ln in lines:
        c.drawString(40, y, ln)
        y -= 18
    c.save()
    return buf.getvalue()


class H(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ct="text/html"):
        self.send_response(200); self.send_header("Content-Type", ct); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        p = self.path
        if p == "/robots.txt":
            return self._send(b"User-agent: *\nAllow: /\n", "text/plain")
        if p.endswith("/Board.nsf/Public"):
            return self._send(PUBLIC.encode())
        if "goto?open&id=ITEM00001" in p:
            return self._send(b"<html><body><h1>Pledge of Allegiance</h1><p>Led by a student from Testville High.</p></body></html>")
        if "goto?open&id=ITEM00002" in p:
            return self._send(ITEM2.encode())
        if "goto?open&id=ITEM00003" in p:
            return self._send(ITEM3.encode())
        if "goto?open&id=M2026OCT01" in p or "goto?open&id=M2015JAN01" in p:
            return self._send(b"<html><body><h1>Regular Board Meeting</h1></body></html>")
        if "F0000002/$file/" in p:
            return self._send(_pdf(MINUTES_TEXT), "application/pdf")
        if "F0000001/$file/" in p:
            return self._send(_pdf(["RESOLUTION setting the FY27 millage rate at 18.5 mills."]), "application/pdf")
        self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = parse_qs(self.rfile.read(n).decode())
        if "BD-GetMeetingsList" in self.path:
            assert data["current_committee_id"] == ["A1B2C3D4E5"]
            return self._send(json.dumps(MEETINGS).encode(), "application/json")
        if "BD-GetAgenda" in self.path:
            return self._send(AGENDA.encode() if data["id"] == ["M2026OCT01"] else b"<div><a id='ITEM00009'>Old item</a></div>")
        self.send_response(404); self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def fetcher(tmp_path):
    s = requests.Session()
    s.trust_env = False
    from opencrawl.fetch import Fetcher

    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def test_base_of():
    assert base_of("https://go.boarddocs.com/co/jeffco/Board.nsf/Public#") == "https://go.boarddocs.com/co/jeffco/Board.nsf"
    assert base_of("https://example.com/x") is None


def test_boarddocs_records_and_minutes_votes(server, fetcher, tmp_path):
    public = server + "/co/testville/Board.nsf/Public"
    bd = BoardDocs(fetcher, public)
    assert bd.committees() == [{"id": "A1B2C3D4E5", "name": "Board of Education"}]
    assert bd.probe() == 2
    store = Store(tmp_path / "data")
    src = {"id": "sd-testville:boarddocs", "url": public, "kind": "meeting", "target": "sd-testville", "max_items": 10}
    ctx = channels.CrawlContext(frontier=store)
    recs = [r for r in channels.crawl(fetcher, src, {"channel": "api", "endpoint": "boarddocs:" + public}, {}, ctx=ctx)
            if not isinstance(r, channels.Checkpoint)]
    for r in recs:
        store.add(r)
    store.flush()
    kinds = sorted(r.kind for r in recs)
    assert kinds.count("meeting") == 2 and kinds.count("agenda_item") == 4 and kinds.count("document") == 2
    meet = next(r for r in recs if r.kind == "meeting" and r.published == "2026-10-13")
    assert meet.meta["EventBodyName"] == "Board of Education" and meet.meta["items"] == 3 and meet.meta["opened_url"].endswith("BD-GetAgenda?open&id=M2026OCT01")
    millage = next(r for r in recs if r.kind == "agenda_item" and "Millage" in r.title)
    assert millage.url.endswith("goto?open&id=ITEM00002") and millage.meta["opened"] and "18.5 mills" in millage.text
    assert millage.meta["category"] == "5. Action Items" and millage.meta["type"] == "Action"
    pledge = next(r for r in recs if r.kind == "agenda_item" and "Pledge" in r.title)
    assert pledge.meta["opened"] and pledge.meta["category"] == "1. Call to Order"      # recent meeting: all items opened
    old = next(r for r in recs if r.kind == "agenda_item" and r.title == "Old item")
    assert not old.meta["opened"] and old.channel == "listed"                            # 2015 item: listed from the agenda only
    mins = next(r for r in recs if r.kind == "document" and "Minutes" in r.title)
    assert mins.meta["doc_type"] == "minutes" and mins.channel == "pdf" and "Motion by Local" in mins.text
    # the minutes deriver turns the attachment into a board vote
    out = derive.run(store, only={"minutes"}, log=lambda *_: None)
    assert out["minutes"]["derived"] == 2
    v = store.query("kind='vote'")[0]
    assert v["target"] == "sd-testville" and {x["person"]: x["vote"] for x in v["meta"]["votes"]} == {
        "Pat Local": "yes", "Kim Post": "yes", "Lee Ward": "yes", "Sam Third": "yes"}
    # a second pass skips the old meeting (stored, outside the lookback) but re-reads the recent one
    recs2 = [r for r in channels.crawl(fetcher, src, {"channel": "api", "endpoint": "boarddocs:" + public}, {},
                                       modified_since="2026-11-01T00:00:00", ctx=channels.CrawlContext(frontier=store))
             if not isinstance(r, channels.Checkpoint)]
    assert sorted(r.kind for r in recs2).count("meeting") == 1 and all(r.published == "2026-10-13" for r in recs2 if r.kind == "meeting")
    store.close()
