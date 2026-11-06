"""Rumor Mill - Video: videos from YouTube channels that could not be confirmed as a government's official account.

data/rumor_mill/videos.jsonl, one row per video: the target it was found for, the channel (title, handle,
id), why it is not treated as official, the video (title, date, url) and a status (unverified | confirmed |
rejected). Nothing here is cited or used as a record: it is kept apart, like the candidate Rumor Mill, until a
person confirms the channel (then add it to config/overrides.yaml with `youtube_checked`, and its videos become
records on the next run). Exported as rumor_mill_video.json.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from ..store import utcnow


class VideoRumorMill:
    def __init__(self, root: Path):
        self.path = Path(root) / "data" / "rumor_mill" / "videos.jsonl"
        self._rows: list[dict] | None = None
        self._index: dict[str, dict] = {}
        self._dirty = False
        self._lock = threading.RLock()

    def _load(self) -> list[dict]:
        if self._rows is None:
            self._rows = [json.loads(l) for l in self.path.read_text(encoding="utf-8").splitlines() if l.strip()] \
                if self.path.exists() else []
            self._index = {r["url"]: r for r in self._rows}
        return self._rows

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._load())

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

    def flush(self) -> None:
        with self._lock:
            if self._dirty and self._rows is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self._rows), encoding="utf-8")
                tmp.replace(self.path)
                self._dirty = False
