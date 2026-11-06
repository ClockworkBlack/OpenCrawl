"""Benchmarks (offline, local server, no politeness delay):

1. first crawl vs re-crawl of a synthetic city site (listing pages, deep pages, PDFs)
2. database write throughput
3. finding a candidate's name in every stored record: scan (the v0.2 JSONL way) vs full-text search

usage: python scripts/bench.py [--pages 300] [--pdfs 150] [--records 100000]
"""
from __future__ import annotations

import argparse
import http.server
import io
import json
import random
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from opencrawl import channels  # noqa: E402
from opencrawl.fetch import Fetcher  # noqa: E402
from opencrawl.names import name_regex, name_variants  # noqa: E402
from opencrawl.store import Record, Store  # noqa: E402

WORDS = ("council budget zoning ordinance water sewer park road police fire tax millage permit variance hearing "
         "resolution annexation contract library mayor clerk motion second approved tabled public comment").split()


def make_pdf() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (60, 60), "white").save(buf, "PDF")
    return buf.getvalue()


def site_handler(pages: int, pdfs: int):
    pdf = make_pdf()
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            p = self.path
            if p == "/robots.txt":
                body, ct = b"User-agent: *\nAllow: /\n", "text/plain"
            elif p == "/":
                links = "".join(f"<a href='/council/{i}'>Council page {i}</a> " for i in range(0, pages, 10))
                body, ct = f"<html><body><main><p>{' '.join(WORDS * 8)}</p>{links}</main></body></html>".encode(), "text/html"
            elif p.startswith("/council/"):
                i = int(p.rsplit("/", 1)[1])
                kids = "".join(f"<a href='/council/{j}'>p{j}</a> " for j in range(i + 1, min(i + 10, pages)))
                docs = "".join(f"<a href='/files/minutes-{k}.pdf'>minutes {k}</a> " for k in range(i % pdfs, min(i % pdfs + 3, pdfs)))
                body = f"<html><body><main><h2>Meeting {i}</h2><p>{' '.join(random.choices(WORDS, k=120))}</p>{kids}{docs}</main></body></html>"
                body, ct = body.encode(), "text/html"
            elif p.startswith("/files/"):
                body, ct = pdf, "application/pdf"
            else:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return H, hits


def bench_crawl(pages: int, pdfs: int) -> dict:
    handler, hits = site_handler(pages, pdfs)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    out = {}
    with tempfile.TemporaryDirectory() as d:
        s = requests.Session()
        s.trust_env = False
        f = Fetcher("Bench/1", Path(d) / "state", min_interval=0, session=s, sleep=lambda _: None)
        store = Store(Path(d) / "data")
        src = {"id": "bench:site", "url": base, "kind": "page", "depth": 40}
        for label in ("first crawl", "re-crawl"):
            hits.clear()
            ctx = channels.CrawlContext(frontier=store, page_revisit_days=7)
            t0 = time.perf_counter()
            n = sum(store.add(r) for r in channels.crawl(f, src, {"channel": "html", "endpoint": base},
                                                          {"max_pages": pages + pdfs + 10}, ctx=ctx))
            store.flush()
            dt_ = time.perf_counter() - t0
            req = len([h for h in hits if h != "/robots.txt"])
            out[label] = {"seconds": round(dt_, 2), "requests": req, "pdf_requests": sum(h.endswith(".pdf") for h in hits),
                          "records_written": n, "skipped_as_unchanged": ctx.stats["skipped_fresh"]}
        store.close()
    srv.shutdown()
    return out


def bench_store_and_search(n: int) -> dict:
    random.seed(7)
    first = "James Mary John Patricia Robert Jennifer Michael Linda David Elizabeth William Barbara Kenji Ana".split()
    last = "Smith Johnson Williams Brown Jones Garcia Miller Davis Local Example Okafor Nguyen Patel Kim".split()
    names = [f"{a} {b}" for a in first for b in last]
    out = {}
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "data")
        t0 = time.perf_counter()
        rows = []
        for i in range(n):
            who = random.choice(names) if i % 7 == 0 else ""
            text = " ".join(random.choices(WORDS, k=60)) + (f" Council member {who} moved to approve." if who else "")
            rec = Record(f"j{i % 700}:site", "agenda_item", f"http://j{i % 700}.gov/item/{i}", title=f"Item {i}", text=text,
                         target=f"j{i % 700}", meta={"EventItemId": i})
            store.add(rec)
            rows.append(rec.to_dict())
        store.flush()
        w = time.perf_counter() - t0
        out["write"] = {"records": n, "seconds": round(w, 2), "records_per_second": int(n / w)}
        probe = random.sample(names, 50)
        # v0.2 approach: every record, every name, regex
        t0 = time.perf_counter()
        scan_hits = 0
        for nm in probe:
            rx = name_regex(name_variants(nm))
            scan_hits += sum(1 for r in rows if rx.search(r["title"] + "\n" + r["text"]))
        scan = time.perf_counter() - t0
        t0 = time.perf_counter()
        fts_hits = 0
        for nm in probe:
            fts_hits += len(store.search(Store.fts_phrase(*name_variants(nm)), limit=100000))
        fts = time.perf_counter() - t0
        out["name_search_50_candidates"] = {"scan_seconds": round(scan, 2), "fts_seconds": round(fts, 3),
                                            "speedup": round(scan / fts, 1), "scan_hits": scan_hits, "fts_hits": fts_hits}
        store.close()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=300)
    ap.add_argument("--pdfs", type=int, default=150)
    ap.add_argument("--records", type=int, default=100000)
    a = ap.parse_args()
    res = {"crawl": bench_crawl(a.pages, a.pdfs), **bench_store_and_search(a.records)}
    print(json.dumps(res, indent=1))
