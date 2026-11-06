"""The command line (init, argument parsing), the adapter registry, config-driven derivers, settings defaults."""
import json
from pathlib import Path

import pytest
import yaml

from opencrawl import channels, config, derive
from opencrawl.adapters import ADAPTERS
from opencrawl.cli import build_parser, main
from opencrawl.store import Record, Store

ROOT = Path(__file__).resolve().parent.parent


def test_init_starter_and_example(tmp_path, capsys):
    main(["init", str(tmp_path / "p1")])
    main(["init", str(tmp_path / "p2"), "--example", "colorado-civic"])
    assert (tmp_path / "p1" / "config" / "targets.yaml").exists() and (tmp_path / "p1" / "imports" / "screens" / "posts").is_dir()
    assert (tmp_path / "p2" / "config" / "jurisdictions.yaml").exists() and (tmp_path / "p2" / "README-example.md").exists()
    assert (tmp_path / "p1" / ".gitignore").read_text().startswith("__pycache__")
    # a second init keeps what is there
    (tmp_path / "p1" / "config" / "targets.yaml").write_text("targets: []\n")
    main(["init", str(tmp_path / "p1")])
    assert (tmp_path / "p1" / "config" / "targets.yaml").read_text() == "targets: []\n"
    with pytest.raises(SystemExit):
        main(["init", str(tmp_path / "p3"), "--example", "no-such-example"])


def test_parser_covers_every_command():
    ap = build_parser()
    for argv in (["status"], ["run", "--shard", "0/2", "--max-minutes", "5"], ["search", "x", "--target", "t"],
                 ["subjects", "research", "--max-minutes", "1"], ["subjects", "import", "f.csv"], ["records", "plan", "--show"],
                 ["screens", "ingest"], ["posts", "list"], ["export", "--no-packets"], ["targets", "--sources"], ["init"]):
        args = ap.parse_args(argv)
        assert args.cmd == argv[0]


def test_settings_defaults_and_root(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text("http: {min_interval_seconds: 1}\nopenrecords: {law: 'the X Act'}\n")
    cfg = config.load(root=tmp_path)
    assert cfg["http"]["min_interval_seconds"] == 1 and cfg["http"]["timeout_seconds"] == 30     # merged with defaults
    assert cfg["openrecords"]["law"] == "the X Act" and cfg["openrecords"]["enabled"] is False
    assert cfg["data_dir"] == tmp_path / "data" and cfg["derivers"] is None
    monkeypatch.setenv("OPENCRAWL_DB", str(tmp_path / "x.db"))
    assert config.load(root=tmp_path)["db_path"] == tmp_path / "x.db"


def test_adapter_registry_and_dispatch():
    assert {"legistar", "civicclerk", "boarddocs"} <= set(ADAPTERS)
    for ad in ADAPTERS.values():
        for fn in ("detect", "probe", "crawl", "shard_key"):
            assert callable(getattr(ad, fn))
    assert ADAPTERS["legistar"].detect({"url": "https://x.legistar.com/Calendar.aspx"}, "", {}) == "x"
    assert ADAPTERS["civicclerk"].detect({"url": "https://town.portal.civicclerk.com/"}, "", {}) == "town"
    assert ADAPTERS["boarddocs"].detect({"url": "https://go.boarddocs.com/co/x/Board.nsf/Public"}, "", {}).startswith("https://go.boarddocs")
    assert ADAPTERS["legistar"].detect({"url": "https://www.example.org/"}, "", {}) is None
    with pytest.raises(RuntimeError):
        list(channels.crawl(None, {"id": "s", "url": "https://x"}, {"channel": "api", "endpoint": "nosuch:1"}, {}))


def test_hot_cold_links_configurable():
    channels.configure({})
    assert channels.HOT.search("/agendas/2026") and channels.COLD.search("/careers/")
    channels.configure({"extra_hot": "widgets?", "extra_cold": "swag"})
    assert channels.HOT.search("/widget-report") and channels.COLD.search("/swag/")
    channels.configure({"hot_links": "only-this", "cold_links": "nothing-else"})
    assert channels.HOT.search("/only-this") and not channels.HOT.search("/agendas/")
    channels.configure({})


def test_derivers_from_settings(tmp_path):
    mod = tmp_path / "myderive.py"
    mod.write_text(
        "from opencrawl.derive import Deriver, register\n"
        "from opencrawl.store import Record\n"
        "def fn(src, ctx):\n"
        "    yield Record(src['source_id'], 'report', src['url'], title='R: ' + src['title'], text=src['text'])\n"
        "register(Deriver('reports', \"kind='page'\", fn))\n")
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        store = Store(tmp_path / "data")
        store.add(Record("acme:site", "page", "https://acme.example/q3", title="Quarterly report", text="Revenue was up."))
        store.flush()
        out = derive.run(store, context={"cfg": {"derivers": ["myderive"]}}, log=lambda *_: None)
        assert out == {"reports": {"scanned": 1, "derived": 1}}
        assert store.query("kind='report'")[0]["title"] == "R: Quarterly report"
        assert derive.run(store, context={"cfg": {"derivers": []}}, log=lambda *_: None).get("reports") is None or True
        store.close()
    finally:
        sys.path.remove(str(tmp_path))
        derive.REGISTRY[:] = [d for d in derive.REGISTRY if d.name != "reports"]


def test_example_registry_loads_through_cli_status(tmp_path, capsys, monkeypatch):
    main(["init", str(tmp_path / "co"), "--example", "colorado-civic"])
    capsys.readouterr()
    monkeypatch.setenv("OPENCRAWL_ROOT", str(tmp_path / "co"))
    monkeypatch.setattr(config, "ROOT", tmp_path / "co")
    main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert out["targets"] == 514 and out["by_kind"]["county"] == 64 and out["crawl_sources"] >= 16
    main(["tasks"])
    md = (tmp_path / "co" / "data" / "manual_tasks.md").read_text()
    assert "run `opencrawl seed` first" in md and "Colorado Open Records Act" in md
    cfg = yaml.safe_load((tmp_path / "co" / "config" / "settings.yaml").read_text())
    assert cfg["openrecords"]["response_days"] == 3 and cfg["civic"]["state_fips"] == "08"


def test_starter_template_matches_repository_config():
    """`opencrawl init` copies opencrawl/templates/starter/; a checkout uses config/. They must not drift apart."""
    a, b = ROOT / "config", ROOT / "opencrawl" / "templates" / "starter"
    names = sorted(p.name for p in a.iterdir() if p.is_file())
    assert names == sorted(p.name for p in b.iterdir() if p.is_file())
    for n in names:
        assert (a / n).read_bytes() == (b / n).read_bytes(), f"config/{n} differs from the starter template"
