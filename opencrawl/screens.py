"""Screenshots: automated page capture, a screen-grab inbox, and a simulator to test both.

capture   Render a page in headless Chromium as a phone or a desktop, scroll so lazy
          content loads, take a full-page screenshot, and read it: the page's own text
          when the DOM has it, OCR (tile by tile) when the content is in images, canvas
          or an embedded viewer. Same rules as every other fetch: robots.txt, per-host
          spacing, and hosts that refuse automated access (state/blocked.json) are not
          captured — those are what the inbox is for.

inbox     Screenshots a person took (phone or desktop) dropped in imports/screens/
          (upload them on github.com from a phone: Add file > Upload files). Each one:
          duplicates skipped (perceptual hash); phone status/navigation bars dropped;
          dark mode handled; very tall scrolling captures read in overlapping tiles;
          consecutive shots of one scroll merged without repeated lines; platform,
          handle, date, engagement and any visible URL detected; subjects named in it
          matched. A screenshot is not an opened page, so items stay `needs_review`
          (evidence "lead") until the post/page itself is opened; a visible post URL on a
          verified account is handed to the verified-posts registry for that check.

simulate  Synthetic phone/desktop screenshots of social posts, notices and long
          scrolls (fictional people and handles only) to test the pipeline end to end.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
import shutil
import statistics
import time
from pathlib import Path

from .ingest import lines_to_markdown, preprocess
from .names import name_regex
from .store import Record, utcnow

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
INBOX_EXT = IMAGE_EXT | {".pdf"}  # a page saved as PDF (Share > Print > Save as PDF) or a records-request attachment
DEVICES = {
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3, "is_mobile": True, "has_touch": True,
              "user_agent_suffix": " Mobile"},
    "desktop": {"viewport": {"width": 1280, "height": 900}, "device_scale_factor": 1, "is_mobile": False, "has_touch": False,
                "user_agent_suffix": ""},
}
MAX_FILE_BYTES = 60_000_000   # one screenshot or PDF in the inbox
TILE = 2200          # OCR tile height (px, after preprocessing) — tesseract is faster and better on bounded tiles
TILE_OVERLAP = 160
STATUS_LINE = re.compile(r"^(\d{1,2}:\d{2}(\s?[AP]M)?|.*\b(LTE|5G|4G|3G|Wi-?Fi)\b.*|.*\d{1,3}\s?%\s*$|[\W_]{1,4})$", re.I)
NAV_LINE = re.compile(r"^(home|search|notifications|messages|profile|explore|reels|shop|menu|friends|watch|"
                      r"marketplace|[<>|O○◁△□\W_]{1,6})(\s+(home|search|notifications|messages|profile|explore|reels|"
                      r"shop|menu|friends|watch|marketplace))*$", re.I)

PLATFORMS = [  # (name, patterns that suggest it) — scored, best wins
    ("x", [r"\bRepost(s|ed)?\b", r"\bRetweet", r"\bQuote\b", r"\bViews\b", r"@\w{2,15}\s*[·•]\s*\d+[smhd]\b", r"\bx\.com/", r"twitter\.com/"]),
    ("facebook", [r"\bLike\b.*\bComment\b.*\bShare\b", r"\bfacebook\.com/", r"\bFollow\b.*\bMessage\b", r"\b\d+\s+comments?\b", r"\bMost relevant\b"]),
    ("instagram", [r"\bLiked by\b", r"\binstagram\.com/", r"\bView all \d+ comments\b", r"\bReels\b", r"\bfollowers\b.*\bfollowing\b"]),
    ("bluesky", [r"\bbsky\.app/", r"\.bsky\.social\b", r"\bReposted by\b"]),
    ("youtube", [r"\byoutube\.com/|\byoutu\.be/", r"\bsubscribers\b", r"\bviews\s*[·•]", r"\bSubscribe\b"]),
    ("tiktok", [r"\btiktok\.com/", r"\bFor You\b", r"\bFollowing\b.*\bFor You\b"]),
    ("nextdoor", [r"\bnextdoor\.com/", r"\bNeighbors?\b.*\bnearby\b"]),
    ("threads", [r"\bthreads\.(net|com)/"]),
    ("linkedin", [r"\blinkedin\.com/", r"\bConnect\b.*\bMessage\b", r"\bReactions?\b"]),
]
URL_RX = re.compile(r"\b((?:https?://)?(?:www\.|m\.)?(?<![a-z0-9.-])(?:[a-z0-9-]{1,63}\.){1,6}(?:com|org|net|gov|us|app|social|be|co|io|news|info)"
                    r"(?:/[^\s<>\"')\]]*)?)", re.I)
HANDLE_RX = re.compile(r"(?<![\w.])@([A-Za-z0-9_.]{2,30})")
DATE_RX = re.compile(r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:,\s*\d{4})?"
                     r"|\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|\d+\s?(?:[smhdw]|min|hr|hrs|mins|days?|weeks?)\b(?:\s+ago)?"
                     r"|Yesterday at \d{1,2}:\d{2}\s?[AP]M|Today at \d{1,2}:\d{2}\s?[AP]M)", re.I)
COUNT_RX = re.compile(r"(?<![\d,.])(\d[\d,.]{0,12}\s?[KkMm]?)\s{1,3}(likes?|comments?|shares?|reposts?|retweets?|views?|reactions?)\b", re.I)


# ----------------------------------------------------------------------------- image helpers
def dhash(img, size: int = 8) -> str:
    """64-bit difference hash: near-identical screenshots (re-saves, slight crops) get near-identical hashes."""
    g = img.convert("L").resize((size + 1, size))
    px = list(g.tobytes())  # one byte per pixel in mode L
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def ocr_lines(img) -> tuple[list[dict], int]:
    """OCR an image in overlapping tiles. Returns lines with position (fraction of height) and confidence."""
    import pytesseract

    pre = preprocess(img)
    h = pre.height
    out, seen_tops = [], []
    y = 0
    while y < h:
        tile = pre.crop((0, y, pre.width, min(h, y + TILE)))
        data = pytesseract.image_to_data(tile, output_type=pytesseract.Output.DICT, config="--oem 1 --psm 3")
        lines: dict[tuple, dict] = {}
        for i, word in enumerate(data["text"]):
            word = (word or "").strip()
            conf = float(data["conf"][i])
            if not word or conf < 0:
                continue
            k = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            ln = lines.setdefault(k, {"words": [], "confs": [], "top": data["top"][i] + y, "h": data["height"][i]})
            ln["words"].append(word)
            ln["confs"].append(conf)
        for k in sorted(lines):
            ln = lines[k]
            top = ln["top"]
            if any(abs(top - t) < 12 for t in seen_tops):  # same line read again in the overlap band
                continue
            seen_tops.append(top)
            out.append({"block": k[0] + y * 1000, "par": k[1], "text": " ".join(ln["words"]),
                        "conf": statistics.mean(ln["confs"]), "top": top, "frac": top / h})
        if y + TILE >= h:
            break
        y += TILE - TILE_OVERLAP
    out.sort(key=lambda l: l["top"])
    return out, h


def strip_chrome(lines: list[dict], phone: bool) -> tuple[list[dict], list[str]]:
    """Drop the phone status bar (time, signal, battery) and bottom navigation bar."""
    if not phone:
        return lines, []
    keep, dropped = [], []
    for ln in lines:
        t = ln["text"].strip()
        if ln["frac"] < 0.045 and (STATUS_LINE.match(t) or len(t) <= 14):
            dropped.append(t)
        elif ln["frac"] > 0.94 and NAV_LINE.match(t):
            dropped.append(t)
        else:
            keep.append(ln)
    return keep, dropped


def looks_like_phone(img) -> bool:
    return img.height >= img.width * 1.6 and img.width <= 1600


def detect(text: str) -> dict:
    """Platform, handles, dates, engagement and URLs visible in the screenshot text."""
    scores = {}
    for name, pats in PLATFORMS:
        s = sum(1 for p in pats if re.search(p, text, re.I))
        if s:
            scores[name] = s
    urls = []
    for m in URL_RX.finditer(text):
        u = m.group(1).rstrip(".,;:")
        if "." in u.split("/")[0] and len(u) > 6:
            urls.append(u if u.lower().startswith("http") else "https://" + u)
    counts = {}
    for n, what in COUNT_RX.findall(text):
        counts.setdefault(what.lower().rstrip("s"), n.replace(" ", ""))
    handles = list(dict.fromkeys(HANDLE_RX.findall(text)))
    from_urls = [m.group(1) for u in urls for m in [re.search(r"(?:x|twitter|instagram|threads)\.(?:com|net)/@?([A-Za-z0-9_.]{2,30})/", u)
                                                    or re.search(r"bsky\.app/profile/([A-Za-z0-9_.-]{2,60})/", u)] if m]
    for h in from_urls:  # a handle in a URL is read more reliably than the OCR'd "@handle" line; it wins
        handles = [h] + [x for x in handles if x != h and not h.startswith(x)]
    return {"platform": max(scores, key=scores.get) if scores else None, "platform_scores": scores,
            "handles": handles[:5],
            "dates": list(dict.fromkeys(m.group(1) for m in DATE_RX.finditer(text)))[:5],
            "engagement": counts, "urls": list(dict.fromkeys(urls))[:5]}


def _same_line(a: str, b: str) -> bool:
    """OCR of one line in two shots differs in a character or two (often where the line is clipped)."""
    from difflib import SequenceMatcher

    a, b = re.sub(r"\W+", "", a.lower()), re.sub(r"\W+", "", b.lower())
    if not a or not b:
        return False
    return a == b or (min(len(a), len(b)) >= 8 and SequenceMatcher(None, a, b).ratio() >= 0.9)


def merge_overlap(prev: list[str], cur: list[str], min_run: int = 2) -> list[str]:
    """Join two consecutive captures of one scroll: find the longest run of cur's opening lines (allowing a clipped
    first line or two) that repeats in prev's tail, and keep only what comes after it."""
    tail = prev[-40:]
    best = None  # (run length, index in cur where the new material starts)
    for start in range(0, min(3, len(cur))):
        for i in range(len(tail)):
            k = 0
            while start + k < len(cur) and i + k < len(tail) and _same_line(tail[i + k], cur[start + k]):
                k += 1
            if k >= min_run and i + k == len(tail) or (k >= min_run and i + k >= len(tail) - 2):
                if not best or k > best[0]:
                    best = (k, start + k)
    return cur[best[1]:] if best else cur


