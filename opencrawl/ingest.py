"""Screenshot / scanned image / PDF -> clean text in the best shape for ingestion.

Output for every input is one Record with:
  text      plain text, reading order, hyphenation repaired
  markdown  paragraphs + ordinance-aware headings (Sec. 2-14, ARTICLE IV, ...)
  meta      mean OCR confidence, low-confidence lines to review, sha256 of the
            original file, page count, engine, and how it was captured

Screenshots are for content a person captured themselves (posts, pages a
crawler isn't allowed to fetch, image-only notices). The automated `capture`
path only runs on URLs robots.txt allows; it exists for pages whose text is
locked in images or canvas, not to get around a site's rules.
"""
from __future__ import annotations

import hashlib
import io
import re
import statistics
from pathlib import Path

from .extract import ORDINANCE_HEADING
from .store import Record

LOW_CONF = 70


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preprocess(img):
    """Grayscale, upscale small captures, boost contrast, binarize. Big accuracy win on phone screenshots."""
    from PIL import ImageOps, ImageFilter

    img = ImageOps.exif_transpose(img).convert("L")
    if img.width < 1600:
        scale = 1600 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    # dark-mode screenshots: invert so text is dark on light
    hist = img.histogram()
    dark = sum(hist[:128]) > sum(hist[128:])
    if dark:
        img = ImageOps.invert(img)
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.filter(ImageFilter.SHARPEN)
    return img


def _lines_from_tesseract(img) -> list[dict]:
    import pytesseract

    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--oem 1 --psm 3")
    lines: dict[tuple, dict] = {}
    for i, word in enumerate(data["text"]):
        word = (word or "").strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        ln = lines.setdefault(key, {"words": [], "confs": [], "top": data["top"][i], "left": data["left"][i]})
        ln["words"].append(word)
        ln["confs"].append(conf)
    out = []
    for key in sorted(lines, key=lambda k: (k[0], k[1], k[2])):
        ln = lines[key]
        out.append({"block": key[0], "par": key[1], "text": " ".join(ln["words"]),
                    "conf": statistics.mean(ln["confs"]), "top": ln["top"]})
    return out


def lines_to_markdown(lines: list[dict]) -> tuple[str, str]:
    """Group OCR lines into paragraphs; repair hyphenated line breaks; mark ordinance headings."""
    paras: list[list[str]] = []
    last = None
    heading_open = False
    for ln in lines:
        key = (ln["block"], ln["par"])
        is_heading = bool(ORDINANCE_HEADING.match(ln["text"])) and len(ln["text"]) < 160
        # headings always stand alone, even when OCR puts them in a body paragraph
        if key != last or is_heading or heading_open:
            paras.append([])
            last = key
        paras[-1].append(ln["text"])
        heading_open = is_heading
    md_blocks, txt_blocks = [], []
    for p in paras:
        joined = ""
        for piece in p:
            if joined.endswith("-") and piece[:1].islower():
                joined = joined[:-1] + piece
            else:
                joined = (joined + " " + piece).strip()
        joined = re.sub(r"\s+", " ", joined)
        if not joined:
            continue
        # OCR often splits one sentence across "paragraphs": rejoin mid-sentence breaks
        if (txt_blocks and md_blocks and not md_blocks[-1].startswith("#")
                and not re.search(r"[.:;!?)]$", txt_blocks[-1]) and joined[:1].islower()):
            txt_blocks[-1] += " " + joined
            md_blocks[-1] += " " + joined
            continue
        txt_blocks.append(joined)
        if ORDINANCE_HEADING.match(joined) and len(joined) < 160:
            md_blocks.append("### " + joined)
        elif len(joined) < 80 and joined.isupper() and len(joined.split()) <= 10:
            md_blocks.append("## " + joined.title())
        elif re.match(r"^\(?[a-z0-9]{1,3}[\).]\s", joined):
            md_blocks.append("- " + joined)
        else:
            md_blocks.append(joined)
    return "\n\n".join(txt_blocks), "\n\n".join(md_blocks)


