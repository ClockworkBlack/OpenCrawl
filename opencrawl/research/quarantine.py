"""The quarantine: material that could not be verified, kept apart from every record.

claims   data/quarantine/claims.jsonl. Anything said about a subject that came from a source that is neither an
         official record, the subject's own channel nor a listed outlet: a blog, a forum, an unknown site. Each row
         has the subject, url, the claim text (1,000 characters at most), where it was found, a status
         (unverified | corroborated | debunked | retracted) and the corroboration trail. Never part of a profile
         or packet's evidence; `opencrawl quarantine corroborate ID --evidence URL` records a verified source.
videos   data/quarantine/videos.jsonl. Videos from channels that could not be confirmed as a target's official
         account (see transcripts.py), with the reason. Confirm a channel by hand (config/overrides.yaml,
         `youtube_checked`) and its videos become records on the next run.

Neither file is exported unless settings.yaml `privacy.export_quarantine` is true: unverified claims about named
people are for review, not for publication.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import urlsplit

from ..store import utcnow

STATUSES = ("unverified", "corroborated", "debunked", "retracted")


def _domain(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


class _JsonLines:
    """Rows cached in memory, written on flush (rewriting the file per row doesn't scale), atomically."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._rows: list[dict] | None = None
        self._dirty = False
        self._lock = threading.RLock()

    def _key(self, r: dict):
        raise NotImplementedError

    def _load(self) -> list[dict]:
        if self._rows is None:
            self._rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
                          if line.strip()] if self.path.exists() else []
            self._index = {self._key(r): r for r in self._rows}
        return self._rows

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._load())

    def flush(self) -> None:
        with self._lock:
            if self._dirty and self._rows is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self._rows), encoding="utf-8")
                tmp.replace(self.path)
                self._dirty = False

    def drop(self, predicate) -> int:
        """Remove rows for good (erasure, retention). Returns how many."""
        with self._lock:
            rows = self._load()
            keep = [r for r in rows if not predicate(r)]
            n = len(rows) - len(keep)
            if n:
                self._rows = keep
                self._index = {self._key(r): r for r in keep}
                self._dirty = True
                self.flush()
            return n


class Quarantine(_JsonLines):
    """Unverified claims about subjects."""

    def __init__(self, root: Path):
        super().__init__(Path(root) / "data" / "quarantine" / "claims.jsonl")
        self._index: dict[tuple, dict] = {}

    def _key(self, r: dict):
        return r["subject_id"], r["url"].strip().lower()

    def add(self, subject_id: str, url: str, claim: str, origin: str, seen_at: str | None = None,
            author: str = "", meta: dict | None = None, opened: bool = False) -> dict:
        with self._lock:
            rows = self._load()
            k = (subject_id, url.strip().lower())
            row = self._index.get(k)
            if row:
                row["last_seen"] = utcnow()
                row["claim"] = row["claim"] or claim[:1000]
                row["opened"] = row.get("opened", False) or opened
            else:
                n = max([int(r["id"][1:]) for r in rows if r.get("id", "")[1:].isdigit()] or [0]) + 1
                row = {"id": f"c{n:05d}", "subject_id": subject_id, "url": url, "domain": _domain(url),
                       "claim": claim[:1000], "origin": origin, "author": author, "status": "unverified",
                       "opened": opened, "first_seen": seen_at or utcnow(), "last_seen": utcnow(), "corroboration": [],
                       "notes": "", "meta": meta or {}}
                rows.append(row)
                self._index[k] = row
            self._dirty = True
            return row

    def set_status(self, claim_id: str, status: str, evidence_url: str = "", note: str = "") -> dict | None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        with self._lock:
            for r in self._load():
                if r["id"] == claim_id:
                    r["status"] = status
                    if evidence_url:
                        r["corroboration"].append({"url": evidence_url, "at": utcnow(), "note": note})
                    elif note:
                        r["notes"] = (r["notes"] + "\n" + note).strip()
                    self._dirty = True
                    self.flush()
                    return r
        return None


class VideoQuarantine(_JsonLines):
    """Videos from channels not confirmed as a target's official account."""

    def __init__(self, root: Path):
        super().__init__(Path(root) / "data" / "quarantine" / "videos.jsonl")
        self._index: dict[str, dict] = {}

    def _key(self, r: dict):
        return r["url"]

    def add(self, target: str, channel: dict, reason: str, video: dict) -> dict:
        with self._lock:
            rows = self._load()
            row = self._index.get(video["url"])
            if row:
                row["last_seen"] = utcnow()
                row["reason"] = reason
            else:
                row = {"id": f"v{len(rows) + 1:05d}", "target": target, "url": video["url"],
                       "title": video.get("title", ""), "published": video.get("published"),
                       "channel": {k: channel.get(k) for k in ("id", "title", "handle", "url")},
                       "reason": reason, "status": "unverified", "first_seen": utcnow(), "last_seen": utcnow()}
                rows.append(row)
                self._index[video["url"]] = row
            self._dirty = True
            return row