# ----------------------------------------------------------------------------- reading one screenshot
def read_screenshot(data: bytes, subjects: list[dict] | None = None) -> dict:
    from PIL import ImageOps

    from .ingest import open_image

    img = ImageOps.exif_transpose(open_image(data))
    if getattr(img, "n_frames", 1) > 1:
        img.seek(0)
    img = img.convert("RGB")
    phone = looks_like_phone(img)
    lines, h = ocr_lines(img)
    lines, dropped = strip_chrome(lines, phone)
    text, md = lines_to_markdown(lines)
    det = detect("\n".join(l["text"] for l in lines))
    matched = []
    for c in subjects or []:
        if c.get("variants") and name_regex(c["variants"]).search(text):
            matched.append(c["id"])
    confs = [l["conf"] for l in lines]
    return {"text": text, "markdown": md, "lines": [l["text"] for l in lines], "phone": phone, "dropped_chrome": dropped,
            "width": img.width, "height": img.height, "tiles": max(1, -(-h // (TILE - TILE_OVERLAP))),
            "mean_confidence": round(statistics.mean(confs), 1) if confs else 0.0,
            "needs_review": [l["text"] for l in lines if l["conf"] < 70][:30],
            "sha256": hashlib.sha256(data).hexdigest(), "dhash": dhash(img), "subjects": matched, **det}


BOXES = ("ordinances", "posts", "text")
ORDINANCE_RX = re.compile(r"\b(ordinance\s+(no\.?|number|#)|an ordinance|be it ordained|sec(tion)?\.?\s*\d+[-.]\d+|"
                          r"§\s*\d|code of ordinances|article\s+[ivxlc\d]+\b|chapter\s+\d+|resolution\s+(no\.?|number)|"
                          r"public hearing|zoning (code|ordinance)|amend(ing|ment)? (the )?(code|ordinance))", re.I)


def classify(info: dict, hint: str | None = None) -> str:
    """Which box a screenshot belongs in: the subfolder it was uploaded to wins, else what it looks like."""
    if hint in BOXES:
        return hint
    text = info.get("text", "")
    if len(ORDINANCE_RX.findall(text)) >= 1 and not info.get("platform"):
        return "ordinances"
    if info.get("platform") or info.get("handles"):
        return "posts"
    return "text"


STATE_SUFFIX = {"abbr": "", "name": ""}   # set by configure(): "X, CO" / "X, Colorado" counts as naming the place X


def configure(civic_cfg: dict | None) -> None:
    c = civic_cfg or {}
    STATE_SUFFIX["abbr"], STATE_SUFFIX["name"] = c.get("state_abbr") or "", c.get("state_name") or ""


def match_targets(text: str, targets: list[dict]) -> list[str]:
    """Registry ids named in the text. Counties need "County"; one-word place names (often also surnames: Morgan,
    Brooks, Jackson) need context — "City of X", "Town of X", "X City Council/Commission", "X, <state>"; multi-word
    names ("Castle Rock") and non-civic targets match on their own. Longer names first so "East Point" beats "Point"."""
    out, taken = [], []
    st = "|".join(re.escape(x) for x in (STATE_SUFFIX["abbr"], STATE_SUFFIX["name"]) if x) or "(?!x)x"
    for j in sorted(targets, key=lambda j: -len(j["name"])):
        n = re.escape(j["name"])
        if j.get("kind") == "county":
            rx = re.compile(rf"\b{n}\b", re.I)  # registry names already end in "County"
        elif " " in j["name"].strip() or "-" in j["name"] or j.get("kind") not in ("city", "town", "village", "borough", "township", "consolidated"):
            rx = re.compile(rf"\b{n}\b(?!\s+(street|st\.|road|rd\.|avenue|ave\.?|drive|dr\.|county))", re.I)
        else:
            rx = re.compile(rf"\b(city|town|village) of {n}\b|\b{n}\s+(city|town)\s+(council|commission|hall|clerk|"
                            rf"manager|attorney|limits|code)\b|\b{n},?\s+({st})\b|\bmayor of {n}\b", re.I)
        for m in rx.finditer(text):
            if not any(a <= m.start() < b for a, b in taken):
                taken.append((m.start(), m.end()))
                out.append(j["id"])
                break
    return out[:5]


def read_pdf(data: bytes, subjects: list[dict] | None = None) -> dict:
    """A saved page or emailed PDF: exact text from the text layer, OCR only for scanned pages."""
    from .ingest import pdf_to_record

    rec = pdf_to_record(data)
    text = rec.text
    det = detect(text[:20000])
    matched = [c["id"] for c in subjects or [] if c.get("variants") and name_regex(c["variants"]).search(text)]
    lines = [l for l in text.splitlines() if l.strip()]
    return {"text": text, "markdown": rec.markdown, "lines": lines, "phone": False, "dropped_chrome": [],
            "width": None, "height": None, "tiles": rec.meta.get("pages"), "pages": rec.meta.get("pages"),
            "mean_confidence": rec.meta.get("mean_confidence") or 100.0, "needs_review": [],
            "sha256": hashlib.sha256(data).hexdigest(), "dhash": None, "subjects": matched, **det}


# ----------------------------------------------------------------------------- inbox
class Inbox:
    """imports/screens/ -> records. Processed files move to imports/screens/processed/."""

    def __init__(self, root: Path, store, subjects: list[dict] | None = None, log=print,
                 targets: list[dict] | None = None):
        self.root, self.store, self.log = root, store, log
        self.targets = targets or []
        self.dir = root / "imports" / "screens"
        self.done = self.dir / "processed"
        self.seen_path = root / "state" / "screens_seen.json"
        self.seen = json.loads(self.seen_path.read_text()) if self.seen_path.exists() else {}
        self.subjects = subjects or []

    def _dupe(self, sha: str, dh: str) -> str | None:
        if sha in self.seen:
            return self.seen[sha]["record_id"]
        for v in self.seen.values():
            if dh and v.get("dhash") and hamming(v["dhash"], dh) <= 3:
                return v["record_id"]
        return None

    def ingest(self, files: list[Path] | None = None, source_id: str = "screens", subject_id: str | None = None,
               note: str = "", move: bool = True) -> dict:
        if files is None:  # imports/screens/ and its box subfolders (ordinances/, posts/, text/)
            files = [p for d in [self.dir] + [self.dir / b for b in BOXES] for p in d.glob("*")
                     if p.is_file() and not p.is_symlink() and p.suffix.lower() in INBOX_EXT]
        files = sorted(files, key=lambda p: (p.parent.name, p.name))
        stats = {"files": len(files), "new": 0, "duplicates": 0, "merged_into_previous": 0, "errors": 0}
        prev = None  # (record, lines so far) of the previous shot, to merge consecutive shots of one scroll
        for f in files:
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError(f"file over {MAX_FILE_BYTES // 1_000_000} MB")
                data = f.read_bytes()
                info = read_pdf(data, self.subjects) if f.suffix.lower() == ".pdf" else read_screenshot(data, self.subjects)
            except Exception as exc:  # noqa: BLE001
                self.log(f"{f.name}: could not read ({type(exc).__name__}: {str(exc)[:200]})")
                stats["errors"] += 1
                continue
            dup = self._dupe(info["sha256"], info["dhash"])
            if dup:
                stats["duplicates"] += 1
                self.log(f"{f.name}: duplicate of {dup}")
                self._finish(f, move)
                continue
            cands = sorted(set(info["subjects"]) | ({subject_id} if subject_id else set()))
            # part of the same scroll as the previous shot? (at least 3 consecutive lines repeat)
            if prev is not None and info["lines"] and f.suffix.lower() != ".pdf":
                rest = merge_overlap(prev[1], info["lines"], min_run=3)
                if len(rest) < len(info["lines"]):
                    rec = prev[0]
                    for k in ("platform",):
                        rec.meta[k] = rec.meta.get(k) or info[k]
                    rec.meta["handles"] = list(dict.fromkeys((rec.meta.get("handles") or []) + info["handles"]))[:5]
                    rec.meta["urls_seen"] = list(dict.fromkeys((rec.meta.get("urls_seen") or []) + info["urls"]))[:5]
                    rec.meta["engagement"] = {**info["engagement"], **(rec.meta.get("engagement") or {})}
                    if not rec.meta.get("box_from_folder"):
                        rec.meta["box"] = classify({"text": rec.text + "\n" + "\n".join(rest), "platform": rec.meta["platform"],
                                                    "handles": rec.meta["handles"]})
                    rec.text = (rec.text + "\n" + "\n".join(rest)).strip()
                    rec.markdown = (rec.markdown + "\n\n" + "\n\n".join(rest)).strip()
                    rec.meta["files"].append(f.name)
                    rec.meta["sha256"].append(info["sha256"])
                    rec.meta["subjects"] = sorted(set(rec.meta["subjects"]) | set(cands))
                    self.store.add(rec)
                    self.seen[info["sha256"]] = {"record_id": rec.stable_id, "dhash": info["dhash"], "file": f.name}
                    prev = (rec, prev[1] + rest)
                    stats["merged_into_previous"] += 1
                    self.log(f"{f.name}: continues {rec.meta['files'][0]} ({len(rest)} new lines)")
                    self._finish(f, move)
                    continue
            box = classify(info, f.parent.name if f.parent.name in BOXES else None)
            jids = match_targets(info["text"], self.targets)
            pre = f.name.split("__", 1)[0] if "__" in f.name else ""  # "<target id>__file.pdf" from a records reply
            if pre and any(j["id"] == pre for j in self.targets):
                jids = [pre] + [x for x in jids if x != pre]
            title = next((l for l in info["lines"] if len(l) > 20), info["lines"][0] if info["lines"] else f.name)[:140]
            rec = Record(source_id, "screenshot", f"file://{f.name}", title=title, text=info["text"], markdown=info["markdown"],
                         channel="ocr", evidence="lead", key=f"{source_id}|screenshot|{info['sha256']}",
                         target=jids[0] if jids else "",
                         meta={"box": box, "box_from_folder": f.parent.name in BOXES, "targets": jids, "files": [f.name], "sha256": [info["sha256"]],
                               "dhash": info["dhash"],
                               "platform": info["platform"], "handles": info["handles"], "dates_seen": info["dates"],
                               "engagement": info["engagement"], "urls_seen": info["urls"], "subjects": cands,
                               "phone": info["phone"], "dropped_chrome": info["dropped_chrome"], "pages": info.get("pages"),
                               "file_type": f.suffix.lower().lstrip("."),
                               "mean_confidence": info["mean_confidence"], "needs_review": info["needs_review"],
                               "status": "needs_review", "note": note, "received_at": utcnow(),
                               "why": "screenshot: open the original post or page before citing it"})
            self._link_posts(rec, info, cands)
            self.store.add(rec)
            self.seen[info["sha256"]] = {"record_id": rec.stable_id, "dhash": info["dhash"], "file": f.name}
            stats["new"] += 1
            self.log(f"{f.name}: box={box} platform={info['platform'] or '-'} handles={info['handles'] or '-'} "
                     f"places={jids or '-'} subjects={cands or '-'} confidence={info['mean_confidence']}")
            prev = (rec, info["lines"])
            self._finish(f, move)
        self.store.flush()
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(json.dumps(self.seen, indent=1))
        return stats

    def _link_posts(self, rec: Record, info: dict, cands: list[str]):
        """A visible post URL goes to the verified-posts registry, which decides (by account) if it can be trusted."""
        from .verified_posts import Registry, platform_of

        reg = Registry(self.root / "data" / "verified")
        for u in info["urls"]:
            if platform_of(u) and re.search(r"/(status|post|p|reel|videos?|watch|posts|permalink)\b", u):
                row = reg.add_post(u, text=rec.text[:2000], source="screenshot",
                                   meta={"screenshot_record": rec.stable_id})
                rec.meta.setdefault("posts_linked", []).append({"url": u, "status": row["status"]})

    def _finish(self, f: Path, move: bool):
        if move and (f.parent == self.dir or f.parent.parent == self.dir):
            dest = self.done / (f.parent.name if f.parent != self.dir else "")
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest / f.name))

    @staticmethod
    def box_export(store, include_text: bool = True) -> dict:
        """The screenshot inbox: every item grouped by ordinances / posts / text. Without include_text (settings.yaml
        privacy.export_screenshot_text: false) the OCR text stays out of exports: screenshots of social media show
        other people's names and words."""
        out = {b: [] for b in BOXES}
        for r in store.query("kind='screenshot'", order="changed_at DESC"):
            m = r["meta"]
            out.setdefault(m.get("box", "text"), []).append({
                "id": r["id"], "title": r["title"] if include_text else "", "text": r["text"] if include_text else "",
                "box": m.get("box"), "status": m.get("status"),
                "platform": m.get("platform"), "handles": m.get("handles"), "dates_seen": m.get("dates_seen"),
                "urls_seen": m.get("urls_seen"), "posts_linked": m.get("posts_linked", []),
                "targets": m.get("targets", []), "subjects": m.get("subjects", []),
                "files": m.get("files"), "sha256": m.get("sha256"), "mean_confidence": m.get("mean_confidence"),
                "needs_review": m.get("needs_review"), "received_at": m.get("received_at"), "note": m.get("note", ""),
                "why": m.get("why")})
        return out


