# Colorado civic example

A complete civic project for one US state: every county (64), municipality (272) and public school district (178)
in Colorado, the state's official newsrooms, a starting list of voter-guide organizations and news outlets, the
Colorado Open Records Act in the public-records settings, and the Secretary of State's TRACER system in the
campaign-finance settings.

```bash
opencrawl init my-colorado --example colorado-civic
cd my-colorado
# set http.user_agent in config/settings.yaml to your repository, then check the project
opencrawl doctor
opencrawl seed --max-minutes 30        # websites (Wikidata by FIPS / article, Wikipedia), portals, feeds, records contacts
opencrawl targets --sources            # what will be crawled
opencrawl run --tier a --max-minutes 30
opencrawl derive --max-minutes 10
opencrawl export
```

## Where the registry comes from

`sources/counties.txt`, `sources/municipalities.txt` and `sources/school_districts.txt` are pipe-separated lists
taken from Wikipedia's "List of counties in Colorado", "List of municipalities in Colorado" and "List of school
districts in Colorado" (text under CC BY-SA 4.0; populations are the 2020 census figures those lists carry).
`scripts/build_registry.py --state Colorado --fips 08 --abbr CO` turns them into `config/jurisdictions.yaml` and
`config/school_districts.yaml`. County FIPS codes are included; municipality FIPS codes can be added as a fifth
column from the Census Gazetteer (optional: seeding falls back to each place's Wikipedia article).

The same three files, in the same shape, build a registry for any other state.

## What is Colorado-specific here

| file | Colorado specifics |
|---|---|
| `config/settings.yaml` | `civic.state_fips: "08"`, Mountain-time off-peak windows, CORA (C.R.S. § 24-72-201 et seq., three working days), `.co.us` as a primary-evidence suffix, "Colorado" as a required place word for news, TABOR revenue-retention and metro-district ballot questions (`civic.ballot_types`) |
| `config/press.yaml` | Governor's office, Secretary of State, Attorney General, General Assembly, Department of Education, Public Utilities Commission |
| `config/guides.yaml` | League of Women Voters of Colorado, Vote411, Conservation Colorado, Sierra Club Colorado, CEA, Colorado REALTORS, the two state parties — starting points; check each site |
| `config/outlets.yaml` | Denver and regional outlets |
| `config/topics.yaml`, `config/issues.yaml` | local-government topics (TABOR, mill levies, metro districts, water rights...) and statewide issues |
| `config/finance.yaml` | `state: CO`, TRACER as the state system |
| `config/sources.yaml` | the Secretary of State's elections pages and historical results site |

Nothing in the crawler itself knows about Colorado; swap these files for another state's.
