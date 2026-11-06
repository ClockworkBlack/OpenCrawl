"""Build config/jurisdictions.yaml and config/school_districts.yaml for a US state from three small text files.

Inputs (sources/, pipe-separated, "#" comments):
  counties.txt           name|county FIPS (3 digits)|county seat|population
  municipalities.txt     name (append † for a county seat)|type|county or counties (";")|population
  school_districts.txt   district name|county or counties (";")

Any state works: the Wikipedia "List of counties in <State>", "List of municipalities in <State>" and
"List of school districts in <State>" pages carry exactly these columns, as do the Census Bureau's county and
place files. Municipality FIPS codes are optional: add a fifth column (the 7-digit place code) when you have
them from the Census Gazetteer and the Wikidata lookup in `opencrawl seed` will use them; otherwise seeding
falls back to each jurisdiction's Wikipedia article, which the script links here by the usual
"<Name>, <State>" title.

    python scripts/build_registry.py --state Colorado --fips 08 --abbr CO
"""
from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent.parent


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s.replace("'", "")).strip("-")


def rows(path: Path) -> list[list[str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            out.append([c.strip() for c in line.split("|")])
    return out


def kind_of(mtype: str) -> str:
    t = mtype.lower()
    if "consolidated" in t or "city and county" in t or "city-county" in t:
        return "consolidated"
    if "town" in t or "village" in t or "borough" in t:
        return "town"
    return "city"


def wiki(title: str, state: str) -> str:
    return "https://en.wikipedia.org/wiki/" + f"{title}, {state}".replace(" ", "_")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="state name, e.g. Colorado")
    ap.add_argument("--fips", required=True, help="two-digit state FIPS code, e.g. 08")
    ap.add_argument("--abbr", required=True, help="postal abbreviation, e.g. CO")
    ap.add_argument("--sources", default=str(HERE / "sources"))
    ap.add_argument("--out", default=str(HERE / "config"))
    a = ap.parse_args()
    src, out = Path(a.sources), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    counties, county_ids, county_pop = [], {}, {}
    for name, cfips, seat, pop in rows(src / "counties.txt"):
        short = re.sub(r"^City and County of ", "", name).removesuffix(" County")
        cid = f"co-{slug(short)}"
        county_ids[short.lower()] = cid
        county_pop[short.lower()] = int(pop)
        counties.append({"id": cid, "name": name if name.endswith("County") else f"{short} County", "kind": "county",
                         "fips": f"{a.fips}{cfips}", "seat": seat, "population": int(pop),
                         "wikipedia": wiki(name if name.endswith("County") else short, a.state) if not name.startswith("City and County")
                         else wiki(short, a.state)})

    munis, seen = [], {}
    for r in rows(src / "municipalities.txt"):
        name, mtype, cos, pop = r[:4]
        seat = name.endswith("†")
        name = name.rstrip("†").strip()
        mid = slug(name)
        if mid in seen:  # same name twice: disambiguate by county
            mid = f"{mid}-{slug(cos.split(';')[0])}"
        seen[mid] = True
        m = {"id": mid, "name": name, "kind": kind_of(mtype), "counties": [c.strip() for c in cos.split(";") if c.strip()],
             "population": int(pop), "wikipedia": wiki(name, a.state)}
        if len(r) > 4 and r[4]:
            m["fips"] = r[4]
        if seat:
            m["county_seat"] = True
        if m["kind"] == "consolidated":
            m["consolidated_with"] = f"{m['counties'][0]} County"
        munis.append(m)

    doc = {"_about": f"Every county and municipality of {a.state}: ids, kinds, FIPS (counties), counties, 2020 populations and "
                     f"Wikipedia articles. Built by scripts/build_registry.py from sources/*.txt (Wikipedia lists, CC BY-SA; "
                     f"Census 2020 populations). Websites, portals and feeds are discovered by `opencrawl seed` into "
                     f"state/discovered.json; hand edits go in config/overrides.yaml.",
           "counties": counties, "municipalities": munis}
    (out / "jurisdictions.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")

    districts = []
    for name, cos in rows(src / "school_districts.txt"):
        cs = [c.strip() for c in cos.split(";") if c.strip()]
        first = cs[0] if cs else ""
        d = {"id": f"sd-{slug(name)}", "name": name, "kind": "school_district", "county": first,
             "county_id": county_ids.get(first.lower()), "population": county_pop.get(first.lower()),
             "aliases": [re.sub(r"\s+(RE|R|C|J|JT|RD|RJ)?-?[\w-]*\d[\w-]*$", "", name).strip()]}
        if len(cs) > 1:
            d["counties"] = cs
        if d["aliases"][0] == name:
            d.pop("aliases")
        districts.append(d)
    sd = {"_about": f"Every public school district of {a.state} ({len(districts)}), keyed to the registry's counties. Built by "
                    f"scripts/build_registry.py from sources/school_districts.txt. Websites and board portals (BoardDocs...) come "
                    f"from `opencrawl seed` or config/overrides.yaml.", "districts": districts}
    (out / "school_districts.yaml").write_text(yaml.safe_dump(sd, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    print(f"{len(counties)} counties, {len(munis)} municipalities, {len(districts)} school districts -> {out}")


if __name__ == "__main__":
    main()
