"""Daytime backfill: the switch, the day window, the month's minute cap, the stop rule, one count per run."""
import datetime as dt
import json

from opencrawl import backfill

ET_NOON = dt.datetime(2026, 11, 6, 17, 0, tzinfo=dt.timezone.utc)      # 12:00 EST
ET_NIGHT = dt.datetime(2026, 11, 7, 4, 0, tzinfo=dt.timezone.utc)      # 23:00 EST


def setup(tmp_path, **cfg):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "backfill.json").write_text(json.dumps(dict({"enabled": True, "month_minutes_cap": 1000}, **cfg)))
    return tmp_path


def test_plan_window_and_switch(tmp_path):
    root = setup(tmp_path)
    assert backfill.plan(root, ET_NOON)["go"] is True
    p = backfill.plan(root, ET_NIGHT)
    assert p["active"] is True and p["go"] is False and p["why"] == "outside the day window"
    (root / "config" / "backfill.json").write_text(json.dumps({"enabled": False}))
    assert backfill.plan(root, ET_NOON)["active"] is False


def test_cap_and_stop_rule(tmp_path):
    root = setup(tmp_path)
    r = backfill.next_run(root, 150, True, {"added": {"statement": 400}}, 2000, run_id="1", now=ET_NOON)
    assert r["go"] and r["month_minutes"] == 150 and r["quiet"] == 0
    assert backfill.next_run(root, 150, True, {"added": {}}, 2000, run_id="1", now=ET_NOON)["why"] == "this run was already counted"
    # nightly runs count toward the cap but never chain
    r = backfill.next_run(root, 80, False, None, None, run_id="2", now=ET_NIGHT)
    assert not r["go"] and r["month_minutes"] == 230
    # every candidate researched, then two quiet runs: done
    backfill.next_run(root, 150, True, {"added": {"statement": 10}}, 0, run_id="3", now=ET_NOON)
    r = backfill.next_run(root, 150, True, {"added": {"statement": 5}}, 0, run_id="4", now=ET_NOON)
    assert r["done"] and not r["go"] and "every subject researched" in backfill.load_state(root)["done_reason"]
    assert backfill.plan(root, ET_NOON)["active"] is False


def test_month_cap_pauses(tmp_path):
    root = setup(tmp_path, month_minutes_cap=300)
    backfill.next_run(root, 200, True, {"added": {"x": 500}}, 900, run_id="1", now=ET_NOON)
    r = backfill.next_run(root, 150, True, {"added": {"x": 500}}, 800, run_id="2", now=ET_NOON)
    assert not r["go"] and "cap" in r["why"]
    # a new month starts fresh
    assert backfill.plan(root, dt.datetime(2026, 12, 1, 17, 0, tzinfo=dt.timezone.utc))["go"] is True
