"""Transcripts: VTT/SRT/timestamped parsing, time codes, transcript links on video pages, meeting follow-up,
mentions with time codes. Local server; fictional names."""
import http.server
import threading
from pathlib import Path

import pytest
import requests

from opencrawl import channels, transcripts
from opencrawl.research.subjects import Researcher
from opencrawl.fetch import Fetcher
from opencrawl.names import name_variants
from opencrawl.store import Record, Store

VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Mayor>Call to order. Roll call please.

00:00:04.500 --> 00:00:08.000
Council member Robin Ashby: present.

00:12:30.000 --> 00:12:36.000
Robin Ashby: I move to table the short-term rental ordinance until the traffic study is done.

00:12:36.500 --> 00:12:40.000
Second by council member Taylor Calder. All in favor.

00:45:01.000 --> 00:45:05.000
Motion carries five to zero.

00:45:06.000 --> 00:45:09.000
Next item, the FY27 budget.
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
Good evening.

2
00:00:03,500 --> 00:00:06,000
We have a quorum.

3
00:01:00,000 --> 00:01:02,000
Item one.

4
00:01:02,500 --> 00:01:05,000
Item two.

5
00:01:06,000 --> 00:01:08,000
Item three.
"""

GRANICUS_TRANSCRIPT = """<html><head><title>Transcript - City Council 10/20/2026</title></head><body>
<div class="transcript">
<p>[00:00:03] MAYOR: Good evening and welcome.</p>
<p>[00:00:09] CLERK: Roll call. Council member Robin Ashby.</p>
<p>[00:00:12] ROBIN ASHBY: Here.</p>
<p>[00:31:40] ROBIN ASHBY: I cannot support a data center on that parcel without a water study.</p>
<p>[00:31:55] MAYOR: Noted. Next speaker.</p>
<p>[01:10:02] CLERK: Motion passes.</p>
</div></body></html>"""

VIDEO_PAGE = """<html><head><title>City Council 10/20/2026</title></head><body>
<a href="/TranscriptViewer.php?view_id=2&clip_id=77">Transcript</a>
<video><track kind="captions" src="/captions/77.vtt"></video>
</body></html>"""

PAGES = {
    "/robots.txt": (b"User-agent: *\nAllow: /\n", "text/plain"),
    "/MediaPlayer.php": (VIDEO_PAGE.encode(), "text/html"),
    "/TranscriptViewer.php": (GRANICUS_TRANSCRIPT.encode(), "text/html"),
    "/captions/77.vtt": (VTT.encode(), "text/vtt"),
    "/council": (b"<html><body><main><p>" + b"Council page text. " * 20 + b"</p><a href='/MediaPlayer.php?view_id=2&clip_id=77'>Video</a></main></body></html>", "text/html"),
}


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path not in PAGES:
            self.send_response(404); self.end_headers(); return
        body, ct = PAGES[path]
        self.send_response(200)
        self.send_header("Content-Type", ct)
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


def test_parse_vtt_and_srt():
    cues = transcripts.parse_vtt(VTT)
    assert cues[0] == (1.0, "Call to order. Roll call please.")            # <v> tag stripped
    assert cues[2][0] == 750.0 and "short-term rental" in cues[2][1]
    srt = transcripts.parse_vtt(SRT)
    assert len(srt) == 5 and srt[1] == (3.5, "We have a quorum.")
    rec = transcripts.cues_to_record(cues, "x:granicus", "http://x/77.vtt", "Council", {})
    assert rec.kind == "transcript" and rec.meta["duration"] == "0:45:06" and rec.meta["cues"] == 6
    assert rec.meta["segments"][0] == [1.0, 0] and len(rec.meta["segments"]) == 5     # markers at least 5 s apart
    pos = rec.text.index("I move to table")
    assert transcripts.time_at({"meta": rec.meta}, pos) == "0:12:30"
    assert transcripts.hms(3725) == "1:02:05"


def test_parse_timestamped_html_transcript():
    from opencrawl.extract import soup_of
    cues = transcripts.parse_timestamped(soup_of(GRANICUS_TRANSCRIPT).get_text("\n", strip=True))
    assert len(cues) == 6 and cues[3][0] == 1900.0 and "water study" in cues[3][1]


def test_video_page_links_and_crawl_hook(server, fetcher, tmp_path):
    links = transcripts.transcript_links(VIDEO_PAGE, server + "/MediaPlayer.php?view_id=2&clip_id=77")
    assert any("TranscriptViewer" in u for u in links) and any(u.endswith("/captions/77.vtt") for u in links)
    store = Store(tmp_path / "data")
    src = {"id": "t:site", "url": server + "/council", "kind": "page", "depth": 2}
    recs = list(channels.crawl(fetcher, src, {"channel": "html", "endpoint": server + "/council"}, {},
                               ctx=channels.CrawlContext(frontier=store)))
    kinds = [r.kind for r in recs]
    assert kinds.count("transcript") == 2                                   # viewer page + caption file
    gr = next(r for r in recs if r.kind == "transcript" and "TranscriptViewer" in r.url)
    assert gr.meta["transcript_source"] == "granicus" and gr.meta["video_page"].endswith("clip_id=77")
    store.close()


def test_from_meetings_and_mention_time_codes(server, fetcher, tmp_path):
    store = Store(tmp_path / "data")
    store.add(Record("canton:legistar", "meeting", "https://canton.legistar.com/MeetingDetail.aspx?ID=1", title="City Council 2026-10-20",
                     published="2026-10-20", target="rifle",
                     meta={"EventId": 1, "EventBodyName": "City Council", "EventVideoPath": server + "/MediaPlayer.php?view_id=2&clip_id=77"}))
    store.flush()
    out = transcripts.from_meetings(fetcher, store, log=lambda *_: None)
    assert out == {"checked": 1, "transcripts": 2}
    assert store.query("kind='meeting'")[0]["meta"]["transcript_checked"]
    assert transcripts.from_meetings(fetcher, store, log=lambda *_: None)["checked"] == 0   # not re-checked
    ts = store.query("kind='transcript'")
    assert all(t["target"] == "rifle" and t["meta"]["meeting_date"] == "2026-10-20" for t in ts)
    # a candidate's mentions in transcripts carry the time code
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    base = Path(__file__).resolve().parent.parent
    (root / "config" / "outlets.yaml").write_text((base / "config" / "outlets.yaml").read_text())
    (root / "config" / "jurisdictions.yaml").write_text((base / "opencrawl" / "examples" / "colorado-civic" / "config" / "jurisdictions.yaml").read_text())
    r = Researcher(root, {}, fetcher, store, log=lambda *_: None)
    cand = {"id": "robin-ashby", "name": "Robin Ashby", "aliases": [], "websites": [], "social": [], "targets": ["rifle"],
            "target": "rifle", "variants": name_variants("Robin Ashby")}
    counts = r.research(cand, skip={"votes", "posts", "statements", "articles", "leads", "social_mentions", "connections"})
    assert counts["mention"] == 2
    ms = {m["url"]: m for m in store.read("subj:robin-ashby", "mention")}
    gran = next(m for u, m in ms.items() if "TranscriptViewer" in u)
    assert gran["meta"]["time"] == "0:00:09" and gran["published"] == "2026-10-20"       # first mention: roll call
    assert any("water study" in m["text"] for m in ms.values())
    store.close()


def test_youtube_without_key_is_quiet(fetcher, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert transcripts.youtube_videos(fetcher, "https://www.youtube.com/@cityofx") == []
