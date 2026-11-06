# OpenCrawl

A polite, resumable crawler for **any list of websites, organizations or causes** — with optional modules for
**government records** (meeting portals, ordinances, votes, elections, public records requests) and
**subject research** (everything said by and about a list of people or organizations, graded by evidence).

Give it a list of targets (a city, a company, an agency, a coalition — anything with a website) and it finds where
their records live, picks the cheapest channel for each (API, RSS, sitemap, HTML, headless browser), crawls politely
inside a time budget, keeps everything in one SQLite database with full-text search, and exports a JSON bundle
that a website or a spreadsheet can take as it is. It runs on your laptop or on a nightly GitHub Actions schedule.

## What's in it

| Piece | What it does |
|---|---|
| **Targets** (`config/targets.yaml`) | The things you cover. Each expands into one source per place its records live: its website (`<id>:site`), its YouTube channel (`<id>:youtube`), a meeting portal or other structured system when an adapter knows it. Hand edits in `config/overrides.yaml`. |
| **Sources** (`config/sources.yaml`) | Any website, feed, API or portal on its own, with link filters, page budgets and a pinned channel if you want one. |
| **Seed** (`opencrawl seed`) | Finds each target's website (given, or looked up on Wikidata / Wikipedia by name), then its portals, feeds, API hints and public-records contact. Biggest targets first; time-budgeted; re-checks entries older than N days. |
| **Database** (`data/crawl.db`) | SQLite (WAL) with full-text search. Upserts by natural key: a changed page updates its row (version+1) instead of piling up copies. Holds the URL frontier and resume cursors. |
| **Channel pick** | Per source: probe the official API, WordPress API, feeds, sitemap, HTML, browser; use the fastest working structured one. Probing fingerprints the platform first, so it costs 2–4 requests instead of a dozen blind guesses. A target's own website also gets a coverage crawl so a news feed never stands in for the whole site. |
| **Incremental crawling** | Re-runs ask APIs for changes only, skip feed and sitemap entries older than the last run, never re-download a document (PDFs don't change), and revisit deep pages weekly. Links that match `crawl.hot_links` are crawled first. |
| **Resumable backfill** | A first structured pull walks history in windows and checkpoints after each; an interrupted run resumes at the checkpoint. |
| **Scheduler** (`opencrawl run`) | Tiered cadence (tier a daily, b weekly, c monthly by default), most-overdue first, several hosts in parallel, inside a time budget. Page crawling only inside off-peak windows; APIs and feeds any time. |
| **Parallel runs** (`--shard i/N`, `delta`, `merge`) | Sources split across runners by host, so each site is only ever hit from one place. Each runner ships a small delta; `merge` applies them (newest wins, idempotent). |
| **Politeness** | robots.txt (incl. Crawl-delay), per-host spacing with per-host floors, back-off on 429/503/slow responses, Retry-After, conditional GETs, honest User-Agent. |
| **Blocked sites** (`opencrawl blocked`, `opencrawl tasks`) | A host answering with a bot-protection challenge is left alone for a week; its sources re-route where possible; the rest becomes a manual task list. The crawler never tries to defeat bot protection. |
| **Derive** (`opencrawl derive`) | Parses what was crawled into structured records, incrementally, through pluggable derivers (`settings.yaml derivers:`). Built in: election results and qualifying lists, minutes → motions and named votes, ballot questions, press releases → appointments, voter guides → endorsements and ratings. |
| **Transcripts** (`opencrawl transcripts`) | Meeting or event video pages → captions/transcripts (Granicus viewer, VTT/SRT, YouTube Data API; optional local Whisper). Mentions carry a time code. |
| **Screenshots box** (`opencrawl screens`) | Drop screenshots in `imports/screens/` (from a phone via github.com upload). Each is read and filed: duplicates skipped, phone status bars removed, dark mode handled, long scrolls merged, platform/handle/dates/URLs picked out, targets and subjects matched. `screens capture URL` screenshots allowed pages (never sites that refuse crawlers). |
| **Screenshot → text** (`opencrawl ocr`) | Screenshots, scans and PDFs to Markdown with structure-aware headings, confidence scores and lines to double-check. |
| **Export** (`opencrawl export`) | `dist/bundle/`: targets, per-target records, subjects, research packets in a small `{t,u,d}` source format, profiles, connections, verified posts, rumor mill, blocked hosts, manifest, schema. Stable ids for upserts. |

### Civic module (optional)

Everything a local-government project needs, switched on by putting a `jurisdictions.yaml` registry in `config/`
(see `examples/colorado-civic`, which covers every county, municipality and school district in one state):

- **Adapters** for Legistar (matters, meetings, roll-call votes, office records), CivicClerk and BoardDocs; CivicPlus
  AgendaCenter and Granicus through their feeds; eCode360 and American Legal code libraries through the browser channel.
  Municode is recorded but never crawled (it blocks crawlers).
- **Derivers** for city/county election results and qualifying lists, minutes → motions and named votes, ballot
  questions (text, amounts, results), press releases → appointments, endorsement / scorecard / questionnaire pages.
- **Official YouTube channels only**: a channel becomes a source only when the government's own website links to it (or a
  hand check in `overrides.yaml` says so) and the channel is plainly the government's; everything else goes to a
  separate **Rumor Mill – Video** bucket, never into the records.
