"""Minimal RSS 2.0 / Atom / RDF / JSON Feed parser (no third-party feed library needed)."""
from __future__ import annotations

import json
import re

from lxml import etree


def _txt(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _local(tag) -> str:
    return tag.split("}")[-1].lower() if isinstance(tag, str) else ""


def parse_feed(content: bytes) -> list[dict]:
    head = content.lstrip()[:1]
    if head in (b"{", b"["):
        data = json.loads(content)
        return [
            {"title": i.get("title", ""), "link": i.get("url", ""), "id": i.get("id", i.get("url", "")),
             "published": i.get("date_published"), "summary": i.get("content_text") or i.get("summary", "")}
            for i in data.get("items", [])
        ]
    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    root = etree.fromstring(content, parser=parser)
    if root is None:
        return []
    items = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in ("item", "entry"):
            continue
        rec = {"title": "", "link": "", "id": "", "published": None, "summary": "", "source": "", "source_url": ""}
        for child in el:
            c = _local(child.tag)
            if c == "title":
                rec["title"] = _txt(child)
            elif c == "source":
                rec["source"], rec["source_url"] = _txt(child), child.get("url", "")
            elif c == "link":
                href = child.get("href")
                if href and child.get("rel", "alternate") == "alternate":
                    rec["link"] = href
                elif not href and not rec["link"]:
                    rec["link"] = _txt(child)
            elif c in ("guid", "id"):
                rec["id"] = _txt(child)
            elif c in ("pubdate", "published", "updated", "date") and not rec["published"]:
                rec["published"] = _txt(child)
            elif c in ("description", "summary", "content", "encoded") and not rec["summary"]:
                rec["summary"] = _txt(child)
        rec["id"] = rec["id"] or rec["link"]
        items.append(rec)
    return items


def looks_like_feed(content: bytes, content_type: str = "") -> bool:
    ct = content_type.lower()
    if any(k in ct for k in ("rss", "atom", "feed+json")):
        return True
    head = content.lstrip()[:600].lower()
    return b"<rss" in head or b"<feed" in head or b"<rdf:rdf" in head or b'"version": "https://jsonfeed.org' in head
