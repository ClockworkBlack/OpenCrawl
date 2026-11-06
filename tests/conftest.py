"""Test-wide settings.

The tests stand up local HTTP servers on 127.0.0.1, which the network guard refuses by default, so the suite runs
with OPENCRAWL_ALLOW_PRIVATE_NETWORKS=1. Tests of the guard itself switch it off with the `guard_on` fixture.
"""
import os

import pytest

os.environ["OPENCRAWL_ALLOW_PRIVATE_NETWORKS"] = "1"
os.environ.pop("OPENCRAWL_REDACT_LOGS", None)
os.umask(0o077)
if hasattr(os, "geteuid") and os.geteuid() == 0:
    # Chromium's sandbox can't start as root (a container); a normal user (CI) keeps it on
    os.environ.setdefault("OPENCRAWL_BROWSER_ALLOW_NO_SANDBOX", "1")


@pytest.fixture
def guard_on(monkeypatch):
    from opencrawl import netguard

    monkeypatch.delenv("OPENCRAWL_ALLOW_PRIVATE_NETWORKS", raising=False)
    netguard.configure(False, [])
    yield netguard
    netguard.configure(False, [])
