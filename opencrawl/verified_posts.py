"""Verified social posts list.

Two tables (JSONL, git-friendly), under data/verified/:

  accounts.jsonl  one row per subject (a candidate, an official, an organization) social account. An account is
                  `verified` only when an official page (campaign site, city
                  council bio, qualifying record page) links to that exact
                  profile. That link is the proof, and it is re-checked.
  posts.jsonl     one row per post. A post is `verified` when its account is
                  verified AND the post URL belongs to that account. Posts can
                  come from an open API (Bluesky, YouTube, X with a token) or
                  be added by hand with a screenshot, which gets OCR'd and
                  hashed as evidence.

Facebook and Instagram have no open read API for this, so those posts are
manual adds (URL + screenshot) only.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import extract
from .fetch import Disallowed, Fetcher
from .store import utcnow

PLATFORM_HOSTS = {
    "x": ("x.com", "twitter.com"),
    "facebook": ("facebook.com", "fb.com"),
    "instagram": ("instagram.com",),
    "bluesky": ("bsky.app",),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
    "threads": ("threads.net", "threads.com"),
    "linkedin": ("linkedin.com",),
}


def platform_of(url: str) -> str | None:
    host = urlsplit(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    for name, hosts in PLATFORM_HOSTS.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return name
    return None


def handle_of(url: str) -> str | None:
    """Account handle from a profile or post URL."""
    plat = platform_of(url)
    path = [p for p in urlsplit(url).path.split("/") if p]
    if not plat or not path:
        return None
    if plat == "bluesky" and path[0] == "profile" and len(path) > 1:
        return path[1].lower()
    if plat == "youtube":
        if path[0].startswith("@"):
            return path[0].lower()
        if path[0] in ("channel", "c", "user") and len(path) > 1:
            return path[1].lower()
        return None
    if plat == "linkedin" and path[0] == "in" and len(path) > 1:
        return path[1].lower()
    if plat == "facebook" and path[0] in ("profile.php",):
        return None
    if path[0] in ("p", "reel", "watch", "share", "permalink.php", "story.php"):
        return None  # post URL without a handle in it
    return path[0].lstrip("@").lower()


def _norm(url: str) -> str:
    s = urlsplit(url)
    return (s.netloc.lower().removeprefix("www.").removeprefix("m.") + s.path.rstrip("/").lower())


class Registry:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.acc_path = self.root / "accounts.jsonl"
        self.post_path = self.root / "posts.jsonl"

    # ------------------------------------------------------------- io
    def _read(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _write(self, path: Path, rows: list[dict]):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def accounts(self) -> list[dict]:
        return self._read(self.acc_path)

    def posts(self) -> list[dict]:
        return self._read(self.post_path)

    # ------------------------------------------------------------- accounts
    def add_account(self, subject: str, profile_url: str, proof_url: str, office: str = "",
                    target: str = "", notes: str = "") -> dict:
        plat = platform_of(profile_url)
        if not plat:
            raise ValueError(f"unrecognized social platform: {profile_url}")
        rows = self.accounts()
        key = _norm(profile_url)
        row = next((r for r in rows if _norm(r["profile_url"]) == key), None)
        if row is None:
            row = {"id": f"{plat}:{handle_of(profile_url) or key}", "added_at": utcnow()}
            rows.append(row)
        row.update({"subject": subject, "platform": plat, "handle": handle_of(profile_url),
                    "profile_url": profile_url, "proof_url": proof_url, "office": office,
                    "target": target, "notes": notes, "status": row.get("status", "pending")})
        self._write(self.acc_path, rows)
        return row

    def mark_subject(self, account_id: str, subject_id: str):
        """Tie an account to a config/subjects.yaml id (so research can pick its posts)."""
        rows = self.accounts()
        for r in rows:
            if r["id"] == account_id:
                r["subject_id"] = subject_id
        self._write(self.acc_path, rows)
        posts = self.posts()
        for p in posts:
            if p.get("account_id") == account_id:
                p["subject_id"] = subject_id
        self._write(self.post_path, posts)

    def verify_accounts(self, fetcher: Fetcher, only_subject: str | None = None) -> list[dict]:
        """Fetch each proof page and confirm it links to the profile."""
        rows = self.accounts()
        for r in rows:
            if only_subject and r.get("subject_id") != only_subject:
                continue
            if not r.get("proof_url"):
                r.update(status=r.get("status", "pending"), check_note="no proof page given", checked_at=utcnow())
                continue
            try:
                res = fetcher.get(r["proof_url"])
                page_links = extract.links(res.text, res.url) if res.status == 200 else []
            except Disallowed:
                r.update(status="pending", check_note="proof page disallowed by robots.txt; verify by hand",
                         checked_at=utcnow())
                continue
            except Exception as exc:  # noqa: BLE001
                r.update(check_note=f"proof fetch failed: {exc}"[:200], checked_at=utcnow())
                continue
            target, handle = _norm(r["profile_url"]), r.get("handle")
            hit = any(_norm(l) == target or (handle and platform_of(l) == r["platform"] and handle_of(l) == handle)
                      for l in page_links)
            r.update(status="verified" if hit else "unverified",
                     check_note="official page links to this profile" if hit else "proof page does not link to this profile",
                     checked_at=utcnow())
        self._write(self.acc_path, rows)
        self._refresh_post_status()
        return rows

    def mark_account(self, account_id: str, status: str, note: str):
        """Manual verification (e.g. proof page blocks crawlers, you checked it yourself)."""
        rows = self.accounts()
        for r in rows:
            if r["id"] == account_id:
                r.update(status=status, check_note=f"manual: {note}", checked_at=utcnow())
        self._write(self.acc_path, rows)
        self._refresh_post_status()

    # ------------------------------------------------------------- posts
    def _account_for(self, post_url: str, account_id: str | None = None) -> dict | None:
        accs = self.accounts()
        if account_id:
            return next((a for a in accs if a["id"] == account_id), None)
        plat, h = platform_of(post_url), handle_of(post_url)
        return next((a for a in accs if a["platform"] == plat and h and a.get("handle") == h), None)

    def _post_status(self, post: dict, acc: dict | None) -> str:
        if not acc or acc.get("status") != "verified":
            return "pending"
        h = handle_of(post["post_url"])
        if h is None:  # URL carries no handle (e.g. instagram.com/p/...), rely on API source or screenshot review
            return "verified" if post.get("source") == "api" else "needs_review"
        return "verified" if h == acc.get("handle") else "rejected"

    def add_post(self, post_url: str, text: str = "", posted_at: str | None = None, account_id: str | None = None,
                 screenshot: str | None = None, source: str = "manual", topics: list[str] | None = None,
                 archived_url: str = "", meta: dict | None = None) -> dict:
        rows = self.posts()
        key = _norm(post_url)
        row = next((r for r in rows if _norm(r["post_url"]) == key), None)
        if row is None:
            row = {"added_at": utcnow()}
            rows.append(row)
        acc = self._account_for(post_url, account_id)
        evidence = dict(row.get("evidence", {}))
        if screenshot:
            from .ingest import image_to_record

            ocr = image_to_record(screenshot, source_id="verified_posts", url=post_url)
            evidence.update(screenshot=str(screenshot), sha256=ocr.meta["sha256"],
                            ocr_confidence=ocr.meta["mean_confidence"], needs_review=ocr.meta["needs_review"][:10])
            text = text or ocr.text
        if archived_url:
            evidence["archived_url"] = archived_url
        row.update({
            "post_url": post_url, "platform": platform_of(post_url),
            "account_id": acc["id"] if acc else account_id, "subject": acc.get("subject", "") if acc else "",
            "subject_id": acc.get("subject_id") if acc else row.get("subject_id"),
            "posted_at": posted_at or row.get("posted_at"), "text": text or row.get("text", ""),
            "topics": topics or row.get("topics", []), "source": source, "evidence": evidence,
            "meta": {**row.get("meta", {}), **(meta or {})},
        })
        row["status"] = self._post_status(row, acc)
        self._write(self.post_path, rows)
        return row

    def _refresh_post_status(self):
        rows = self.posts()
        accs = {a["id"]: a for a in self.accounts()}
        for r in rows:
            r["status"] = self._post_status(r, accs.get(r.get("account_id")) or self._account_for(r["post_url"]))
        self._write(self.post_path, rows)

    # ------------------------------------------------------------- pulling from open APIs
    def pull(self, fetcher: Fetcher, since: str = "2016-01-01", max_per_account: int = 200,
             only_subject: str | None = None) -> int:
        """Pull recent posts for verified accounts on platforms with an open read API."""
        added = 0
        for acc in self.accounts():
            if acc.get("status") != "verified":
                continue
            if only_subject and acc.get("subject_id") != only_subject:
                continue
            fn = {"bluesky": _pull_bluesky, "youtube": _pull_youtube, "x": _pull_x}.get(acc["platform"])
            if not fn:
                continue
            try:
                for p in fn(fetcher, acc, since, max_per_account):
                    self.add_post(p["url"], p["text"], p["posted_at"], acc["id"], source="api", meta=p.get("meta"))
                    added += 1
            except Disallowed:
                continue
        return added

    def export_csv(self, path: Path, only_verified: bool = True) -> int:
        rows = [r for r in self.posts() if not only_verified or r["status"] == "verified"]
        cols = ["subject", "platform", "posted_at", "post_url", "text", "topics", "status", "source", "account_id"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({**r, "topics": ";".join(r.get("topics", []))})
        return len(rows)


# ----------------------------------------------------------------- platform pullers
def _before(ts: str | None, since: str) -> bool:
    return bool(ts) and ts[:10] < since[:10]


HANDLE_RX = {
    "x": re.compile(r"^[A-Za-z0-9_]{1,15}$"),
    "bluesky": re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+$"),
    "youtube": re.compile(r"^(@[A-Za-z0-9._-]{1,100}|UC[A-Za-z0-9_-]{22})$"),
}


def valid_handle(platform: str, handle: str | None) -> bool:
    """Handles go into API paths and parameters: only the characters each platform allows."""
    rx = HANDLE_RX.get(platform)
    return bool(handle) and (rx is None or bool(rx.match(handle)))


def _pull_bluesky(fetcher: Fetcher, acc: dict, since: str, limit: int):
    if not valid_handle("bluesky", acc.get("handle")):
        return
    url = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
    cursor, n = None, 0
    while n < limit:
        params = {"actor": acc["handle"], "limit": 100, "filter": "posts_no_replies"}
        if cursor:
            params["cursor"] = cursor
        r = fetcher.get(url, params=params, use_cache=False)
        if r.status != 200:
            return
        data = r.json()
        for item in data.get("feed", []):
            post = item.get("post", {})
            rec = post.get("record", {})
            if post.get("author", {}).get("handle", "").lower() != acc["handle"]:
                continue  # reposts of other people
            ts = rec.get("createdAt")
            if _before(ts, since):
                return
            rkey = post.get("uri", "").rsplit("/", 1)[-1]
            yield {"url": f"https://bsky.app/profile/{acc['handle']}/post/{rkey}", "text": rec.get("text", ""),
                   "posted_at": ts, "meta": {"uri": post.get("uri"), "likes": post.get("likeCount")}}
            n += 1
        cursor = data.get("cursor")
        if not cursor:
            return


def _pull_youtube(fetcher: Fetcher, acc: dict, since: str, limit: int):
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return
    api = "https://www.googleapis.com/youtube/v3"
    h = acc["handle"] or ""
    if h.lower().startswith("uc") and len(h) == 24:
        h = "UC" + h[2:]          # handle_of lower-cases; channel ids are case-sensitive, so prefer the stored URL's
        m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", acc.get("profile_url") or "")
        h = m.group(1) if m else h
    if not valid_handle("youtube", h):
        return
    hdr = {"X-goog-api-key": key}                 # the key travels in a header, never in a URL that could be logged
    params = {"part": "contentDetails"}
    params.update({"forHandle": h} if h.startswith("@") else {"id": h})
    ch = fetcher.get(f"{api}/channels", params=params, headers=hdr, use_cache=False).json()
    items = ch.get("items") or []
    if not items:
        return
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    token, n = None, 0
    while n < limit:
        p = {"part": "snippet", "playlistId": uploads, "maxResults": 50}
        if token:
            p["pageToken"] = token
        data = fetcher.get(f"{api}/playlistItems", params=p, headers=hdr, use_cache=False).json()
        for it in data.get("items", []):
            sn = it["snippet"]
            if _before(sn.get("publishedAt"), since):
                return
            vid = sn["resourceId"]["videoId"]
            yield {"url": f"https://www.youtube.com/watch?v={vid}", "posted_at": sn.get("publishedAt"),
                   "text": f"{sn.get('title','')}\n\n{sn.get('description','')}".strip(), "meta": {"video_id": vid}}
            n += 1
        token = data.get("nextPageToken")
        if not token:
            return


def _pull_x(fetcher: Fetcher, acc: dict, since: str, limit: int):
    token = os.environ.get("X_BEARER_TOKEN")
    if not token:
        return
    if not valid_handle("x", acc.get("handle")):
        return
    hdr = {"Authorization": f"Bearer {token}"}
    u = fetcher.get(f"https://api.x.com/2/users/by/username/{acc['handle']}", headers=hdr, use_cache=False).json()
    uid = str((u.get("data") or {}).get("id") or "")
    if not uid.isdigit() or len(uid) > 20:
        return
    params = {"max_results": 100, "tweet.fields": "created_at", "exclude": "replies,retweets",
              "start_time": since[:10] + "T00:00:00Z"}
    n = 0
    while n < limit:
        data = fetcher.get(f"https://api.x.com/2/users/{uid}/tweets", params=params, headers=hdr, use_cache=False).json()
        for t in data.get("data", []):
            yield {"url": f"https://x.com/{acc['handle']}/status/{t['id']}", "text": t.get("text", ""),
                   "posted_at": t.get("created_at"), "meta": {"tweet_id": t["id"]}}
            n += 1
        nxt = (data.get("meta") or {}).get("next_token")
        if not nxt:
            return
        params["pagination_token"] = nxt