# ----------------------------------------------------------------------------- automated capture
def capture_page(url: str, fetcher, device: str = "phone", selector: str | None = None, scroll: bool = True,
                 max_height: int = 20000, wait_ms: int = 1500, save_to: Path | None = None) -> dict:
    """Screenshot a page the way a person would and read it, in the guarded headless browser (browser.session).
    Refuses robots-disallowed and blocked hosts."""
    from . import browser

    fetcher._admit(url)           # guard, block list, robots.txt: refuse before starting a browser
    d = DEVICES[device]
    t0 = time.monotonic()
    with browser.session(fetcher, block=("media",), viewport=d["viewport"], device_scale_factor=d["device_scale_factor"],
                         is_mobile=d["is_mobile"], has_touch=d["has_touch"],
                         user_agent=fetcher.user_agent + d["user_agent_suffix"]) as ctx:
        page = ctx.new_page()
        resp = browser.goto(page, url, fetcher)
        status = resp.status if resp else 0
        if status != 200:
            return {"url": url, "status": status}
        if scroll:  # let lazy-loaded content arrive, like a person scrolling down
            last = 0
            for _ in range(30):
                h = page.evaluate("document.documentElement.scrollHeight")
                if h == last or h > max_height:
                    break
                last = h
                page.evaluate("window.scrollBy(0, window.innerHeight)")
                page.wait_for_timeout(250)
            page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(wait_ms)
        target = page.locator(selector).first if selector else None
        png = target.screenshot() if target else page.screenshot(full_page=True)
        dom_text = (target.inner_text() if target else page.inner_text("body")) or ""
        title = page.title()
        final = page.url
    info = {"url": final, "status": 200, "title": title, "png": png, "dom_text": dom_text.strip(), "device": device,
            "seconds": round(time.monotonic() - t0, 1)}
    if save_to:
        save_to.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(png).hexdigest()[:16] + ".png"
        (save_to / name).write_bytes(png)
        info["saved"] = str(save_to / name)
    return info


