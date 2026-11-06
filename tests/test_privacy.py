"""Erasure, retention, the HTTP cache, the encrypted vault, the doctor and public-figures-only research."""
import io
import json
import os
import sqlite3
import time

import pytest

from opencrawl import privacy, vault
from opencrawl.cli import main
from opencrawl.research.quarantine import Quarantine
from opencrawl.store import Record, Store
from opencrawl.verified_posts import Registry


def _seed(root):
    store = Store(root / "data")
    old = "2020-01-01T00:00:00+00:00"
    store.add(Record("subj:sam-sample", "statement", "https://sam.example/a", title="Sam on parks",
                     text="UNIQUEWORDALPHA Sam Sample said the park needs lights.", evidence="primary"))
    store.add(Record("subj:sam-sample", "article", "https://news.example/b", title="Old story", text="older text",
                     fetched_at=old, evidence="reported"))
    store.add(Record("subj:other", "statement", "https://other.example/a", title="Other", text="keep me"))
    store.add(Record("screens", "screenshot", "file://a.png", title="shot", text="screenshot text", fetched_at=old,
                     meta={"subjects": ["sam-sample", "other"], "box": "posts"}))
    store.add(Record("town:site", "page", "https://town.example/", title="Town", text="public page"))
    store.flush()
    q = Quarantine(root)
    q.add("sam-sample", "https://blog.example/x", "an unsourced claim", "article", seen_at=old)
    q.add("other", "https://blog.example/y", "another claim", "article")
    q.flush()
    reg = Registry(root / "data" / "verified")
    acc = reg.add_account("Sam Sample", "https://bsky.app/profile/sam.example.social", "https://sam.example")
    reg.mark_subject(acc["id"], "sam-sample")
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "subject_progress.json").write_text(json.dumps({"subj:sam-sample": {"last_ok": "x"},
                                                                      "subj:other": {"last_ok": "y"}}))
    b = root / "dist" / "bundle"
    for d in ("subjects", "packets", "profiles"):
        (b / d).mkdir(parents=True, exist_ok=True)
        (b / d / "sam-sample.json").write_text("{}")
    return store


def test_erase_subject_removes_everything_and_compacts(tmp_path):
    store = _seed(tmp_path)
    out = privacy.erase_subject(tmp_path, store, "sam-sample")
    assert out["records"] == 2 and out["quarantined"] == 1 and out["verified_accounts"] == 1 and out["export_files"] == 3
    assert out["screenshot_links"] == 1 and out["progress"] == 1
    assert store.query("source_id='subj:sam-sample'") == [] and len(store.query("source_id='subj:other'")) == 1
    shot = store.query("kind='screenshot'")[0]
    assert shot["meta"]["subjects"] == ["other"]
    assert store.search(Store.fts_phrase("UNIQUEWORDALPHA")) == []
    assert [r["subject_id"] for r in Quarantine(tmp_path).all()] == ["other"]
    store.close()
    raw = (tmp_path / "data" / "crawl.db").read_bytes()
    assert b"UNIQUEWORDALPHA" not in raw                       # gone from the file itself, not just unlinked
    with pytest.raises(ValueError):
        privacy.erase_subject(tmp_path, Store(tmp_path / "data"), "../etc")


def test_retention_expires_old_personal_data(tmp_path):
    store = _seed(tmp_path)
    cache = tmp_path / "state" / "http_cache"
    cache.mkdir(parents=True)
    (cache / "old.body").write_text("page copy")
    os.utime(cache / "old.body", (time.time() - 400 * 86400,) * 2)
    (cache / "new.body").write_text("page copy")
    assert privacy.expire(tmp_path, {"privacy": {}}, store)["skipped"]
    out = privacy.expire(tmp_path, {"privacy": {"retention_days": 365}, "state_dir": tmp_path / "state"}, store)
    assert out["subject_records"] == 1 and out["screenshots"] == 1 and out["quarantined"] == 1 and out["http_cache"] == 1
    assert {r["url"] for r in store.query("1")} == {"https://sam.example/a", "https://other.example/a", "https://town.example/"}
    assert privacy.prune_cache(tmp_path / "state", 0) == {"removed": 1, "bytes": 9}
    store.close()


def test_purge_and_cache_commands(tmp_path, monkeypatch, capsys):
    from opencrawl import config

    main(["init", str(tmp_path / "p")])
    root = tmp_path / "p"
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setenv("OPENCRAWL_ROOT", str(root))
    _seed(root).close()
    capsys.readouterr()
    main(["purge", "--subject", "sam-sample"])
    assert json.loads(capsys.readouterr().out)["records"] == 2
    main(["cache", "prune"])
    assert "removed" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["purge"])


