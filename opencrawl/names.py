"""Name variants for matching subjects (people) in text.

"Robert \"Bob\" Example Jr."   -> Robert Example, Bob Example
"Robin A. Ashby"               -> Robin A Ashby, Robin Ashby
"DOE, JANE"                    -> Jane Doe
"TAYLOR CALDER (NORTH COUNTY)" -> Taylor Calder
Every variant keeps at least a first and last name, so a bare surname never matches.
"""
from __future__ import annotations

import re
import unicodedata

QUOTED = re.compile(r"[\"“”']([^\"“”']{2,20})[\"“”']")
SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v|esq|md|phd)\.?$", re.I)
TITLES = re.compile(r"^(dr|mr|mrs|ms|rev|hon|judge|sheriff|commissioner|councilmember|councilman|councilwoman|mayor)\.?\s+",
                    re.I)


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _clean(n: str) -> str:
    n = re.sub(r"\([^)]*\)", " ", n)
    n = n.replace(",", " , ")
    n = re.sub(r"\s+", " ", n).strip(" ,")
    return n


def _words(n: str) -> list[str]:
    n = TITLES.sub("", n)
    w = [x for x in re.split(r"\s+", n) if x and x != ","]
    while w and SUFFIX.fullmatch(w[-1].lower().rstrip(".")):
        w.pop()
    return w


def name_variants(display: str, aka: list[str] | tuple = ()) -> list[str]:
    out: list[str] = []
    for raw in [display, *aka]:
        if not raw:
            continue
        raw = _ascii(raw)
        nicks = QUOTED.findall(raw)
        base = _clean(QUOTED.sub(" ", raw))
        if base.count(",") == 1 and len(base.split(",")[0].split()) <= 2:  # "DOE, JANE A"
            last, first = (x.strip() for x in base.split(","))
            base = f"{first} {last}"
        w = _words(base.replace(",", " "))
        if len(w) < 2:
            continue
        cand = [" ".join(w), f"{w[0]} {w[-1]}"]
        cand += [f"{nk.strip()} {w[-1]}" for nk in nicks]
        if len(w) > 2 and len(w[1].rstrip(".")) == 1:  # "Pat A Local" also as written with the period
            cand.append(f"{w[0]} {w[1].rstrip('.')}. {' '.join(w[2:])}")
        for c in cand:
            c = re.sub(r"\s+", " ", c).strip()
            if len(c.split()) >= 2 and len(c) >= 5:
                out.append(c.title() if c.isupper() else c)
    return list(dict.fromkeys(out))


def name_regex(variants: list[str]) -> re.Pattern:
    """Word-boundary, case-insensitive, punctuation-tolerant pattern for any variant."""
    parts = []
    for v in variants:
        toks = re.findall(r"[A-Za-z0-9]+", v)
        if len(toks) >= 2:
            parts.append(r"\b" + r"[\s.\-']+".join(re.escape(t) for t in toks) + r"\b")
    return re.compile("|".join(parts) or r"(?!x)x", re.I)


def excerpts(text: str, pattern: re.Pattern, n: int = 3, width: int = 220) -> list[str]:
    """Up to n passages of text around matches (no overlapping windows), for citing what was said."""
    out, last_end = [], -1
    for m in pattern.finditer(text or ""):
        if m.start() < last_end:
            continue
        a, b = max(0, m.start() - width), min(len(text), m.end() + width)
        a = text.rfind(" ", 0, a) + 1 if a > 0 else 0
        b = text.find(" ", b) if text.find(" ", b) != -1 else len(text)
        out.append(("… " if a > 0 else "") + re.sub(r"\s+", " ", text[a:b]).strip() + (" …" if b < len(text) else ""))
        last_end = b
        if len(out) >= n:
            break
    return out


def person_key(name: str) -> str:
    """A stable upper-case key for a person's name: no parentheticals or quotes, letters only, without
    JR/SR/MR/MRS/MS/DR/HON and single-letter tokens. "Henry \"Hank\" Example Jr." -> HENRY HANK EXAMPLE."""
    n = re.sub(r"\([^)]*\)", " ", name or "")
    n = re.sub(r"[“”\"]", " ", n)
    n = re.sub(r"[^A-Za-z' -]", " ", n).upper().replace("'", "").replace("-", " ")
    w = [x for x in n.split() if x not in ("JR", "SR", "MR", "MRS", "MS", "DR", "HON")]
    return " ".join(x for x in w if len(x) > 1)
