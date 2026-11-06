"""`opencrawl doctor`: check a project's security and privacy settings before it runs anywhere.

Each check reports ok, warn (works, but worth a look) or fail (fix before running). Exit status 1 when anything
fails, so CI can run it first. Nothing is sent anywhere; `--browser` also starts the headless browser once; `--ci`
adds the checks for a scheduled CI run (an encryption key for what it stores, no private-network access).
"""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

FLOORS = {  # minimum versions with known security fixes in the libraries that parse untrusted input
    "requests": "2.32.4", "urllib3": "2.6.0", "pdfminer.six": "20251107", "Pillow": "11.3.0", "lxml": "5.3.0",
    "cryptography": "44.0.0", "PyYAML": "6.0.1",
}
SECRET_RX = re.compile(r"(?i)(?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{12,}|"
                       r"\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}|\bAKIA[0-9A-Z]{16}\b|"
                       r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bxox[baprs]-[A-Za-z0-9-]{10,}|\bsk-[A-Za-z0-9]{32,}")


def _ver(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def run(cfg: dict, root: Path, browser: bool = False, ci: bool = False) -> list[tuple[str, str, str]]:
    from importlib.metadata import PackageNotFoundError, version

    out: list[tuple[str, str, str]] = []

    def add(level, name, msg):
        out.append((level, name, msg))

    # --- runtime and libraries
    add("ok" if sys.version_info >= (3, 11) else "fail", "python", sys.version.split()[0])
    for pkg, floor in FLOORS.items():
        try:
            v = version(pkg)
        except PackageNotFoundError:
            add("warn" if pkg == "cryptography" else "fail", pkg, "not installed")
            continue
        add("ok" if _ver(v) >= _ver(floor) else "fail", pkg, f"{v}" + ("" if _ver(v) >= _ver(floor) else
                                                                        f" (security fixes need >= {floor})"))
    # --- network guard
    from . import netguard

    try:
        netguard.check_url("http://169.254.169.254/latest/meta-data/", resolve=False)
        add("fail" if ci else "warn", "network guard",
            "cloud metadata services are reachable (http.block_metadata is false): they hand out the machine's "
            "credentials" + (" - never on a CI runner" if ci else ""))
    except netguard.BlockedAddress:
        add("ok", "network guard", "cloud metadata services are blocked")
    add("ok", "network guard", "local, private-network and cloud-hosted sites can be crawled"
        if netguard.private_allowed() else "only public internet addresses are fetched (http.allow_private_networks: false)")
    http = cfg.get("http") or {}
    if http.get("allow_hosts"):
        add("ok", "network guard", f"always allowed: {', '.join(map(str, http['allow_hosts']))}")
    ua = os.environ.get("OPENCRAWL_USER_AGENT") or http.get("user_agent") or ""
    placeholder = not ua or "your-org/your-repo" in ua
    add("warn" if placeholder else "ok", "user agent",
        (ua or "not set") + (" - set http.user_agent to your project's URL so site owners can reach you" if placeholder else ""))
    if http.get("contact") or os.environ.get("OPENCRAWL_CONTACT"):
        c = http.get("contact") or os.environ.get("OPENCRAWL_CONTACT")
        add("warn" if re.search(r"@(gmail|yahoo|outlook|hotmail|icloud|proton)\.", c or "", re.I) else "ok", "contact",
            f"{c} (sent with every request: use a project or role address)")
    # --- secrets kept out of files
    found = []
    for p in sorted((root / "config").glob("*.y*ml")) + sorted((root / "config").glob("*.json")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if SECRET_RX.search(text):
            found.append(p.name)
    add("fail" if found else "ok", "secrets in config",
        f"something that looks like a key or password in {', '.join(found)}: move it to an environment variable / "
        "repository secret" if found else "none found in config/")
    if cfg.get("legistar_tokens"):
        add("fail", "secrets in config", "legistar_tokens in settings.yaml: use the LEGISTAR_TOKENS secret instead")
    # --- files on disk
    for d in (cfg.get("data_dir"), cfg.get("state_dir")):
        if d and Path(d).exists():
            mode = Path(d).stat().st_mode
            add("warn" if mode & (stat.S_IROTH | stat.S_IWOTH) else "ok", f"permissions {Path(d).name}/",
                f"{oct(mode & 0o777)}" + (" - readable by other users of this machine" if mode & stat.S_IROTH else ""))
    db = Path(cfg.get("db_path") or Path(cfg["data_dir"]) / "crawl.db")
    if db.exists():
        mode = db.stat().st_mode
        add("warn" if mode & (stat.S_IRGRP | stat.S_IROTH) else "ok", "database file",
            f"{oct(mode & 0o777)}" + (" - chmod 600 it" if mode & (stat.S_IRGRP | stat.S_IROTH) else ""))
    gi = root / ".gitignore"
    ignored = gi.read_text() if gi.exists() else ""
    for pat in ("data/*.db", "state/http_cache/", "dist/"):
        if pat not in ignored:
            add("warn", ".gitignore", f"{pat} is not ignored: the database, page cache and exports stay out of git")
    # --- privacy settings
    pv = cfg.get("privacy") or {}
    add("ok" if pv.get("retention_days") else "warn", "retention",
        f"research and screenshots kept {pv['retention_days']} days" if pv.get("retention_days") else
        "privacy.retention_days is not set: research data is kept until erased (`opencrawl purge --expired`)")
    add("ok", "subjects", "only public figures are researched (privacy.public_figures_only)"
        if pv.get("public_figures_only") else "anyone listed in subjects.yaml is researched")
    if pv.get("export_quarantine"):
        add("warn", "exports", "unverified claims are exported (privacy.export_quarantine)")
    if (cfg.get("subjects") or {}).get("bluesky_search"):
        add("warn", "subjects", "subjects.bluesky_search collects posts by members of the public")
    # --- records requests mailbox
    orr = cfg.get("openrecords") or {}
    if orr.get("enabled"):
        need = [k for k in ("ORR_SMTP_HOST", "ORR_USER", "ORR_PASSWORD", "ORR_FROM") if not os.environ.get(k)]
        add("fail" if need else "ok", "records mailbox", f"missing {', '.join(need)}" if need else "secrets present")
        port = os.environ.get("ORR_SMTP_PORT") or "465"
        add("ok" if port in ("465", "587") else "fail", "records mailbox", f"SMTP port {port} (465 TLS or 587 STARTTLS)")
        if not orr.get("requester") or str(orr.get("requester")).startswith("["):
            add("fail", "records mailbox", "openrecords.requester is not set")
    # --- encryption key for data that leaves the machine
    key = os.environ.get("OPENCRAWL_DATA_KEY")
    if key:
        from .vault import VaultError, _parse_key

        try:
            kind, _ = _parse_key(key)
            add("ok", "data key", "OPENCRAWL_DATA_KEY is a " + ("random key" if kind == 1 else "passphrase"))
        except VaultError as exc:
            add("fail", "data key", str(exc))
    elif ci:
        add("fail", "data key", "OPENCRAWL_DATA_KEY secret missing: artifacts and release assets would be unencrypted")
    else:
        add("ok", "data key", "not set (only needed where data leaves this machine: CI artifacts, backups)")
    # --- browser
    if browser:
        try:
            from . import browser as br

            with br.session(None) as ctx:
                ctx.new_page().set_content("<p>ok</p>")
            add("ok" if not br.no_sandbox_allowed() else "warn", "browser",
                "starts with its sandbox" if not br.no_sandbox_allowed() else
                "OPENCRAWL_BROWSER_ALLOW_NO_SANDBOX is set: it may run without the sandbox")
        except Exception as exc:  # noqa: BLE001
            add("fail", "browser", str(exc).splitlines()[0][:300])
    return out


def main(cfg: dict, root: Path, browser: bool = False, ci: bool = False) -> int:
    results = run(cfg, root, browser, ci)
    marks = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}
    for level, name, msg in results:
        print(f"{marks[level]}  {name:<20} {msg}")
    fails = sum(1 for r in results if r[0] == "fail")
    print(f"\n{len(results)} checks, {sum(1 for r in results if r[0] == 'warn')} warnings, {fails} failures")
    return 1 if fails else 0