- **Public records requests** (`opencrawl records`): for jurisdictions whose code can't be crawled, the crawler finds the
  records email or portal on the official site (never guessed), drafts one request per jurisdiction under the law you
  configure (name, citation, response deadline), sends a small daily batch from a project mailbox, reads replies, files
  attached codes into the Screenshots box, and follows up when the deadline passes. See **Open records requests**.
- **Campaign finance** (`opencrawl finance`): state-system CSV exports, disclosure PDFs posted by cities, FEC receipts and
  disbursements; entity resolution across filings; what each organization is (FEC, IRS 990 via ProPublica,
  OpenCorporates, the org's own about page); a connections graph with patterns (shared donors, employer clusters,
  officer overlap, donor-matter leads).

### Research module (optional)

A **subject** is anyone or anything you track by name — a candidate, an official, an executive, an organization.
`config/subjects.yaml` (or `opencrawl subjects import list.csv`) lists them; `opencrawl subjects research` collects, per
subject: mentions in every crawled record (full-text search, no network), votes and sponsorships (civic), statements
from the subject's own websites, verified social posts (Bluesky / YouTube / X with a token), news via GDELT with each
article **opened** and checked for the name **and** a place word before it counts, Google News leads, Bluesky mentions,
and money. Every item is graded:

- `primary` — a target's own record or the subject's own channel
- `reported` — a news outlet listed in `config/outlets.yaml`
- `lead` — found in a search index but not opened: listed, never cited
- `unverified` — everything else: kept in the **Rumor Mill** with a status (unverified → corroborated / debunked /
  retracted) and a corroboration trail; never in a profile or packet.

`opencrawl profiles` then builds one profile supplement per subject (positions by issue, statements, ideology markers,
endorsements, ratings, votes, bills, career timeline, money), every item pointing at the record it came from.

## Quick start

```bash
git clone https://github.com/<you>/OpenCrawl.git && cd OpenCrawl
pip install -e .
sudo apt-get install tesseract-ocr            # OCR (optional)
python -m playwright install chromium         # only for script-rendered sites (optional)

opencrawl init my-project                     # a fresh project (config/, data/, state/, imports/)
#   or: opencrawl init my-colorado --example colorado-civic
cd my-project
#   edit config/targets.yaml, config/sources.yaml, config/settings.yaml (set http.user_agent to your repo)

opencrawl seed --max-minutes 30               # discover websites, portals, feeds (resumable)
opencrawl targets --sources                   # what will be crawled
opencrawl run --max-minutes 30                # crawl what is due
opencrawl derive --max-minutes 10             # structured records from pages and documents
opencrawl subjects research --max-minutes 20  # if you listed subjects
opencrawl search "short-term rental" --target springfield
opencrawl export                              # dist/bundle/
opencrawl status                              # coverage, crawl progress, database size
```

Useful flags: `run --source <id>` (one target or source), `--force` (ignore the off-peak window), `--full` (re-crawl
history instead of a delta), `--shard 0/4`, `--tier a`.

A project is any directory with `config/settings.yaml`; `opencrawl` finds it from the current directory or
`$OPENCRAWL_ROOT`. The repository itself is a project too (its `config/` is the starter), so everything works right
after cloning.

## Configuration

