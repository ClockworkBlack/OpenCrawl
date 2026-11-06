# The civic module

Government targets get extra treatment: a registry of jurisdictions, adapters for the systems local governments
publish through, derivers that turn minutes and results pages into votes and contests, public records requests, and
campaign-finance research. Everything switches on by configuration; nothing here runs for a project that has no
`jurisdictions.yaml`.

## The registry

`config/jurisdictions.yaml` lists counties and municipalities; `config/school_districts.yaml` lists districts:

```yaml
counties:
  - {id: co-adams, name: Adams County, kind: county, fips: "08001", seat: Brighton, population: 554668,
     wikipedia: https://en.wikipedia.org/wiki/Adams_County,_Colorado}
municipalities:
  - {id: aurora, name: Aurora, kind: city, counties: [Arapahoe, Adams, Douglas], population: 386261,
     wikipedia: https://en.wikipedia.org/wiki/Aurora,_Colorado}
```

Kinds: `county`, `city`, `town`, `consolidated` (a city-county), `village`, `borough`, `township`, `school_district`.
Crawl tiers come from population (a: counties and ≥10k; b: 1k–10k; c: under 1k; school boards b/c). FIPS codes are
optional for municipalities (a one-query Wikidata lookup uses them when present); Wikipedia links are the fallback.

`opencrawl/examples/colorado-civic/scripts/build_registry.py` builds both files from three small pipe-separated text files
(counties, municipalities, school districts) that any state's Wikipedia lists or Census files provide:

```bash
python scripts/build_registry.py --state Colorado --fips 08 --abbr CO
```

Set the state in `settings.yaml`:

```yaml
civic: {state_fips: "08", state_name: Colorado, state_abbr: CO}
```

## What each jurisdiction expands into

| source | system | how it is read |
|---|---|---|
| `<id>:legistar` | Legistar Web API | matters (ordinances, resolutions), meetings, agenda items, roll-call votes (one paged call per office holder), office records, sponsor lists; yearly backfill windows with checkpoints; deltas by `LastModifiedUtc` |
| `<id>:civicclerk` | CivicClerk OData | meetings and their published files (agendas, minutes, packets) |
| `<id>:boarddocs` | BoardDocs | school-board meetings, agenda items with permalinks, opened attachments |
| `<id>:agendas` | CivicPlus AgendaCenter | agendas and minutes through its RSS feeds and document center |
| `<id>:granicus` | Granicus | agendas, minutes and video pages through the publisher RSS |
| `<id>:elections` | the elections office site | results pages and PDFs, qualifying lists, sample ballots |
| `<id>:code` | eCode360 / American Legal | ordinances, through the headless browser (both render client-side) |
| `<id>:youtube` | the official YouTube channel | meeting videos and captions (needs `YOUTUBE_API_KEY`) |
| `<id>:site` | the official website | pages and PDFs matching the civic include list (council, agenda, minute, ordinance, election, news...) |

Municode links are recorded by seed but never crawled: its library blocks crawlers, so those codes go through
`opencrawl tasks` (capture by hand) or a public records request.

## Derivers

| deriver | reads | writes |
|---|---|---|
| elections | results pages and PDFs, qualifying lists | `election_result` (one per contest: date, office, candidates with votes, percentages, outcomes), `qualifying` |
| minutes | council minutes (text or PDF) | `agenda_item` (one per motion) and `vote` with movers, seconders, roll calls, tallies; each vote carries its basis |
| ballot | referendum notices, sample ballots, resolutions calling an election; Yes/No contests on results pages | `ballot_question` (text, type, amount, years, purposes, results) |
| press | press releases and news-flash items | `appointment` (who, to what, by whom, term) |
| guides | voter guides, scorecards, endorsement pages (`config/guides.yaml`) | `endorsement`, `rating`, `questionnaire` records, and `connection` records on matched subjects |

Ballot-question types cover bonds, sales/use/lodging/excise taxes, homestead exemptions, property taxes and levies,
debt and revenue-limit questions, alcohol, charter amendments, annexation and incorporation, government structure,
recalls and constitutional amendments. Add your state's own kinds, tried first, in `settings.yaml civic.ballot_types`
(`{label, pattern}`; the Colorado example adds TABOR revenue-retention questions and metro districts).

## Official YouTube channels

A channel becomes a source only when the government points to it — a link on its official website found by seed, or
a hand check in `config/overrides.yaml` as `youtube_checked` — and the channel is plainly the government's (it names
the place or reads as a government channel; nothing marking a chamber, news outlet, station, party or residents'
group). Videos from any other channel go to the video quarantine (`data/quarantine/videos.jsonl`), never into the
records; to promote one, check it by hand and add it to overrides.

## Public records requests

See the README's **Open records requests** section. `settings.yaml openrecords:` names the law, its citation, the
response deadline in business days and the cost cap the letter asks about; `opencrawl records plan --show` prints the
letter that would go out. The crawler asks only jurisdictions whose code it could not crawl, only at addresses or
portals printed on their official sites, at most `daily_limit` a day, never twice within `cooldown_days`. Replies
count only from the domain the request went to or `openrecords.trusted_reply_domains`; others are kept for review.

## Campaign finance

`config/finance.yaml` names the state system (`state_commission`: TRACER in Colorado, for instance). The crawler never
guesses its search URL: `opencrawl finance probe` lists the links its front page offers; once a person confirms the
search page and its query format, `search_url` with `{last}` / `{first}` / `{name}` placeholders and `confirmed: true`
switch it on. CSV exports from the system go in `imports/finance/<subject-id>.csv`. Federal candidates come from the
FEC API; organizations are resolved across filings and described from FEC committee details, IRS 990 data
(ProPublica), OpenCorporates (with a key), their own about pages and `config/organizations.yaml`.
