"""Screenshots: simulator -> inbox (boxes, chrome stripping, dark mode, duplicates, scroll merging, places,
candidates), capture rules, helpers. Synthetic images and fictional people only."""
import http.server
import json
import shutil
import threading
from pathlib import Path

import pytest
import requests
import yaml

from opencrawl import screens
from opencrawl.blocked import Blocked
from opencrawl.fetch import Disallowed, Fetcher
from opencrawl.names import name_variants
from opencrawl.store import Store

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "examples" / "colorado-civic" / "config" / "jurisdictions.yaml"
needs_ocr = pytest.mark.skipif(not shutil.which("tesseract"), reason="tesseract not installed")
_REG = yaml.safe_load(REGISTRY.read_text())
JUR = _REG["counties"] + _REG["municipalities"]


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    root = tmp_path_factory.mktemp("proj")
    inbox = root / "imports" / "screens"
    screens.simulate(inbox)
    (inbox / "ordinances").mkdir()
    (inbox / "desktop_notice.png").rename(inbox / "ordinances" / "desktop_notice.png")
    store = Store(root / "data")
    cands = [{"id": "casey-morgan", "variants": name_variants("Casey Morgan")}]
    stats = screens.Inbox(root, store, cands, log=lambda *_: None, targets=JUR).ingest()
    yield root, store, stats
    store.close()


@needs_ocr
def test_inbox_stats_duplicates_and_merge(ingested):
    root, store, stats = ingested
    assert stats == {"files": 7, "new": 5, "duplicates": 1, "merged_into_previous": 1, "errors": 0}
    assert not [p for p in (root / "imports" / "screens").glob("*.png")]                      # inbox emptied
    assert (root / "imports" / "screens" / "processed" / "ordinances" / "desktop_notice.png").exists()
    assert json.loads((root / "state" / "screens_seen.json").read_text())                     # dedupe memory kept


@needs_ocr
def test_boxes_platforms_and_review_status(ingested):
    _, store, _ = ingested
    box = screens.Inbox.box_export(store)
    assert len(box["ordinances"]) == 1 and len(box["posts"]) == 4 and box["text"] == []
    assert all(i["status"] == "needs_review" for b in box.values() for i in b)                 # never citeable as is
    plats = sorted(i["platform"] for i in box["posts"])
    assert plats == ["facebook", "facebook", "instagram", "x"]                                 # merged scroll -> facebook
    x = next(i for i in box["posts"] if i["platform"] == "x")
    assert x["handles"][0] == "jordanavery_co" and x["posts_linked"][0]["url"].endswith("/status/1800042")
    rows = store.query("kind='screenshot'")
    assert all(r["evidence"] == "lead" for r in rows)
    assert any("9:41" in line for r in rows for line in r["meta"]["dropped_chrome"])          # status bar removed
    assert not any("9:41" in r["text"] for r in rows)


@needs_ocr
def test_candidates_and_no_surname_places(ingested):
    _, store, _ = ingested
    notice = screens.Inbox.box_export(store)["ordinances"][0]
    assert notice["subjects"] == ["casey-morgan"]
    assert "morgan" not in notice["targets"]                                             # a surname, not the town
    assert "public hearing" in notice["text"].lower()


def test_match_jurisdictions():
    screens.configure({"state_abbr": "CO", "state_name": "Colorado"})
    m = lambda t: screens.match_targets(t, JUR)  # noqa: E731
    assert m("The City of Rifle council adopted it") == ["rifle"]
    assert m("Council member Casey Morgan said") == []
    assert m("Posted by Adams County") == ["co-adams"]
    assert m("Castle Rock residents") == ["castle-rock"]
    assert m("Ruling in Morrison, CO today") == ["morrison"]           # a surname-like town needs the state
    assert m("Ruling in Morrison today") == []
    assert m("Parker Town Council meets Monday") == ["parker"]
    assert m("Castle Pines and Denver") and m("Castle Pines and Denver")[0] == "castle-pines"


def test_classify_and_detect():
    assert screens.classify({"text": "ORDINANCE NO. 2026-14 An ordinance amending Sec. 18-3"}) == "ordinances"
    assert screens.classify({"text": "anything", "platform": "x"}) == "posts"
    assert screens.classify({"text": "meeting moved to Thursday"}) == "text"
    assert screens.classify({"text": "a post"}, hint="ordinances") == "ordinances"            # folder wins
    d = screens.detect("Jane Q · @janeq · 2h\n12 Reposts 48 Likes 2,310 Views\nbsky.app/profile/janeq.bsky.social/post/3k")
    assert d["handles"][0] == "janeq.bsky.social" and d["engagement"]["like"] == "48" and "2h" in d["dates"]


def test_merge_overlap_tolerates_ocr_noise():
    prev = ["intro", "Line 7 of a thread about the budget hear:", "Line 8 of a thread about the budget heari",
            "Line 9 of a thread about the budget hear"]
    cur = ["e of a thread ab", "Line 7 of a thread about the budget heari", "Line 8 of a thread about the budget hear",
           "Line 9 of a thread about the budget hear:", "Line 10 is new"]
    assert screens.merge_overlap(prev, cur, min_run=3) == ["Line 10 is new"]
    assert screens.merge_overlap(prev, ["unrelated", "lines", "here", "too"], min_run=3) == ["unrelated", "lines", "here", "too"]


def test_dhash_near_duplicates():
    from PIL import Image, ImageDraw
    a = Image.new("RGB", (400, 800), "white")
    ImageDraw.Draw(a).rectangle((50, 100, 300, 400), fill="black")
    b = a.copy().resize((390, 780))
    c = Image.new("RGB", (400, 800), "white")
    ImageDraw.Draw(c).rectangle((50, 500, 300, 700), fill="black")
    assert screens.hamming(screens.dhash(a), screens.dhash(b)) <= 3 < screens.hamming(screens.dhash(a), screens.dhash(c))


# ----------------------------------------------------------------------------- automated capture rules
PAGES = {"/robots.txt": b"User-agent: *\nDisallow: /private\n",
         "/notice": b"<html><head><title>Notice</title></head><body><main><h1>Public notice</h1><p>"
                    + b"The council will hold a public hearing on the short-term rental ordinance. " * 6 + b"</p></main></body></html>"}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain" if self.path.endswith(".txt") else "text/html")
        self.end_headers()
        self.wfile.write(body)

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
    return Fetcher("Test/1", tmp_path / "state", min_interval=0, session=s, sleep=lambda _: None)


def test_capture_respects_robots_and_blocklist(server, fetcher):
    with pytest.raises(Disallowed):
        screens.capture_page(server + "/private/x", fetcher)
    fetcher.blocklist.add(server + "/notice", "HTTP 403 bot-protection challenge")
    with pytest.raises(Blocked):
        screens.capture_page(server + "/notice", fetcher)


def test_capture_reads_page_text(server, fetcher, tmp_path):
    pytest.importorskip("playwright")
    try:
        rec = screens.capture_record(server + "/notice", fetcher, device="phone", save_to=tmp_path / "shots")
    except Exception as exc:  # noqa: BLE001 — no browser available here
        pytest.skip(f"browser unavailable: {exc}")
    assert rec.meta["text_from"] == "dom" and "public hearing" in rec.text and rec.meta["opened"]
    assert Path(rec.meta["screenshot_file"]).exists() and rec.meta["captured_as"] == "phone"