| File | What goes in it |
|---|---|
| `config/settings.yaml` | Behavior: User-Agent and contact, per-host spacing, off-peak windows, cadence by tier, page budgets, hot/cold link words, evidence grading, research budgets, derivers, public-records law, civic state. Every key is commented in the starter file. |
| `config/targets.yaml` | Targets: `id`, `name`, `kind`, `website` (or `wikidata` / `wikipedia` / a `search_hint` for lookup), `tier`, `include` / `exclude`, `portals`, `extra_feeds`, `tags`, `enabled`. |
| `config/sources.yaml` | Standalone sources: `id`, `url`, `kind`, `tier`, `include` / `exclude`, `max_pages`, `depth`, `channel` + `endpoint` to pin, `render: browser`, `feeds`, `coverage`. An entry whose id matches a target's source overrides it. |
| `config/overrides.yaml` | Hand edits per target that win over discovery: `website`, `portals`, `tier`, `enabled`, `include`, `youtube_checked`, `notes`. |
| `config/subjects.yaml` | People and organizations to research: `id`, `name`, `aliases`, `kind`, `target(s)`, `places`, `websites`, `social`, `search_terms`, `news_feeds`, `priority`; civic: `office`, `party`, `races`, `finance_csv`, `fec_id`. |
| `config/outlets.yaml` | News domains that count as `reported` evidence; domains never used at all. |
| `config/topics.yaml`, `config/issues.yaml` | Regex topic tags for records, and issue tags for subjects' positions. |
| `config/finance.yaml`, `config/organizations.yaml` | Campaign-finance sources and hand notes on organizations. |
| `config/jurisdictions.yaml`, `school_districts.yaml`, `press.yaml`, `guides.yaml` | Civic registry, official newsrooms and voter-guide organizations (see the example project). |
| `config/backfill.json` | The daytime backfill switch and limits for GitHub Actions. |

State the crawler keeps: `state/discovered.json` (what seed found), `state/progress.json` (per-source crawl status),
`state/channels.json` (channel choices), `state/blocked.json` (hosts that refused), cursors and entity ids in the
database. `data/crawl.db` is the database; `data/verified/` the verified-posts registry; `data/rumor_mill/` the rumor
mill; `data/openrecords/requests.json` the records-request tracker.

## Sourcing rules

- Every record's `url` is a page or API resource the crawler actually opened (HTTP 200), or a link that resource itself
  returned. Nothing is built from a URL pattern.
- Articles count only after the crawler opened them and found the subject's name (and, when configured, a place word)
  in the text; same-named people elsewhere are dropped. Search hits that can't be opened are `lead`s.
- Unverified material never reaches a profile or packet's sources; it sits in the Rumor Mill with a status and a
  corroboration trail.
- The crawler never collects photos of people.
- robots.txt is honored everywhere except the handful of documented public APIs listed under `http.api_endpoints`
  (Wikidata, Wikipedia, FEC, ProPublica, OpenCorporates), whose operators invite programmatic use.

## Scheduling and cost (GitHub Actions)

`.github/workflows/crawl.yml` runs **prepare → crawl (N shards) → merge** nightly, with seeding and a longer research
block weekly. The database and the bundle are assets of the `crawl-data` release; small state files are committed back
to the repository. Nothing in the workflow is specific to one project: it reads `config/` like the CLI does.

Private repositories have a monthly Actions allowance (about 2,000 Linux minutes on GitHub Free, 3,000 on Pro, billed
per minute past that; public repositories are free). Every step has a time cap rather than a fixed cost. Raise
throughput with repository variables `CRAWL_SHARDS`, `CRAWL_MINUTES`, `SEED_MINUTES`, `RESEARCH_MINUTES`,
`DERIVE_MINUTES`, `TRANSCRIPT_MINUTES`, `SPONSOR_MINUTES`, `FINANCE_MINUTES`, or use a self-hosted runner (no minute
cost). The **daytime backfill** (`config/backfill.json`) chains back-to-back runs while subject profiles are still
filling and switches itself off; `month_minutes_cap` is its own ceiling.

**Spending cap.** github.com/settings/billing → **Budgets and alerts → New budget** → product **Actions** → an amount →
check **Stop usage when budget limit is reached** and the alert thresholds → **Create budget**. Keep one Actions
budget only.

## Secrets and API keys

Every key is a repository secret: GitHub stores it encrypted and the workflow passes it to the steps that need it; it
is never written to a file, a log or the database. Keys go in by hand, never through a chat or a commit.

**Adding a secret.** Your repository on github.com → **Settings → Secrets and variables → Actions → New repository
secret**: type the name exactly as below, paste the value, **Add secret**. Or, from a terminal with the GitHub CLI,
`gh secret set FEC_API_KEY --repo <you>/<repo>` (it prompts for the value, which stays out of your shell history).
The next run uses it; nothing else changes. Each run prints which keys are set or missing (never their values).

