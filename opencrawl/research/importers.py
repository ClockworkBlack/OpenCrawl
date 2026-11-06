"""`opencrawl subjects import FILE`: bring a list of subjects in from a CSV or JSON file.

The file can come from anywhere: a spreadsheet of candidates, a membership list, an export from your own site.
It is read once into state/subjects_imported.json, which `load_subjects` merges under config/subjects.yaml
(hand entries win; list fields are unioned). Re-importing replaces the imported set.

CSV columns (header names are matched case-insensitively; everything but `name` is optional)
  id, name, aliases, kind, target, targets, places, websites, social, search_terms, news_feeds, office, party,
  public_role, title, priority, external_id, last_active, fec_id, finance_csv
  List columns hold several values separated by ";" or "|".
JSON: a list of objects with the same fields, or {"subjects": [...]}.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

LIST_FIELDS = ("aliases", "targets", "places", "websites", "social", "search_terms", "news_feeds", "counties")
STATE_FILE = "subjects_imported.json"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _split(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[;|]", str(v)) if x.strip()]


def normalize(row: dict) -> dict | None:
    r = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    name = (r.get("name") or "").strip()
    if not name:
        return None
    out: dict = {"id": _slug(r.get("id") or name), "name": name}
    for k in LIST_FIELDS:
        vals = _split(r.get(k))
        if vals:
            out[k] = vals
    for k in ("kind", "target", "office", "party", "public_role", "title", "external_id", "last_active", "fec_id",
              "finance_csv", "finance_source", "search_hint"):
        if r.get(k) not in (None, ""):
            out[k] = str(r[k]).strip()
    if str(r.get("priority", "")).strip().lower() in ("1", "true", "yes", "y"):
        out["priority"] = True
    if out.get("target") and "targets" not in out:
        out["targets"] = [out["target"]]
    return out


def read_file(path: Path) -> list[dict]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("subjects") if isinstance(data, dict) else data
    else:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    out = []
    for row in rows or []:
        n = normalize(row)
        if n:
            out.append(n)
    return out


def import_subjects(path: Path, root: Path, log=print) -> dict:
    subjects = read_file(path)
    by_id: dict[str, dict] = {}
    for s in subjects:
        if s["id"] in by_id:  # same id twice: union the lists, keep the first scalar values
            base = by_id[s["id"]]
            for k in LIST_FIELDS:
                if s.get(k):
                    base[k] = list(dict.fromkeys((base.get(k) or []) + s[k]))
            for k, v in s.items():
                base.setdefault(k, v)
        else:
            by_id[s["id"]] = s
    state = Path(root) / "state"
    state.mkdir(parents=True, exist_ok=True)
    meta = {"from": Path(path).name, "subjects": len(by_id)}       # the file's name only, not where it lives
    (state / STATE_FILE).write_text(json.dumps({"meta": meta, "subjects": list(by_id.values())}, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    log(f"imported {len(by_id)} subjects from {path} -> state/{STATE_FILE}")
    return meta


def imported_subjects(root: Path) -> list[dict]:
    p = Path(root) / "state" / STATE_FILE
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text(encoding="utf-8")).get("subjects") or [])
    except Exception:  # noqa: BLE001
        return []
