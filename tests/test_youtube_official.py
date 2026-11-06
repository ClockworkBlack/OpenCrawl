"""YouTube: only official government channels become records; anything else goes to the Rumor Mill - Video bucket.
Bare youtube.com/name links in a site's social icons are found; single videos and embeds are not channels."""
import json

from opencrawl import transcripts
from opencrawl.civic.registry import find_portals, sources_for
from opencrawl.research.rumor_video import VideoRumorMill
from opencrawl.transcripts import official_channel

SITE = "linked from the official website (https://www.commercecity.example/)"


def test_official_rules():
    ok, why = official_channel({"title": "City of Commerce City", "handle": "@commercecityco"}, "Commerce City", SITE)
    assert ok and "names the target" in why
    assert official_channel({"title": "Jefferson County Government", "handle": "@cobbcountygovt"}, "Jefferson County", SITE)[0]
    # the county site links the Chamber of Commerce's channel: not the government's
    ok, why = official_channel({"title": "Douglas County Chamber", "handle": "@dcchamber"}, "Douglas County",
                               "linked from the official website (http://www.douglascountygeorgia.com/)")
    assert not ok and "chamber" in why.lower()
    assert not official_channel({"title": "KXYZ-TV", "handle": "@kxyztv"}, "Denver", SITE)[0]
    assert not official_channel({"title": "Commerce City Residents United", "handle": "@sfru"}, "Commerce City", SITE)[0]
    # no official page points to it: never official, whatever it is called
    assert not official_channel({"title": "City of Commerce City", "handle": "@x"}, "Commerce City", "")[0]
    assert official_channel({"title": "C3 TV", "handle": "@c3tv"}, "Commerce City", "hand-checked: 2026-11-06 ...")[0]


def test_channel_links_found():
    assert find_portals('<a href="https://www.youtube.com/cityofsouthfulton">', "https://x.gov/") == \
        {"youtube": "https://www.youtube.com/cityofsouthfulton"}
    assert find_portals('<a href="https://www.youtube.com/watch?v=Z1F9POTRCTE">', "https://x.gov/") == {}
    assert find_portals('<iframe src="https://www.youtube.com/embed/xyz">', "https://x.gov/") == {}
    src = [s for s in sources_for({"id": "commerce-city", "name": "Commerce City", "tier": "a", "kind": "municipality", "website": "https://www.commercecity.example/",
                                   "portals": {"youtube": "https://www.youtube.com/@commercecityco"}}) if s["kind"] == "video"][0]
    assert src["youtube_basis"] == "linked from the official website (https://www.commercecity.example/)"


def test_unofficial_channel_goes_to_rumor_mill(tmp_path, monkeypatch):
    chans = {"official": {"id": "UC1", "title": "City of Commerce City", "handle": "@commercecityco", "description": "",
                          "uploads": "UU1", "url": "https://www.youtube.com/channel/UC1"},
             "other": {"id": "UC2", "title": "Commerce City News Now", "handle": "@sfnews", "description": "",
                       "uploads": "UU2", "url": "https://www.youtube.com/channel/UC2"}}
    vids = [{"video_id": "Z1F9POTRCTE", "url": "https://www.youtube.com/watch?v=Z1F9POTRCTE", "title": "City Council Meeting 10/27/2026",
             "published": "2026-10-27T22:00:00Z", "description": ""},
            {"video_id": "q", "url": "https://www.youtube.com/watch?v=q", "title": "Holiday parade", "published": "2026-10-01T00:00:00Z",
             "description": ""}]
    monkeypatch.setattr(transcripts, "youtube_channel", lambda f, u: chans[u])
    monkeypatch.setattr(transcripts, "youtube_videos", lambda f, u, since, limit=200, channel=None: [dict(v, url=v["url"] + u) for v in vids])
    rm = VideoRumorMill(tmp_path)
    recs = list(transcripts.youtube_records(None, "commerce-city:youtube", "commerce-city", "official", target_name="Commerce City",
                                            basis=SITE, rumors=rm))
    assert [r.title for r in recs] == ["City Council Meeting 10/27/2026"] and recs[0].meta["official_channel"]["id"] == "UC1"
    assert not list(transcripts.youtube_records(None, "commerce-city:youtube", "commerce-city", "other", target_name="Commerce City",
                                                basis=SITE, rumors=rm))
    rows = [json.loads(l) for l in (tmp_path / "data" / "rumor_mill" / "videos.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["status"] == "unverified" and "news" in rows[0]["reason"].lower()
    assert rows[0]["channel"]["title"] == "Commerce City News Now"