def test_vault_cli_roundtrip_and_pack(tmp_path, monkeypatch, capsys):
    main(["vault", "keygen"])
    key = capsys.readouterr().out.strip()
    assert key.startswith("ocv1:")
    monkeypatch.setenv("OPENCRAWL_DATA_KEY", key)
    (tmp_path / "plain.txt").write_text("personal data")
    main(["vault", "encrypt", str(tmp_path / "plain.txt"), str(tmp_path / "sealed.ocv")])
    assert b"personal data" not in (tmp_path / "sealed.ocv").read_bytes()
    main(["vault", "decrypt", str(tmp_path / "sealed.ocv"), str(tmp_path / "back.txt")])
    assert (tmp_path / "back.txt").read_text() == "personal data"
    assert oct((tmp_path / "back.txt").stat().st_mode & 0o777) == "0o600"
    # pack / unpack a project's data and state, leaving the page cache out
    proj = tmp_path / "proj"
    (proj / "data").mkdir(parents=True)
    (proj / "state" / "http_cache").mkdir(parents=True)
    (proj / "data" / "crawl.db").write_bytes(b"db")
    (proj / "state" / "progress.json").write_text("{}")
    (proj / "state" / "http_cache" / "x.body").write_text("cached page")
    monkeypatch.chdir(proj)
    main(["vault", "pack", str(tmp_path / "data.ocv"), "data", "state", "--exclude", "state/http_cache"])
    main(["vault", "unpack", str(tmp_path / "data.ocv"), str(tmp_path / "restored")])
    assert (tmp_path / "restored" / "data" / "crawl.db").read_bytes() == b"db"
    assert (tmp_path / "restored" / "state" / "progress.json").exists()
    assert not (tmp_path / "restored" / "state" / "http_cache").exists()
    monkeypatch.setenv("OPENCRAWL_DATA_KEY", vault.keygen())
    with pytest.raises(SystemExit):
        main(["vault", "decrypt", str(tmp_path / "sealed.ocv"), str(tmp_path / "nope.txt")])
    assert not (tmp_path / "nope.txt").exists()


def test_vault_refuses_hostile_archives(tmp_path):
    import tarfile

    key = vault.keygen()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        data = b"owned"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    sealed = io.BytesIO()
    vault.encrypt_stream(io.BytesIO(raw.getvalue()), sealed, key)
    (tmp_path / "evil.ocv").write_bytes(sealed.getvalue())
    with pytest.raises(vault.VaultError, match="refusing"):
        vault.unpack(tmp_path / "evil.ocv", tmp_path / "out", key)
    assert not (tmp_path / "escape.txt").exists()


def test_vault_detects_tampering_and_truncation():
    key = vault.keygen()
    data = os.urandom(vault.CHUNK * 2 + 10)
    sealed = io.BytesIO()
    vault.encrypt_stream(io.BytesIO(data), sealed, key)
    blob = sealed.getvalue()
    for bad in (blob[:-1], blob[: vault.HEADER_LEN + vault.CHUNK + vault.TAG], blob[:20] + bytes([blob[20] ^ 1]) + blob[21:]):
        with pytest.raises(vault.VaultError):
            vault.decrypt_stream(io.BytesIO(bad), io.BytesIO(), key)
    with pytest.raises(vault.VaultError):
        vault.encrypt_stream(io.BytesIO(b"x"), io.BytesIO(), "short")         # passphrases need 16+ characters
    out = io.BytesIO()
    vault.decrypt_stream(io.BytesIO(blob), out, key)
    assert out.getvalue() == data


def test_doctor_flags_secrets_and_open_guard(tmp_path, monkeypatch, capsys):
    from opencrawl import config, doctor

    main(["init", str(tmp_path / "p")])
    root = tmp_path / "p"
    cfg = config.load(root=root)
    monkeypatch.delenv("OPENCRAWL_ALLOW_PRIVATE_NETWORKS", raising=False)
    from opencrawl import netguard

    netguard.configure(False, [])
    assert doctor.main(cfg, root) == 0
    (root / "config" / "finance.yaml").write_text("api_key: abcdefghijklmnop1234\n")
    res = doctor.run(cfg, root)
    assert any(level == "fail" and name == "secrets in config" for level, name, _ in res)
    netguard.configure(True, [], block_metadata=False)
    assert any(level == "warn" and name == "network guard" for level, name, _ in doctor.run(cfg, root))
    assert any(level == "fail" and name == "network guard" for level, name, _ in doctor.run(cfg, root, ci=True))
    netguard.configure(True, [])
    assert not any(level == "fail" for level, name, _ in doctor.run(cfg, root, ci=True) if name == "network guard")
    netguard.configure(False, [])
    monkeypatch.delenv("OPENCRAWL_DATA_KEY", raising=False)
    assert not any(level == "fail" for level, name, _ in doctor.run(cfg, root) if name == "data key")
    assert any(level == "fail" and name == "data key" for level, name, _ in doctor.run(cfg, root, ci=True))


def test_store_files_are_private(tmp_path):
    s = Store(tmp_path / "data")
    s.close()
    assert oct((tmp_path / "data" / "crawl.db").stat().st_mode & 0o777) == "0o600"
    db = sqlite3.connect(tmp_path / "data" / "crawl.db")
    assert db.execute("PRAGMA trusted_schema").fetchone() is not None