def capture_record(url: str, fetcher, source_id: str = "capture", device: str = "phone", selector: str | None = None,
                   save_to: Path | None = None, kind: str = "page") -> Record | None:
    cap = capture_page(url, fetcher, device=device, selector=selector, save_to=save_to)
    if cap.get("status") != 200:
        return None
    png = cap["png"]
    if len(cap["dom_text"]) >= 200:  # the page's own text is exact; OCR only adds errors
        text, md, method, conf = cap["dom_text"], cap["dom_text"], "dom", None
    else:
        info = read_screenshot(png)
        text, md, method, conf = info["text"], info["markdown"], "ocr", info["mean_confidence"]
    return Record(source_id, kind, cap["url"], title=cap["title"], text=text, markdown=md, channel="browser",
                  evidence="primary",
                  meta={"captured_as": cap["device"], "text_from": method, "mean_confidence": conf, "opened": True,
                        "screenshot_sha256": hashlib.sha256(png).hexdigest(), "screenshot_file": cap.get("saved")})


# ----------------------------------------------------------------------------- simulator
SIM_PEOPLE = [("Rowan Teller", "rowanteller_co"), ("Harper Bennett", "harperforcouncil"), ("Emerson Vance", "emersonvance4mayor")]
SIM_TEXTS = [
    "Thanks to everyone who joined the library board listening session last night. The branch hours survey closes "
    "Friday, and I will push for evening hours before any new construction is approved.",
    "Our crosswalk request for Maple Avenue is finally on the transportation committee agenda. Please share your "
    "experience walking to school there; written comments are due Wednesday at noon.",
    "The draft budget adds two park rangers and keeps the recycling pickup weekly. I support both and will ask for "
    "a public workshop on the fee schedule before the second reading.",
]