def image_to_record(path_or_bytes, source_id: str = "manual", kind: str = "ocr", url: str = "",
                    title: str = "", captured_by: str = "manual", meta: dict | None = None) -> Record:
    from PIL import Image

    data = Path(path_or_bytes).read_bytes() if not isinstance(path_or_bytes, (bytes, bytearray)) else bytes(path_or_bytes)
    img = preprocess(Image.open(io.BytesIO(data)))
    lines = _lines_from_tesseract(img)
    text, md = lines_to_markdown(lines)
    confs = [l["conf"] for l in lines]
    return Record(
        source_id=source_id, kind=kind, url=url, channel="ocr",
        title=title or (text.split("\n")[0][:120] if text else ""),
        text=text, markdown=md,
        meta={
            **(meta or {}),
            "engine": "tesseract",
            "captured_by": captured_by,
            "sha256": _sha256(data),
            "mean_confidence": round(statistics.mean(confs), 1) if confs else 0.0,
            "needs_review": [l["text"] for l in lines if l["conf"] < LOW_CONF][:50],
            "file": str(path_or_bytes) if not isinstance(path_or_bytes, (bytes, bytearray)) else None,
        },
    )


OCR_MAX_PAGES = 60   # scanned pages OCR'd per document during a crawl (a 300-page scan would hold a worker for many minutes)


def pdf_to_record(path_or_bytes, source_id: str = "manual", kind: str = "document", url: str = "",
                  title: str = "", meta: dict | None = None, ocr_min_chars: int = 40,
                  ocr_max_pages: int | None = None) -> Record:
    """Text layer first (exact, fast); OCR only the pages that are scans, at most `ocr_max_pages` of them
    (OCR_MAX_PAGES by default; meta.ocr_truncated marks a document whose later scanned pages were left unread)."""
    ocr_max_pages = OCR_MAX_PAGES if ocr_max_pages is None else ocr_max_pages
    truncated = False
    import pdfplumber

    data = Path(path_or_bytes).read_bytes() if not isinstance(path_or_bytes, (bytes, bytearray)) else bytes(path_or_bytes)
    pages_md, pages_txt, ocr_pages, confs = [], [], [], []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            txt = page.extract_text() or ""
            if len(txt.strip()) >= ocr_min_chars:
                lines = [{"block": 0, "par": n, "text": l.strip(), "conf": 100.0, "top": 0}
                         for n, l in enumerate(_reflow(txt))]
            elif len(ocr_pages) >= ocr_max_pages:
                truncated = True
                lines = [{"block": 0, "par": 0, "text": txt.strip(), "conf": 0.0, "top": 0}] if txt.strip() else []
            else:
                img = page.to_image(resolution=300).original
                lines = _lines_from_tesseract(preprocess(img))
                ocr_pages.append(i)
                confs += [l["conf"] for l in lines]
            t, m = lines_to_markdown(lines)
            pages_txt.append(t)
            pages_md.append(m)
        n_pages = len(pdf.pages)
    text = "\n\n".join(pages_txt)
    return Record(
        source_id=source_id, kind=kind, url=url, channel="ocr" if ocr_pages else "pdf",
        title=title or (text.split("\n")[0][:120] if text else ""),
        text=text, markdown="\n\n---\n\n".join(pages_md),
        meta={**(meta or {}), "sha256": _sha256(data), "pages": n_pages, "ocr_pages": ocr_pages,
              "mean_confidence": round(statistics.mean(confs), 1) if confs else None,
              **({"ocr_truncated": True} if truncated else {})},
    )


def _reflow(txt: str) -> list[str]:
    """PDF text layers break every visual line; rebuild paragraphs on blank lines / headings."""
    paras, cur = [], []
    for raw in txt.splitlines():
        line = raw.strip()
        if not line:
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        if ORDINANCE_HEADING.match(line) and cur:
            paras.append(" ".join(cur))
            cur = []
        if cur and cur[-1].endswith("-") and line[:1].islower():
            cur[-1] = cur[-1][:-1] + line
        else:
            cur.append(line)
        if ORDINANCE_HEADING.match(line) and len(line) < 160:
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return paras


def capture(url: str, fetcher, source_id: str = "capture", wait_ms: int = 2500) -> Record:
    """Render a page in a headless browser and OCR a full-page screenshot.

    Only for pages robots.txt allows whose text isn't in the DOM (image notices,
    canvas viewers). Refuses disallowed URLs.
    """
    from .fetch import Disallowed

    if not fetcher.allowed(url):
        raise Disallowed(url)
    fetcher.limiter.wait(url, fetcher.robots.crawl_delay(url))
    from playwright.sync_api import sync_playwright

    from .fetch import launch_browser

    with sync_playwright() as p:
        browser = launch_browser(p)
        page = browser.new_page(user_agent=fetcher.user_agent, viewport={"width": 1280, "height": 2000})
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(wait_ms)
        png = page.screenshot(full_page=True)
        browser.close()
    rec = image_to_record(png, source_id=source_id, url=url, captured_by="browser")
    return rec
