"""Topic tagging from config/topics.yaml (compiled once)."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

from .config import ROOT


@lru_cache(maxsize=4)
def _patterns(path: str) -> tuple[tuple[str, re.Pattern], ...]:
    p = Path(path)
    if not p.exists():
        return ()
    d = yaml.load(p.read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader)) or {}
    return tuple((name, re.compile(rx, re.I)) for name, rx in (d.get("topics") or {}).items())


def tag(*texts: str, path: Path | None = None) -> list[str]:
    hay = " ".join(t for t in texts if t)[:20000]
    if not hay:
        return []
    return [name for name, rx in _patterns(str(path or ROOT / "config" / "topics.yaml")) if rx.search(hay)]
