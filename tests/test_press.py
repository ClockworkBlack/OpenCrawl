"""Press releases -> appointments; newsroom sources; page dates. Fictional names."""
from pathlib import Path

from opencrawl import derive, extract
from opencrawl.civic import press, registry
from opencrawl.store import Record, Store

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "opencrawl" / "examples" / "colorado-civic"
RELEASE = """DENVER – October 2, 2026 – Governor Dale P. Corwin today announced the appointment of Jamie Q. Sample to the State Board of Education, representing the 7th Congressional District. Sample is a Lakewood attorney. The Governor also appointed John Doe as judge of the District Court of the Fourth Judicial District, filling the vacancy created by the retirement of Judge Iris Penn. Corwin also announced appointments to the Board of Natural Resources: Casey Garnet of Pueblo; Jesse Fenwick of Greeley; and Taylor Calder of Durango, with terms expiring January 1, 2032."""
CITY = """The Rifle City Council on Thursday appointed Robin Ashby to the Planning Commission for a term ending December 31, 2028. Mayor Morgan Okafor said Ashby brings experience. The council also discussed the parks plan."""


def test_parse_appointments():
    a = {x["person"]: x for x in press.parse_appointments(RELEASE)}
    assert a["Jamie Q. Sample"]["body"] == "State Board of Education" and a["Jamie Q. Sample"]["by"] == "Dale P. Corwin"
    assert a["John Doe"]["role"] == "judge" and a["John Doe"]["body"].startswith("District Court")
    assert {"Casey Garnet", "Jesse Fenwick", "Taylor Calder"} <= set(a) and a["Taylor Calder"]["term"] == "January 1, 2032" and a["Casey Garnet"]["of"] == "of Pueblo"
    assert "Iris Penn" not in a and "Dale P. Corwin" not in a
    c = press.parse_appointments(CITY)
    assert len(c) == 1 and c[0]["person"] == "Robin Ashby" and c[0]["body"] == "Planning Commission" and c[0]["term"] == "December 31, 2028"


def test_press_deriver_and_sources(tmp_path):
    store = Store(tmp_path / "data")
    store.add(Record("press:governor", "press_release", "https://gov.example/press-releases/appointments-oct-2", title="Gov. Corwin Announces Appointments",
                     text=RELEASE, published="2026-10-02", channel="html", evidence="primary", meta={"opened": True}))
    store.add(Record("canton:site", "page", "https://canton.example/news/council-appoints", title="Council appoints planning commissioner",
                     text=CITY, published="2026-10-09", target="rifle", channel="html", evidence="primary", meta={"opened": True}))
    store.add(Record("canton:site", "page", "https://canton.example/parks", title="Parks", text="Fun for all. " * 30, target="rifle"))
    store.flush()
    out = derive.run(store, only={"press"}, log=lambda *_: None)
    assert out["press"] == {"scanned": 2, "derived": 6}
    apps = {r["meta"]["person"]: r for r in store.query("kind='appointment'")}
    assert apps["Jamie Q. Sample"]["meta"]["level"] == "state" and apps["Jamie Q. Sample"]["published"] == "2026-10-02"
    assert apps["Robin Ashby"]["target"] == "rifle" and apps["Robin Ashby"]["meta"]["level"] == "rifle" and apps["Robin Ashby"]["meta"]["by"] == "Morgan Okafor"
    assert derive.run(store, only={"press"}, log=lambda *_: None)["press"]["scanned"] == 0
    store.close()
    ids = {s["id"]: s for s in registry.press_sources(EXAMPLE)}
    assert "press:governor" in ids and ids["press:governor"]["kind"] == "press_release" and ids["press:governor"]["level"] == "state"
    assert any(s["id"] == "press:sos" for s in registry.all_sources(EXAMPLE))


def test_page_date():
    assert extract.page_date('<html><head><meta property="article:published_time" content="2026-10-02T14:00:00-04:00"></head><body>x</body></html>') == "2026-10-02"
    assert extract.page_date("<html><body><p>DENVER – October 2, 2026 – Governor Dale P. Corwin today announced</p></body></html>") == "2026-10-02"
    assert extract.page_date('<html><body><time datetime="2026-09-30">Sept. 30</time></body></html>') == "2026-09-30"
    assert extract.page_date("<html><body>nothing</body></html>") is None
