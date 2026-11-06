"""Store efficiency guarantees: meta-only updates don't re-index full text; older databases gain the text-hash
column on open; graph lookups go through adjacency indexes."""
import sqlite3

from opencrawl.research import connections
from opencrawl.research.entities import EntityIndex
from opencrawl.store import Record, Store


def test_meta_only_update_keeps_fts_and_skips_reindex(tmp_path):
    s = Store(tmp_path / "data")
    s.add(Record("x:site", "page", "https://x/1", title="Budget hearing", text="The millage rate stays flat", meta={"a": 1}))
    s.flush()
    rid = s.db.execute("SELECT rid FROM records").fetchone()[0]
    # count full-text index writes with a trigger of our own on the shadow table
    s.db.execute("CREATE TEMP TABLE fts_writes(n INTEGER)")
    s.db.execute("CREATE TEMP TRIGGER count_fts AFTER UPDATE OF title, text ON records BEGIN INSERT INTO fts_writes VALUES (1); END")
    assert s.add(Record("x:site", "page", "https://x/1", title="Budget hearing", text="The millage rate stays flat", meta={"a": 2}))
    s.flush()
    assert s.db.execute("SELECT count(*) FROM fts_writes").fetchone()[0] == 0          # meta changed, text didn't: no re-index
    assert s.query("rid=?", (rid,))[0]["meta"] == {"a": 2} and s.query("rid=?", (rid,))[0]["version"] == 2
    assert s.search('"millage rate"')[0]["url"] == "https://x/1"
    assert s.add(Record("x:site", "page", "https://x/1", title="Budget hearing", text="The millage rate goes up", meta={"a": 2}))
    s.flush()
    assert s.db.execute("SELECT count(*) FROM fts_writes").fetchone()[0] == 1
    assert s.search('"goes up"') and not s.search('"stays flat"')
    assert "thash" not in s.query("1")[0]
    s.close()


def test_graph_adjacency_matches_scan(tmp_path):
    s = Store(tmp_path / "data")
    for i in range(30):
        s.add(Record("subj:a", "contribution", f"file://x{i}", title="c", key=f"k{i}",
                     meta={"type": "contribution", "name": f"Donor {i % 7} Person", "amount": 10.0 * i, "date": "2026-01-01",
                           "recipient": "Ann Able", "subject_id": "a", "donor_is_org": False,
                           "employer": "Acme Construction, LLC" if i % 2 else ""}))
    s.flush()
    from opencrawl.research.finance import Finance

    fin = Finance(tmp_path, {}, None, s, log=lambda *_: None)
    fin.resolve_all([{"id": "a", "name": "Ann Able", "aliases": []}])
    ix = EntityIndex(store=s)
    g = connections.build(s, ix)
    eid = ix.by_subject("a")
    assert [e for e in g.edges if e["b"] == eid and e["type"] == "contributed"] == [e for e in g.inn[eid] if e["type"] == "contributed"]
    pats = connections.patterns(g, s)
    m = connections.money_for(g, s, ix, "a", pats)
    assert m["contributions"] == 30 and m["total_raised"] == sum(10.0 * i for i in range(30))
    assert m["patterns"] == [p for p in pats if eid in str(p)]
    s.close()


def test_fetcher_stops_at_the_budget(tmp_path):
    import time

    import pytest

    from opencrawl.fetch import Fetcher, OutOfTime

    f = Fetcher("Test/1", tmp_path / "state", min_interval=0, sleep=lambda _: None)
    f.robots.allowed = lambda url: True
    f.stop_at = time.monotonic() - 1
    with pytest.raises(OutOfTime):
        f.get("http://127.0.0.1:9/anything")