| Secret | What it adds | How to get it |
|---|---|---|
| `YOUTUBE_API_KEY` | Targets' YouTube channels (listings and transcripts) and subjects' videos. | **console.cloud.google.com**: create a project → **APIs & Services → Library** → *YouTube Data API v3* → **Enable** → **Credentials → Create credentials → API key** → restrict it to the YouTube Data API. Free: 10,000 units a day; the crawler uses 1-unit calls. |
| `FEC_API_KEY` | Federal campaign finance (receipts, disbursements, committees). Without it the public DEMO_KEY allows 30 requests an hour. | **api.data.gov/signup**: name and email; the key arrives by email. 1,000 requests an hour. Free. |
| `X_BEARER_TOKEN` | Verified subject posts on X. Optional. | **developer.x.com**: a developer account and app; the app's Bearer Token. Check the current API plan terms first. |
| `OPENCORPORATES_API_KEY` | Corporate officers for organizations in campaign finance. Optional. | Apply for an API account at **opencorporates.com**. |
| `ORR_SMTP_HOST`, `ORR_SMTP_PORT`, `ORR_USER`, `ORR_PASSWORD`, `ORR_FROM`, `ORR_IMAP_HOST` | Public records requests from the project mailbox (see below). | The mailbox provider's SMTP/IMAP settings; use an app password, not the account password. |

## Open records requests (`opencrawl records`)

For jurisdictions whose code can't be crawled, the crawler asks the clerk for it under your state's public records
law from a **dedicated project mailbox** — never a personal address. It finds the records email or portal on the
official site (`records contacts`; never guessed addresses), drafts one request per jurisdiction signed with
`openrecords.requester`, sends a small daily batch (`openrecords.daily_limit`, default 10), never asks the same place
twice within `cooldown_days`, reads replies over IMAP (attached codes go to `imports/screens/ordinances/` and into the
Screenshots box; cost estimates, denials and bounces are recorded), and sends one follow-up when the response deadline
has passed. Portal-only jurisdictions are listed for submitting by hand. The tracker is `data/openrecords/requests.json`,
committed with the rest of the state so the history survives every run.

**Setup, once.**

1. Create a mailbox for the project (any provider with SMTP and IMAP; a plain mailbox at your own domain works best).
   Use an **app password** if the provider offers one.
2. Add the six `ORR_*` secrets to the repository (above). `ORR_FROM` is the address requests come from and the address
   the crawler's User-Agent carries as its contact.
3. In `config/settings.yaml`, set `openrecords.requester` (the project's name), the `law`, `citation` and
   `response_days` for your state (the Colorado example uses the Colorado Open Records Act, C.R.S. § 24-72-201 et seq.,
   three working days), and finally `openrecords.enabled: true`.
4. `opencrawl records plan --show` previews who would be asked and the exact letter; `opencrawl records status` shows
   where every request stands. The nightly workflow runs `contacts → check → follow-up → send → status`.

Nothing is sent until all of `enabled`, `requester` and the mailbox secrets are in place.

## What needs a human

- **Municode** (many codes of ordinances) blocks crawlers: `opencrawl tasks` lists confirmed links to capture by hand
  (`opencrawl ocr`) or request from the clerk.
- **Facebook / Instagram**: no open read API; add posts by hand with a screenshot (`opencrawl posts add`).
- **State campaign finance**: most systems have no bulk API; export CSVs into `imports/finance/<subject-id>.csv` and
  confirm the system's search URL in `config/finance.yaml` after `opencrawl finance probe`.
- **Organizations**: registries say what a PAC or nonprofit is; add websites and hand notes in
  `config/organizations.yaml` for the rest.
- **Leads** and **donor-matter patterns**: open both records and confirm it is the same entity before citing.
- **Transcript lines**: speaker labels are what the transcript says; items are marked `needs_review`.

## Extending

- **A new structured system**: a module in `opencrawl/adapters/` with `NAME`, `detect`, `probe`, `crawl`, `shard_key`,
  registered with `register()`; a `PORTAL_PATTERNS` entry so seed recognizes its links; a `PORTAL_SOURCES` entry so
  targets expand into it. See `docs/extending.md`.
- **A new deriver**: a module that calls `derive.register(Deriver(name, where, fn))`, listed in `settings.yaml derivers:`.
- **Another state or country**: copy `examples/colorado-civic`, rebuild `jurisdictions.yaml` from your own lists with
  `scripts/build_registry.py`, set `civic.state_*` and the records law in `settings.yaml`.

## Tests

```bash
pytest -q   # offline: local servers, synthetic data, generated screenshots and PDFs
```

## License

MIT — see `LICENSE`.
