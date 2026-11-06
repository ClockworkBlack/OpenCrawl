"""Turn HTML into clean, ingestion-ready Markdown, and find feeds/APIs/links in a page."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

NOISE = ["script", "style", "noscript", "nav", "footer", "header", "form", "iframe", "svg", "button", "aside"]
ORDINANCE_HEADING = re.compile(
    r"^\s*(?:(?:Sec(?:tion)?\.?|§)\s*\d[\w\-.]*|ARTICLE\s+[IVXLC\d]+|CHAPTER\s+\d+[A-Z]?|DIVISION\s+\d+|ORDINANCE\s+(?:NO\.?|NUMBER)\s*[\w\-]+)",
    re.I,
)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def main_node(soup: BeautifulSoup) -> Tag:
    for sel in ["main", "article", "[role=main]", "#content", "#main", ".content", "#maincontent"]:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) > 200:
            return node
    # largest text block, but only when it holds most of the page's prose (a small page is read whole)
    body = soup.body or soup
    total = len(body.get_text(" ", strip=True))
    best, best_len = body, 0
    for div in soup.find_all(["main", "article", "div", "section"]):
        n = len(div.get_text(" ", strip=True))
        links = sum(len(a.get_text(strip=True)) for a in div.find_all("a"))
        score = n - 2 * links
        if score > best_len:
            best, best_len = div, score
    if best is not body and len(best.get_text(" ", strip=True)) < 0.6 * total:
        return body
    return best


def _table_md(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)).replace("|", "\\|") for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _inline(node) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name == "br":
        return "\n"
    inner = "".join(_inline(c) for c in node.children)
    if node.name in ("strong", "b") and inner.strip():
        return f"**{inner.strip()}**"
    if node.name in ("em", "i") and inner.strip():
        return f"*{inner.strip()}*"
    if node.name == "a" and node.get("href") and inner.strip():
        return f"[{inner.strip()}]({node['href']})"
    return inner


def html_to_markdown(html: str, base_url: str = "") -> tuple[str, str]:
    """Returns (title, markdown)."""
    soup = soup_of(html)
    title = (soup.title.get_text(strip=True) if soup.title else "") or ""
    for tag in soup(NOISE):
        tag.decompose()
    if base_url:
        for a in soup.find_all("a", href=True):
            a["href"] = urljoin(base_url, a["href"])
    root = main_node(soup)
    blocks: list[str] = []

    def walk(node: Tag):
        for child in node.children:
            if isinstance(child, NavigableString):
                t = str(child).strip()
                if t:
                    blocks.append(t)
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                txt = child.get_text(" ", strip=True)
                if txt:
                    blocks.append("#" * int(name[1]) + " " + txt)
            elif name == "table":
                md = _table_md(child)
                if md:
                    blocks.append(md)
            elif name in ("ul", "ol"):
                items = []
                for i, li in enumerate(child.find_all("li", recursive=False), 1):
                    txt = re.sub(r"\s+", " ", _inline(li)).strip()
                    if txt:
                        items.append(f"{i}. {txt}" if name == "ol" else f"- {txt}")
                if items:
                    blocks.append("\n".join(items))
            elif name in ("p", "blockquote", "pre"):
                txt = re.sub(r"[ \t]+", " ", _inline(child)).strip()
                if txt:
                    if ORDINANCE_HEADING.match(txt) and len(txt) < 160:
                        blocks.append("### " + txt)
                    else:
                        blocks.append(("> " + txt) if name == "blockquote" else txt)
            else:
                walk(child)

    walk(root)
    md = "\n\n".join(b for b in blocks if b)
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    return title, md


DATE_META = ("article:published_time", "og:published_time", "datePublished", "date", "dc.date", "dc.date.issued",
             "pubdate", "publish-date", "publication_date", "sailthru.date", "parsely-pub-date", "citation_date")
TEXT_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|"
                       r"Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+(20\d\d|19\d\d)\b|\b(\d{1,2})/(\d{1,2})/(20\d\d)\b|"
                       r"\b(20\d\d)-(\d\d)-(\d\d)\b")
MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


def page_date(html: str, text: str = "") -> str | None:
    """When a page says it was published: meta tags, <time datetime>, JSON-LD, else the first date in its opening text."""
    soup = soup_of(html[:300000])
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").lower()
        if key in {k.lower() for k in DATE_META}:
            v = (m.get("content") or "").strip()
            d = _iso10(v)
            if d:
                return d
    t = soup.find("time", attrs={"datetime": True})
    if t:
        d = _iso10(t["datetime"])
        if d:
            return d
    for sc in soup.find_all("script", attrs={"type": "application/ld+json"}):
        mm = re.search(r'"datePublished"\s*:\s*"([^"]+)"', sc.get_text() or "")
        if mm:
            d = _iso10(mm.group(1))
            if d:
                return d
    head = (text or soup.get_text(" ", strip=True))[:1500]
    mm = TEXT_DATE.search(head)
    if mm:
        return _iso10(mm.group(0))
    return None


def _iso10(v: str) -> str | None:
    v = (v or "").strip()
    m = re.match(r"(20\d\d|19\d\d)-(\d\d)-(\d\d)", v)
    if m:
        return m.group(0)
    m = TEXT_DATE.search(v)
    if not m:
        return None
    if m.group(1):
        mo = MONTHS.get(m.group(1).lower()[:3])
        return f"{m.group(3)}-{mo:02d}-{int(m.group(2)):02d}" if mo else None
    if m.group(4):
        return f"{m.group(6)}-{int(m.group(4)):02d}-{int(m.group(5)):02d}"
    return f"{m.group(7)}-{m.group(8)}-{m.group(9)}"


def main_text(html: str) -> str:
    """Plain text of a page's main content (markdown stripped of link targets and emphasis)."""
    _, md = html_to_markdown(html)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)
    t = re.sub(r"[*#>`]+", "", t)
    return re.sub(r"[ \t]+", " ", t).strip()


FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml", "application/feed+json")


def find_feed_links(html: str, base_url: str) -> list[str]:
    soup = soup_of(html)
    out = []
    for link in soup.find_all("link", rel=True, href=True):
        rel = " ".join(link["rel"]) if isinstance(link["rel"], list) else link["rel"]
        if "alternate" in rel and link.get("type", "").lower() in FEED_TYPES:
            out.append(urljoin(base_url, link["href"]))
    for a in soup.find_all("a", href=True):
        h = a["href"].lower()
        if any(k in h for k in ("rss.aspx", "rssfeed", "/rss", "/feed", ".rss", "atom.xml")):
            out.append(urljoin(base_url, a["href"]))
    return list(dict.fromkeys(out))


def find_api_hints(html: str, base_url: str) -> dict:
    soup = soup_of(html)
    hints = {}
    for link in soup.find_all("link", rel=True, href=True):
        rel = " ".join(link["rel"]) if isinstance(link["rel"], list) else link["rel"]
        if "https://api.w.org/" in rel:
            hints["wp_json"] = urljoin(base_url, link["href"])
    m = re.search(r"https?://([a-z0-9\-]+)\.legistar\.com", html, re.I)
    if m:
        hints["legistar_client"] = m.group(1).lower()
    return hints


def links(html: str, base_url: str) -> list[str]:
    soup = soup_of(html)
    return list(dict.fromkeys(urljoin(base_url, a["href"]).split("#")[0] for a in soup.find_all("a", href=True)))
