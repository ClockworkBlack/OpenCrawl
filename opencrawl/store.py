"""Record store: one SQLite database (WAL) with full-text search.

Why SQLite instead of JSONL
- upserts by natural key: when a page changes, its row is updated (version+1)
  instead of a second copy piling up next to the first
- full-text search (FTS5, bm25 ranking, snippets) over every record, so
  "every record that names this subject" is a millisecond query
  rather than a scan of every file
- a URL frontier (urls table): documents already fetched are not fetched
  again, which is what makes re-runs and interrupted backfills cheap
- per-source cursors, so a long first-time download resumes where it stopped
- one file to persist between CI runs, and deltas that merge cleanly

Record ids
- key   natural key: source_id | kind | url | natural id (Legistar ids, guid...)
- id    sha256(key)[:24]: stable across content changes; use it to upsert downstream
- hash  sha256 of the content: changes when the content does
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA_VERSION = 3


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


# meta fields that identify a record when several records share one URL
ID_FIELDS = ("EventItemId", "VoteId", "MatterId", "EventId", "OfficeRecordId", "event_item_id", "file_id", "event_id",
             "guid", "wp_id", "record_id", "tweet_id", "video_id", "uri", "account_id", "person_id")
# kinds where many distinct records legitimately share a URL and carry no id: keyed by content
CONTENT_KEYED = {"connection", "ocr"}


@dataclass
class Record:
    source_id: str
    kind: str            # page | document | feed_item | post | article | ocr | transcript | video ... (any string; the civic
                         # and research modules add ordinance, matter, meeting, agenda_item, vote, mention, statement, lead...)
    url: str
    title: str = ""
    text: str = ""
    markdown: str = ""
    published: str | None = None
    channel: str = ""    # api | wp_json | feed | sitemap | html | browser | pdf | ocr | import | store
    fetched_at: str = field(default_factory=utcnow)
    meta: dict = field(default_factory=dict)
    target: str = ""     # id of the target (organization, place, campaign...) these records belong to; "" = none
    evidence: str = ""       # primary | reported | unverified | lead
    key: str = ""            # natural key; computed when empty

    @property
    def content_hash(self) -> str:
        h = hashlib.sha256()
        for part in (self.source_id, self.kind, self.url, self.title, self.text or self.markdown,
                     json.dumps(self.meta, sort_keys=True, default=str)):
            h.update((part or "").encode())
            h.update(b"\0")
        return h.hexdigest()

    def natural_key(self) -> str:
        if self.key:
            return self.key
        nid = next((str(self.meta[f]) for f in ID_FIELDS if self.meta.get(f) not in (None, "")), "")
        if not nid and self.kind in CONTENT_KEYED:
            nid = "h:" + self.content_hash[:16]
        return f"{self.source_id}|{self.kind}|{self.url}|{nid}"

    @property
    def stable_id(self) -> str:
        return hashlib.sha256(self.natural_key().encode()).hexdigest()[:24]

    def to_dict(self) -> dict:
        return {"id": self.stable_id, "hash": self.content_hash[:24], "source_id": self.source_id, "kind": self.kind,
                "url": self.url, "title": self.title, "text": self.text, "markdown": self.markdown,
                "published": self.published, "channel": self.channel, "fetched_at": self.fetched_at,
                "meta": self.meta, "target": self.target, "evidence": self.evidence}


DDL = """
CREATE TABLE IF NOT EXISTS records(
  rid INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  id TEXT NOT NULL,
  hash TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  markdown TEXT NOT NULL DEFAULT '',
  published TEXT,
  channel TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  thash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS records_source ON records(source_id, kind);
CREATE INDEX IF NOT EXISTS records_target ON records(target, kind);
CREATE INDEX IF NOT EXISTS records_kind ON records(kind);
CREATE INDEX IF NOT EXISTS records_changed ON records(changed_at);
CREATE INDEX IF NOT EXISTS records_url ON records(url);
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
  title, text, content='records', content_rowid='rid', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
  INSERT INTO records_fts(rowid, title, text) VALUES (new.rid, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, title, text) VALUES ('delete', old.rid, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS records_au AFTER UPDATE OF title, text ON records BEGIN
  INSERT INTO records_fts(records_fts, rowid, title, text) VALUES ('delete', old.rid, old.title, old.text);
  INSERT INTO records_fts(rowid, title, text) VALUES (new.rid, new.title, new.text);
END;
CREATE TABLE IF NOT EXISTS urls(
  url TEXT PRIMARY KEY,
  source_id TEXT NOT NULL DEFAULT '',
  status INTEGER,
  content_type TEXT NOT NULL DEFAULT '',
  hash TEXT NOT NULL DEFAULT '',
  fetched_at TEXT NOT NULL,
  changed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS urls_fetched ON urls(fetched_at);
CREATE TABLE IF NOT EXISTS cursors(
  source_id TEXT NOT NULL,
  stream TEXT NOT NULL,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(source_id, stream)
);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

COLS = ("key", "id", "hash", "source_id", "target", "kind", "url", "title", "text", "markdown", "published",
        "channel", "evidence", "meta", "fetched_at", "first_seen", "changed_at", "version", "thash")


def text_hash(title: str | None, text: str | None) -> str:
    """Hash of what the full-text index holds: an update that leaves it alone needn't re-index the row."""
    return hashlib.blake2b(f"{title or ''}\0{text or ''}".encode(), digest_size=8).hexdigest()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("rid", None)
    d.pop("thash", None)
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


class Store:
    """Thread-safe SQLite record store. `root` is a directory (db at root/crawl.db) or a .db path."""

    def __init__(self, root: Path, commit_every: int = 400, commit_seconds: float = 3.0):
        root = Path(root)
        self.path = root if root.suffix == ".db" else root / "crawl.db"
        self.root = self.path.parent
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.execute("PRAGMA cache_size=-65536")  # 64 MB page cache
        # only write at startup when the schema is missing, so opening a database another process is
        # writing (e.g. `opencrawl search` during a run) never waits on its write lock
        have = self.db.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('records','urls','cursors','kv',"
                               "'records_fts')").fetchone()[0]
        if have < 5:
            self.db.executescript(DDL)
            self.db.execute("INSERT OR IGNORE INTO kv VALUES('schema', ?)", (str(SCHEMA_VERSION),))
        else:
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(records)")}
            if "thash" not in cols:
                # one-time upgrade of a database made before the column existed (rows get it on their next update)
                self.db.execute("ALTER TABLE records ADD COLUMN thash TEXT NOT NULL DEFAULT ''")
            if "target" not in cols and "jurisdiction" in cols:
                # a database made by an older release that called the target column "jurisdiction"
                self.db.execute("ALTER TABLE records RENAME COLUMN jurisdiction TO target")
                self.db.execute("DROP INDEX IF EXISTS records_jur")
                self.db.execute("CREATE INDEX IF NOT EXISTS records_target ON records(target, kind)")
        self._pending = 0
        self._last_commit = time.monotonic()
        self._commit_every, self._commit_seconds = commit_every, commit_seconds
        self._in_tx = False
        self._import_legacy_jsonl()

    # ---------------------------------------------------------------- transactions
    def _begin(self):
        if not self._in_tx:
            self.db.execute("BEGIN")
            self._in_tx = True

    def _maybe_commit(self, force: bool = False):
        self._pending += 1
        if force or self._pending >= self._commit_every or time.monotonic() - self._last_commit > self._commit_seconds:
            self.flush()

    def flush(self):
        with self._lock:
            if self._in_tx:
                self.db.execute("COMMIT")
                self._in_tx = False
            self._pending = 0
            self._last_commit = time.monotonic()

    def close(self):
        self.flush()
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- records
    def add(self, rec: Record) -> bool:
        """Insert, or update when the content changed. True if new or changed."""
        key, h = rec.natural_key(), rec.content_hash[:24]
        th = text_hash(rec.title, rec.text)
        now = utcnow()
        with self._lock:
            self._begin()
            cur = self.db.execute("SELECT rid, hash, thash FROM records WHERE key=?", (key,)).fetchone()
            if cur and cur["hash"] == h:
                self._maybe_commit()
                return False
            meta = json.dumps(rec.meta, ensure_ascii=False, default=str)
            if cur and cur["thash"] == th:
                # only meta/markdown/dates changed: leave title and text (and so the full-text index) untouched
                self.db.execute(
                    "UPDATE records SET id=?, hash=?, source_id=?, target=?, kind=?, url=?, markdown=?, published=?, "
                    "channel=?, evidence=?, meta=?, fetched_at=?, changed_at=?, version=version+1 WHERE rid=?",
                    (rec.stable_id, h, rec.source_id, rec.target or "", rec.kind, rec.url or "", rec.markdown or "",
                     rec.published, rec.channel or "", rec.evidence or "", meta, rec.fetched_at, now, cur["rid"]))
                self._maybe_commit()
                return True
            vals = (key, rec.stable_id, h, rec.source_id, rec.target or "", rec.kind, rec.url or "",
                    rec.title or "", rec.text or "", rec.markdown or "", rec.published, rec.channel or "",
                    rec.evidence or "", meta, rec.fetched_at)
            if cur:
                self.db.execute(
                    "UPDATE records SET id=?, hash=?, source_id=?, target=?, kind=?, url=?, title=?, text=?, "
                    "markdown=?, published=?, channel=?, evidence=?, meta=?, fetched_at=?, changed_at=?, "
                    "version=version+1, thash=? WHERE rid=?", vals[1:] + (now, th, cur["rid"]))
            else:
                self.db.execute(f"INSERT INTO records({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))})",
                                vals + (now, now, 1, th))
            self._maybe_commit()
            return True

    def add_many(self, recs: Iterable[Record]) -> int:
        return sum(self.add(r) for r in recs)

    def read(self, source_id: str, kind: str) -> list[dict]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM records WHERE source_id=? AND kind=? ORDER BY rid",
                                   (source_id, kind)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def read_recent(self, source_id: str, kind: str, limit: int, where: str = "", params: tuple = ()) -> list[dict]:
        """The newest `limit` records of a source and kind (by published date), decoded; an index walk, not a full read."""
        sql = ("SELECT * FROM records WHERE source_id=? AND kind=?" + (f" AND ({where})" if where else "") +
               " ORDER BY published DESC, rid DESC LIMIT ?")
        with self._lock:
            rows = self.db.execute(sql, (source_id, kind) + tuple(params) + (int(limit),)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def raw(self, where: str, params: tuple = (), cols: str = "rid, url, title, text, published, meta") -> list[sqlite3.Row]:
        """Rows without decoding meta: for scans that only need a few columns (decode `meta` with json.loads if needed)."""
        with self._lock:
            return self.db.execute(f"SELECT {cols} FROM records WHERE {where} ORDER BY rid", params).fetchall()

    def raw_iter(self, where: str, params: tuple = (), cols: str = "*", batch: int = 2000) -> Iterator[sqlite3.Row]:
        """Undecoded rows in rid order, streamed in batches (keyset pagination): exports that copy `meta` through."""
        last = 0
        while True:
            with self._lock:
                rows = self.db.execute(f"SELECT rid, {cols} FROM records WHERE rid>? AND ({where}) ORDER BY rid LIMIT {batch}",
                                       (last,) + tuple(params)).fetchall()
            if not rows:
                return
            yield from rows
            last = rows[-1]["rid"]

    def query(self, where: str = "1", params: tuple = (), order: str = "rid", limit: int | None = None) -> list[dict]:
        sql = f"SELECT * FROM records WHERE {where} ORDER BY {order}" + (f" LIMIT {int(limit)}" if limit else "")
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def iter_query(self, where: str = "1", params: tuple = (), order: str = "rid", batch: int = 2000) -> Iterator[dict]:
        """Stream rows in batches (keyset pagination) so exports don't hold everything in memory."""
        last = 0
        while True:
            with self._lock:
                rows = self.db.execute(f"SELECT * FROM records WHERE rid>? AND ({where}) ORDER BY rid LIMIT {batch}",
                                       (last,) + tuple(params)).fetchall()
            if not rows:
                return
            for r in rows:
                yield _row_to_dict(r)
            last = rows[-1]["rid"]

    def kinds(self, source_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT kind FROM records WHERE source_id=? ORDER BY kind",
                                                  (source_id,))]

    def sources(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT DISTINCT source_id FROM records ORDER BY source_id")]

    def counts(self, by: str = "kind", where: str = "1", params: tuple = ()) -> dict:
        assert by in ("kind", "source_id", "target", "evidence", "channel")
        with self._lock:
            return {r[0]: r[1] for r in self.db.execute(
                f"SELECT {by}, COUNT(*) FROM records WHERE {where} GROUP BY {by}", params)}

    def total(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    # ---------------------------------------------------------------- full-text search
    @staticmethod
    def fts_phrase(*phrases: str) -> str:
        """An FTS5 query matching any of the phrases exactly (quotes escaped, punctuation tokenized away)."""
        out = []
        for p in phrases:
            toks = "".join(ch if ch.isalnum() else " " for ch in p).split()
            if toks:
                out.append('"' + " ".join(toks).replace('"', '""') + '"')
        return " OR ".join(dict.fromkeys(out))

    def search(self, match: str, where: str = "1", params: tuple = (), limit: int = 200,
               snippet_tokens: int = 40) -> list[dict]:
        """bm25-ranked full-text search; each hit carries a `snippet` of the text around the match."""
        if not match:
            return []
        sql = (f"SELECT r.*, snippet(records_fts, 1, '', '', ' … ', {int(snippet_tokens)}) AS snippet, "
               f"bm25(records_fts) AS score FROM records_fts JOIN records r ON r.rid = records_fts.rowid "
               f"WHERE records_fts MATCH ? AND ({where}) ORDER BY score LIMIT {int(limit)}")
        with self._lock:
            rows = self.db.execute(sql, (match,) + tuple(params)).fetchall()
        return [_row_to_dict(r) for r in rows]

    def has(self, key: str) -> bool:
        """A record with this natural key exists (whatever its content)."""
        with self._lock:
            self._begin()
            return self.db.execute("SELECT 1 FROM records WHERE key=?", (key,)).fetchone() is not None

    def add_new(self, rec: Record) -> bool:
        """Insert only when no record has this key; never overwrites (keeps meta added later, e.g. entity ids)."""
        if self.has(rec.natural_key()):
            return False
        return self.add(rec)

    # ---------------------------------------------------------------- frontier
    def url_seen(self, url: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM urls WHERE url=?", (url,)).fetchone()
        return dict(r) if r else None

    def fresh(self, url: str, max_age_days: float) -> bool:
        """Fetched successfully within max_age_days (so it can be skipped)."""
        r = self.url_seen(url)
        if not r or r["status"] != 200:
            return False
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(r["fetched_at"])
        return age.total_seconds() < max_age_days * 86400

    def mark_url(self, url: str, status: int, source_id: str = "", content_type: str = "", content: bytes | None = None):
        h = hashlib.sha256(content).hexdigest()[:24] if content is not None else ""
        now = utcnow()
        with self._lock:
            self._begin()
            prev = self.db.execute("SELECT hash, changed_at FROM urls WHERE url=?", (url,)).fetchone()
            changed = now if (not prev or (h and prev["hash"] != h)) else prev["changed_at"]
            self.db.execute("INSERT INTO urls(url, source_id, status, content_type, hash, fetched_at, changed_at) "
                            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET source_id=excluded.source_id, "
                            "status=excluded.status, content_type=excluded.content_type, "
                            "hash=CASE WHEN excluded.hash<>'' THEN excluded.hash ELSE urls.hash END, "
                            "fetched_at=excluded.fetched_at, changed_at=?",
                            (url, source_id, status, content_type, h, now, changed, changed))
            self._maybe_commit()

    # ---------------------------------------------------------------- cursors
    def cursors(self, source_id: str) -> dict[str, str]:
        with self._lock:
            return {r["stream"]: r["value"] for r in
                    self.db.execute("SELECT stream, value FROM cursors WHERE source_id=?", (source_id,))}

    def set_cursor(self, source_id: str, stream: str, value: str):
        with self._lock:
            self._begin()
            self.db.execute("INSERT INTO cursors VALUES (?,?,?,?) ON CONFLICT(source_id, stream) DO UPDATE SET "
                            "value=excluded.value, updated_at=excluded.updated_at", (source_id, stream, value, utcnow()))
            self._maybe_commit(force=True)  # cursors are progress: make them durable at once

    def clear_cursors(self, source_id: str):
        with self._lock:
            self._begin()
            self.db.execute("DELETE FROM cursors WHERE source_id=?", (source_id,))
            self._maybe_commit(force=True)

    # ---------------------------------------------------------------- key/value (derivation cursors, settings)
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return r[0] if r else default

    def kv_set(self, key: str, value: str):
        with self._lock:
            self._begin()
            self.db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
            self._maybe_commit(force=True)

    def changed_since(self, since: str | None, where: str = "1", params: tuple = (), batch: int = 1000) -> Iterator[dict]:
        """Records changed after `since` (ISO), oldest change first; for derivations that keep a cursor."""
        where2 = f"({where}) AND changed_at > ?" if since else where
        params2 = tuple(params) + ((since,) if since else ())
        yield from self.iter_query(where2, params2, batch=batch)

    def delete_where(self, where: str, params: tuple = ()) -> int:
        with self._lock:
            self._begin()
            n = self.db.execute(f"DELETE FROM records WHERE {where}", params).rowcount
            self._maybe_commit(force=True)
        return n

    # ---------------------------------------------------------------- legacy JSONL (v0.1/v0.2 data/<source>/<kind>.jsonl)
    def _import_legacy_jsonl(self):
        files = [p for p in self.root.glob("*/*.jsonl") if p.parent.name not in ("verified", "rumor_mill")]
        if not files or self.db.execute("SELECT v FROM kv WHERE k='legacy_imported'").fetchone():
            return
        self.db.execute("INSERT OR REPLACE INTO kv VALUES('legacy_imported', 'started')")
        for p in files:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                self.add(Record(d["source_id"], d["kind"], d.get("url", ""), title=d.get("title", ""),
                                text=d.get("text", ""), markdown=d.get("markdown", ""), published=d.get("published"),
                                channel=d.get("channel", ""), fetched_at=d.get("fetched_at") or utcnow(),
                                meta=d.get("meta") or {}, target=d.get("target", ""),
                                evidence=d.get("evidence", "")))
        self.db.execute("INSERT OR REPLACE INTO kv VALUES('legacy_imported', ?)", (utcnow(),))
        self.flush()
