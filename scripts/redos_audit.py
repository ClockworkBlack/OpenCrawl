#!/usr/bin/env python3
"""Check every module-level regular expression in the package against inputs built to trigger catastrophic
backtracking (ReDoS). Web pages, feeds, emails and OCR output are untrusted text, and every pattern here runs on
some of it. Each pattern gets a few seconds per input in a child process; a slow one is reported and the script
exits non-zero.

    python scripts/redos_audit.py            # all patterns
    python scripts/redos_audit.py --seconds 1
"""
from __future__ import annotations

import argparse
import importlib
import multiprocessing as mp
import pkgutil
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

N = 30_000
INPUTS = [
    "a" * N, "a" * N + "!", " " * N + "x", "\t \n" * (N // 3), "-" * N, "." * N, "1" * N + "x", "1." * (N // 2),
    "a " * (N // 2), "a." * (N // 2) + "@", "@" * N, "/" * N, "<" * N, "<a " * (N // 3), "\"" * N, "'" * N,
    "x@" + "a." * (N // 2), "http://" + "a" * N, "http://" + "a." * (N // 2), "Sec. " + "1-" * (N // 2),
    "#" * N, "$" * N + "1", "%" * N, "(" * N, ")" * N, "ab" * (N // 2), "A" * N + "a", "é" * N,
    "Ordinance No. " + "1" * N, "Mr. " * (N // 4), "Jan " * (N // 4), "12/" * (N // 3), "aA1_-" * (N // 5),
]


def _try(rx_pattern: str, flags: int, text: str, q) -> None:
    rx = re.compile(rx_pattern, flags)
    t0 = time.monotonic()
    rx.search(text)
    list(rx.finditer(text[:5000]))
    q.put(time.monotonic() - t0)


def patterns():
    import opencrawl

    seen = set()
    for info in pkgutil.walk_packages(opencrawl.__path__, "opencrawl."):
        if info.name.endswith("__main__"):
            continue
        try:
            mod = importlib.import_module(info.name)
        except Exception as exc:  # noqa: BLE001
            print(f"skip {info.name}: {exc}")
            continue
        for name, val in vars(mod).items():
            vals = val if isinstance(val, (list, tuple)) else [val]
            for v in vals:
                if isinstance(v, tuple) and len(v) == 2 and isinstance(v[1], list):
                    vals2 = [re.compile(x, re.I) for x in v[1] if isinstance(x, str)]
                else:
                    vals2 = [v]
                for p in vals2:
                    if isinstance(p, re.Pattern) and (p.pattern, p.flags) not in seen:
                        seen.add((p.pattern, p.flags))
                        yield f"{info.name}.{name}", p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args()
    ctx = mp.get_context("fork")
    slow, n = [], 0
    for where, p in patterns():
        n += 1
        for text in INPUTS:
            q = ctx.Queue()
            proc = ctx.Process(target=_try, args=(p.pattern, p.flags, text, q))
            proc.start()
            proc.join(a.seconds)
            if proc.is_alive():
                proc.kill()
                proc.join()
                slow.append((where, p.pattern[:100], text[:20]))
                break
    print(f"{n} patterns checked against {len(INPUTS)} inputs")
    for where, pat, sample in slow:
        print(f"SLOW  {where}: {pat!r}  (input starting {sample!r})")
    return 1 if slow else 0


if __name__ == "__main__":
    sys.exit(main())