def _font(size: int):
    from PIL import ImageFont

    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/System/Library/Fonts/Helvetica.ttc", "C:/Windows/Fonts/arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= width:
            cur = t
        else:
            lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])


def simulate(out: Path, n: int = 6, seed: int = 1) -> list[Path]:
    """Write synthetic screenshots: phone posts (light/dark) for several platforms, a long scrolling capture split
    into two overlapping shots, a desktop page with a visible URL, and an exact duplicate. Fictional names only."""
    import random

    from PIL import Image, ImageDraw

    rnd = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    files = []

    def phone_post(name, handle, body, platform, dark, extra_lines=0, url=None):
        W, H = 1170, 2532
        bg, fg, muted = ((0, 0, 0), (235, 235, 235), (140, 140, 140)) if dark else ((255, 255, 255), (15, 15, 15), (110, 110, 110))
        img = Image.new("RGB", (W, H), bg)
        d = ImageDraw.Draw(img)
        f_s, f_m, f_l = _font(38), _font(46), _font(52)
        d.text((60, 40), "9:41", fill=fg, font=f_s)
        d.text((W - 260, 40), "5G 87%", fill=fg, font=f_s)
        y = 180
        if url:
            d.rounded_rectangle((40, y, W - 40, y + 90), 30, fill=(40, 40, 40) if dark else (235, 235, 235))
            d.text((80, y + 22), url, fill=fg, font=f_s)
            y += 150
        d.ellipse((60, y, 180, y + 120), fill=(90, 120, 200))
        d.text((210, y + 6), name, fill=fg, font=f_l)
        when = rnd.choice(["2h", "Oct 28", "Yesterday at 6:15 PM"])
        meta = f"@{handle} · {when}" if platform in ("x", "bluesky") else when
        d.text((210, y + 70), meta, fill=muted, font=f_s)
        y += 190
        body_lines = _wrap(d, body, f_m, W - 120)
        body_lines += [f"Line {i + 1} of a longer thread about the budget workshop and trail repairs." for i in range(extra_lines)]
        for ln in body_lines:
            d.text((60, y), ln, fill=fg, font=f_m)
            y += 70
            if y > H - 400:
                break
        y += 40
        foot = {"x": "12 Reposts   48 Likes   2,310 Views", "facebook": "Like    Comment    Share     37 comments",
                "instagram": "Liked by neighbor_sam and 212 others", "bluesky": "Reposted by 9 people   31 likes"}[platform]
        d.text((60, min(y, H - 330)), foot, fill=muted, font=f_s)
        d.text((60, H - 140), "Home  Search  Notifications  Messages", fill=muted, font=f_s)
        return img

    plats = ["x", "facebook", "instagram", "bluesky"]
    for i in range(max(1, n - 3)):
        name, handle = SIM_PEOPLE[i % len(SIM_PEOPLE)]
        img = phone_post(name, handle, SIM_TEXTS[i % len(SIM_TEXTS)], plats[i % len(plats)], dark=bool(i % 2),
                         url=f"x.com/{handle}/status/18{i}0042" if plats[i % len(plats)] == "x" and i == 0 else None)
        p = out / f"IMG_{100 + i}.png"
        img.save(p)
        files.append(p)
    # a long scroll captured as two overlapping shots
    name, handle = SIM_PEOPLE[0]
    long = phone_post(name, handle, SIM_TEXTS[0], "facebook", dark=False, extra_lines=40)
    W, H = long.size
    top, bottom = long.crop((0, 0, W, int(H * 0.62))), long.crop((0, int(H * 0.45), W, H))
    for j, part in enumerate((top, bottom)):
        canvas = Image.new("RGB", (W, H), (255, 255, 255))
        canvas.paste(part, (0, 0 if j == 0 else 0))
        p = out / f"IMG_{200 + j}.png"
        canvas.save(p)
        files.append(p)
    # desktop capture of a city notice with the address bar visible
    img = Image.new("RGB", (1600, 1000), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1600, 70), fill=(230, 230, 230))
    d.text((30, 18), "https://www.examplecity.gov/notices/public-hearing", fill=(20, 20, 20), font=_font(30))
    y = 120
    for ln in ["NOTICE OF PUBLIC HEARING", "The Mayor and Council will hold a public hearing on November 17, 2026",
               "at 6:00 PM on an ordinance amending the zoning code for short-term rentals.",
               "Council member Harper Bennett requested the item be placed on the agenda."]:
        d.text((60, y), ln, fill=(10, 10, 10), font=_font(38))
        y += 70
    p = out / "desktop_notice.png"
    img.save(p)
    files.append(p)
    # an exact duplicate (re-saved) of the first shot
    dup = out / "IMG_100_copy.jpg"
    Image.open(files[0]).convert("RGB").save(dup, quality=95)
    files.append(dup)
    return files
