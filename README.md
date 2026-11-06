# OpenCrawl

A polite, resumable crawler for **any list of websites, organizations or causes**, with optional modules for
**government records** (meeting portals, ordinances, votes, elections, public records requests) and
**subject research** (everything said by and about a list of people or organizations, graded by evidence).

Give it a list of targets (a city, a company, an agency, a coalition: anything with a website) and it finds where
their records live, picks the cheapest channel for each (API, RSS, sitemap, HTML, headless browser), crawls politely
inside a time budget, keeps everything in one SQLite database with full-text search, and exports a JSON bundle
that a website or a spreadsheet can take as it is. It runs on your laptop or on a GitHub Actions schedule.

Crawl anything you want: public websites, a site running on your own machine (`http://localhost:8000`), an intranet
or a site hosted on a private cloud network. It is built to be safe to point at the open web and to handle
information about people with care: see **Security** and **Privacy** below, [`SECURITY.md`](SECURITY.md) and
[`PRIVACY.md`](PRIVACY.md).

## What's in it

| Piece | What it does |
|---|---|
| **Targets** (`config/targets.yaml`) | The things you cover. Each expands into one source per place its records live: its website (`<id>:site`), its YouTube channel (`<id>:youtube`), a meeting portal or other structured system when an adapter knows it. Hand edits in `config/overrides.yaml`. |
| **Sources** (`config/sources.yaml`) | Any website, feed, API or portal on its own, with link filters, page budgets and a pinned channel if you want one. |
| **Seed** (`opencrawl seed`) | Finds each target's website (given, or looked up on Wikidata / Wikipedia by name), then its portals, feeds, API hints and public-records contact. Biggest targets first; time-budgeted; re-checks entries older than N days. |
| **Database** (`data/crawl.db`) | SQLite (WAL) with full-text search. Upserts by natural key: a changed page updates its row (version+1) instead of piling up copies. Holds the URL frontier and resume cursors. |
| **Channel pick** | Per source: probe the official API, WordPress API, feeds, sitemap, HTML, browser; use the fastest working structured one. Probing fingerprints the platform first, so it costs 2–4 requests instead of a dozen blind guesses. |
| **Incremental crawling** | Re-runs ask APIs for changes only, skip feed and sitemap entries older than the last run, never re-download a document (PDFs don't change), and revisit deep pages weekly. |
| **Scheduler** (`opencrawl run`) | Tiered cadence (tier a daily, b weekly, c monthly by default), most-overdue first, several hosts in parallel, inside a time budget. Page crawling only inside off-peak windows; APIs and feeds any time. |
| **Parallel runs** (`--shard i/N`, `delta`, `merge`) | Sources split across runners by host, so each site is only ever hit from one place. Each runner ships a small delta; `merge` applies them (newest wins, idempotent). |
| **Politeness** | robots.txt (incl. Crawl-delay), per-host spacing, back-off on 429/503/slow responses, Retry-After, conditional GETs, an honest User-Agent. |
| **Blocked sites** (`opencrawl blocked`, `opencrawl tasks`) | A host answering with a bot-protection challenge is left alone for a week; its sources re-route where possible; the rest becomes a manual task list. The crawler never tries to defeat bot protection. |
| **Derive** (`opencrawl derive`) | Parses what was crawled into structured records, incrementally, through pluggable derivers (`settings.yaml derivers:`). Built in: election results and qualifying lists, minutes → motions and named votes, ballot questions, press releases → appointments, voter guides → endorsements and ratings. |
| **Transcripts** (`opencrawl transcripts`) | Meeting or event video pages → captions/transcripts (Granicus viewer, VTT/SRT, YouTube Data API; optional local Whisper). Mentions carry a time code. |
| **Screenshot inbox** (`opencrawl screens`) | Drop screenshots in `imports/screens/` (from a phone via github.com upload). Each is read and filed: duplicates skipped, phone status bars removed, dark mode handled, long scrolls merged, platform/handle/dates/URLs picked out, targets and subjects matched. `screens capture URL` screenshots allowed pages (never sites that refuse crawlers). |
| **Screenshot → text** (`opencrawl ocr`) | Screenshots, scans and PDFs to Markdown with structure-aware headings, confidence scores and lines to double-check. |
| **Export** (`opencrawl export`) | `dist/bundle/`: targets, per-target records, subjects, research packets, profiles, connections, verified posts, blocked hosts, manifest, schema. Stable ids for upserts. |
| **Security and privacy** (`opencrawl doctor`, `purge`, `cache`, `vault`) | A settings check, erasure and retention, the page cache, and encrypted files for data that leaves the machine. |

### Civic module (optional)

Everything a local-government project needs, switched on by putting a `jurisdictions.yaml` registry in `config/`
(see the example project `colorado-civic`, which covers every county, municipality and school district in one state):

- **Adapters** for Legistar (matters, meetings, roll-call votes, office records), CivicClerk and BoardDocs; CivicPlus
  AgendaCenter and Granicus through their feeds; eCode360 and American Legal code libraries through the browser channel.
  Municode is recorded but never crawled (it blocks crawlers).
- **Derivers** for city/county election results and qualifying lists, minutes → motions and named votes, ballot
  questions (text, amounts, results; your state's question kinds in `civic.ballot_types`), press releases →
  appointments, endorsement / scorecard / questionnaire pages.
- **Official YouTube channels only**: a channel becomes a source only when the government's own website links to it (or a
  hand check in `overrides.yaml` says so) and the channel is plainly the government's; everything else goes to the
  video quarantine, never into the records.
- **Public records requests** (`opencrawl records`): for jurisdictions whose code can't be crawled, the crawler finds the
  records email or portal on the official site (never guessed), drafts one request per jurisdiction under the law you
  configure, sends a small daily batch from a project mailbox, reads replies (only from the domain asked), files attached
  codes into the screenshot inbox, and follows up when the deadline passes. See **Open records requests**.
- **Campaign finance** (`opencrawl finance`): state-system CSV exports, disclosure PDFs posted by governments, FEC receipts
  and disbursements; entity resolution across filings; what each organization is (FEC, IRS 990 via ProPublica,
  OpenCorporates, the org's own about page); a connections graph with patterns (shared donors, employer clusters,
  officer overlap, donor-matter leads).

### Research module (optional)

A **subject** is anyone or anything you track by name: a person, an official, a candidate, an executive, an
organization. `config/subjects.yaml` (or `opencrawl subjects import list.csv`) lists them; to limit research to
public figures, set `privacy.public_figures_only: true`. `opencrawl subjects research` collects, per subject: mentions in every crawled record (full-text search, no network), votes and
sponsorships (civic), statements from the subject's own websites, verified social posts (Bluesky / YouTube / X with a
token), news via GDELT with each article **opened** and checked for the name **and** a place word before it counts,
Google News leads, and money. Every item is graded:

- `primary`: a target's own record or the subject's own channel
- `reported`: a news outlet listed in `config/outlets.yaml`
- `lead`: found in a search index but not opened; listed, never cited
- `unverified`: everything else; kept in the **quarantine** with a status (unverified → corroborated / debunked /
  retracted) and a corroboration trail; never in a profile or packet, and not exported unless you ask.

`opencrawl profiles` then builds one profile per subject (positions by issue, statements, ideology markers,
endorsements, ratings, votes, bills, career timeline, money), every item pointing at the record it came from.

## Quick start

```bash
pip install "opencrawl[all] @ git+https://github.com/<you>/OpenCrawl.git"   # or: git clone ... && pip install -e ".[all]"
sudo apt-get install tesseract-ocr            # OCR (optional)
python -m playwright install chromium         # only for script-rendered sites and screenshots (optional)

opencrawl init my-project                     # a fresh project (config/, data/, state/, imports/)
#   or: opencrawl init my-colorado --example colorado-civic
cd my-project
#   edit config/targets.yaml, config/sources.yaml, config/settings.yaml (set http.user_agent to your repo)
opencrawl doctor                              # settings, secrets, permissions, library versions

opencrawl seed --max-minutes 30               # discover websites, portals, feeds (resumable)
opencrawl targets --sources                   # what will be crawled
opencrawl run --max-minutes 30                # crawl what is due
opencrawl derive --max-minutes 10             # structured records from pages and documents
opencrawl subjects research --max-minutes 20  # if you listed subjects
opencrawl search "short-term rental" --target example-city
opencrawl export                              # dist/bundle/
opencrawl status                              # coverage, crawl progress, database size
```

### Local, intranet and cloud-hosted sites

Point a target or source at any address the machine can reach: `http://localhost:8000/`, `http://intranet.corp/`, a
private IP, a site on a private cloud network. Nothing to switch on:

```yaml
# config/targets.yaml
targets:
  - {id: my-dev-site, name: My dev site, website: "http://localhost:8000/", tier: a}
  - {id: wiki, name: Team wiki, website: "http://10.20.0.15/", tier: b}
```

Only cloud metadata services stay blocked (they hold the machine's credentials). If you crawl the open web from a
machine with sensitive neighbours, `http.allow_private_networks: false` keeps hostile pages from steering the crawler
into your network.

Useful flags: `run --source <id>` (one target or source), `--force` (ignore the off-peak window), `--full` (re-crawl
history instead of a delta), `--shard 0/4`, `--tier a`.

A project is any directory with `config/settings.yaml`; `opencrawl` finds it from the current directory or
`$OPENCRAWL_ROOT`. A clone of this repository is a project too (its `config/` is the starter), which is how the
GitHub Actions workflow uses it.

## Configuration

| File | What goes in it |
|---|---|
| `config/settings.yaml` | Behavior: User-Agent and contact, the network guard, per-host spacing, off-peak windows, cadence by tier, page budgets, hot/cold link words, evidence grading, research budgets, derivers, privacy, public-records law, civic state. Every key is commented in the starter file. |
| `config/targets.yaml` | Targets: `id`, `name`, `kind`, `website` (or `wikidata` / `wikipedia` / a `search_hint` for lookup), `tier`, `include` / `exclude`, `portals`, `extra_feeds`, `tags`, `enabled`. |
| `config/sources.yaml` | Standalone sources: `id`, `url`, `kind`, `tier`, `include` / `exclude`, `max_pages`, `depth`, `channel` + `endpoint` to pin, `render: browser`, `feeds`, `coverage`. An entry whose id matches a target's source overrides it. |
| `config/overrides.yaml` | Hand edits per target that win over discovery: `website`, `portals`, `tier`, `enabled`, `include`, `youtube_checked`, `notes`. |
| `config/subjects.yaml` | Public figures and organizations to research: `id`, `name`, `aliases`, `kind`, `office` / `public_role` / `title`, `target(s)`, `places`, `websites`, `social`, `search_terms`, `news_feeds`, `priority`, `external_id`, `known_urls`; civic: `party`, `races`, `finance_csv`, `fec_id`. |
| `config/outlets.yaml` | News domains that count as `reported` evidence; domains never used at all. |
| `config/topics.yaml`, `config/issues.yaml` | Regex topic tags for records, and issue tags for subjects' positions. |
| `config/finance.yaml`, `config/organizations.yaml` | Campaign-finance sources and hand notes on organizations. |
| `config/jurisdictions.yaml`, `school_districts.yaml`, `press.yaml`, `guides.yaml` | Civic registry, official newsrooms and voter-guide organizations (see the example project). |
| `config/backfill.json` | The daytime backfill switch and limits for GitHub Actions. |

Ids (targets, subjects) are lower-case letters, digits, `.`, `_` and `-`: they name files in the export.

State the crawler keeps: `state/discovered.json` (what seed found), `state/progress.json` (per-source crawl status),
`state/channels.json` (channel choices), `state/blocked.json` (hosts that refused), cursors and entity ids in the
database. `data/crawl.db` is the database; `data/verified/` the verified-posts registry; `data/quarantine/` unverified
claims and videos; `data/openrecords/requests.json` the records-request tracker.

## Sourcing rules

- Every record's `url` is a page or API resource the crawler actually opened (HTTP 200), or a link that resource itself
  returned. Nothing is built from a URL pattern.
- Articles count only after the crawler opened them and found the subject's name (and, when configured, a place word)
  in the text; same-named people elsewhere are dropped. Search hits that can't be opened are `lead`s.
- Unverified material never reaches a profile or packet's sources; it sits in the quarantine with a status and a
  corroboration trail.
- The crawler never collects photos of people.
- robots.txt is honored everywhere except the handful of documented public APIs listed under `http.api_endpoints`
  (Wikidata, Wikipedia, FEC, ProPublica, OpenCorporates), whose operators invite programmatic use.

## Security

- **Network guard.** Local, intranet and cloud-hosted sites can be crawled, but cloud metadata services
  (169.254.169.254, metadata.google.internal...) are blocked in any spelling (decimal, hex, IPv6-mapped...), because
  a page that links or redirects there could hand the crawler the machine's credentials (`http.block_metadata: false`
  lifts it). For crawling the open web from a machine that can reach things you wouldn't publish, set
  `http.allow_private_networks: false`: then only public internet addresses are fetched, plus the hosts you list in
  `http.allow_hosts`. Either way, names are checked again at connect time (a DNS answer that changes between the check
  and the connection gets nowhere), redirects are followed by hand with every hop checked like a new request (guard,
  robots.txt, block list, spacing), API keys never follow a redirect to another host, and the headless browser talks
  only to a local guard proxy that applies the same rule to every connection.
- **Limits.** Responses are capped in size (`max_bytes`) and total time (`max_response_seconds`); robots.txt is capped;
  images are checked for decompression bombs before they are decoded; PDFs are capped in pages, time and render size;
  every regular expression that reads web text is tested against catastrophic backtracking (`scripts/redos_audit.py`).
- **Untrusted mail.** Replies to records requests count only from the domain the request went to (or a records portal
  you list) and when the mailbox provider's DMARC/SPF results don't fail; attachments must be what their name says
  (checked by content) and within size limits. Mail is sent and read over verified TLS only.
- **Secrets** live in environment variables or repository secrets, are sent in request headers rather than URLs where
  the API allows, and are redacted from logs, state files and exports. `opencrawl doctor` fails when a config file holds
  something that looks like a key.
- **The browser** keeps its sandbox (set `OPENCRAWL_BROWSER_ALLOW_NO_SANDBOX=1` only where it can't run, e.g. as root in
  a container), starts with a minimal environment, refuses downloads, blocks service workers and known trackers.
- **Databases from elsewhere** (a parallel runner's delta) are opened with SQLite's defensive settings and checked
  before merging.
- **The workflow** is opt-in, pins every action to a commit, gives each job only the permissions it needs, passes every
  input through validation and environment variables, switches off workflow commands while crawled text is printed, and
  encrypts everything it stores.

Report vulnerabilities as described in [`SECURITY.md`](SECURITY.md).

## Privacy

- **Who is researched is your choice.** Anyone in `subjects.yaml`. Set `privacy.public_figures_only: true` to research
  a person only when they hold or seek a public role (`office`, `public_role` or `title`).
- **Least collection.** Articles are kept as excerpts; the Bluesky search of posts by members of the public is off
  unless `subjects.bluesky_search` is on; reply excerpts mask the addresses and phone numbers of the people in them.
- **Unverified claims stay private.** The quarantine is not exported unless `privacy.export_quarantine` is true.
- **Retention and erasure.** `privacy.retention_days` with `opencrawl purge --expired` deletes research, screenshots,
  quarantined claims, reply excerpts and cached pages past their age; `opencrawl purge --subject ID` erases everything
  about one subject. Deleted rows are overwritten and the database is vacuumed, so their text is gone from the file.
- **At rest.** Files are created readable by their owner only. Everything the workflow keeps on GitHub is encrypted
  (AES-256-GCM, `opencrawl vault`) with your `OPENCRAWL_DATA_KEY`; logs show subjects as short hashes
  (`OPENCRAWL_REDACT_LOGS=1`).
- **Nothing identifying goes out by default.** The User-Agent names the project; a contact address is added only if you
  set one (use a project or role address).

More in [`PRIVACY.md`](PRIVACY.md).

## Scheduling and cost (GitHub Actions)

`.github/workflows/crawl.yml` runs **prepare → crawl (N shards) → merge → publish** nightly, with seeding and a longer
research block weekly. It does nothing until you opt in:

1. `opencrawl vault keygen` → add the output as the repository secret **`OPENCRAWL_DATA_KEY`** (keep a copy somewhere
   safe: without it the stored data can't be read).
2. Settings → Secrets and variables → Actions → **Variables** → `OPENCRAWL_ENABLED` = `true`.

The database, state, received records and the export bundle are stored **encrypted** as assets of the `crawl-data`
release (`opencrawl-data.ocv`, `bundle.ocv`); restore them anywhere with `opencrawl vault unpack opencrawl-data.ocv .`.
Optional variables: `COMMIT_STATE=true` also commits small state files, the records-request tracker and received records
to the repository in plain text (on a public repository they become public); `PUBLIC_BUNDLE=true` also publishes the
export bundle unencrypted for a site to read. Nothing in the workflow is specific to one project: it reads `config/`
like the CLI does. `.github/workflows/ci.yml` runs the tests on every push; Dependabot keeps the pinned actions and
packages current.

Private repositories have a monthly Actions allowance (about 2,000 Linux minutes on GitHub Free, 3,000 on Pro, billed
per minute past that; public repositories are free). Every step has a time cap rather than a fixed cost. Raise
throughput with repository variables `CRAWL_SHARDS`, `CRAWL_MINUTES`, `SEED_MINUTES`, `RESEARCH_MINUTES`,
`DERIVE_MINUTES`, `TRANSCRIPT_MINUTES`, `SPONSOR_MINUTES`, `FINANCE_MINUTES`, or use a self-hosted runner (no minute
cost). The **daytime backfill** (`config/backfill.json`) chains back-to-back runs while subject profiles are still
filling and switches itself off; `month_minutes_cap` is its own ceiling.

**Spending cap.** github.com/settings/billing → **Budgets and alerts → New budget** → product **Actions** → an amount →
check **Stop usage when budget limit is reached** and the alert thresholds → **Create budget**.

## Secrets and API keys

Every key is a repository secret (or an environment variable on your own machine): GitHub stores it encrypted and the
workflow passes it only to the steps that need it; it is never written to a file, a log or the database. Keys go in by
hand, never through a chat or a commit.

**Adding a secret.** Your repository on github.com → **Settings → Secrets and variables → Actions → New repository
secret**: type the name exactly as below, paste the value, **Add secret**. Or, with the GitHub CLI,
`gh secret set FEC_API_KEY --repo <you>/<repo>` (it prompts for the value, which stays out of your shell history).

| Secret | What it adds | How to get it |
|---|---|---|
| `OPENCRAWL_DATA_KEY` | **Required for the workflow**: the key everything it stores is encrypted with. | `opencrawl vault keygen` |
| `YOUTUBE_API_KEY` | Targets' YouTube channels (listings and transcripts) and subjects' videos. | **console.cloud.google.com**: create a project → **APIs & Services → Library** → *YouTube Data API v3* → **Enable** → **Credentials → Create credentials → API key** → restrict it to the YouTube Data API. |
| `FEC_API_KEY` | Federal campaign finance (receipts, disbursements, committees). Without it the public DEMO_KEY allows 30 requests an hour. | **api.data.gov/signup**; the key arrives by email. |
| `X_BEARER_TOKEN` | Verified subject posts on X. Optional. | **developer.x.com**: a developer account and app; the app's Bearer Token. Check the current API plan terms first. |
| `OPENCORPORATES_API_KEY` | Corporate officers for organizations in campaign finance. Optional. | Apply for an API account at **opencorporates.com**. |
| `LEGISTAR_TOKENS` | Legistar clients that require an API token: `client=token,client2=token2`. Optional. | Ask the client's clerk or Granicus. |
| `ORR_SMTP_HOST`, `ORR_SMTP_PORT`, `ORR_USER`, `ORR_PASSWORD`, `ORR_FROM`, `ORR_IMAP_HOST`, `ORR_IMAP_PORT` | Public records requests from the project mailbox (see below). | The mailbox provider's SMTP/IMAP settings; use an app password, not the account password. |

## Open records requests (`opencrawl records`)

For jurisdictions whose code can't be crawled, the crawler asks the clerk for it under your state's public records
law from a **dedicated project mailbox**, never a personal address. It finds the records email or portal on the
official site (`records contacts`; never guessed addresses), drafts one request per jurisdiction signed with
`openrecords.requester`, sends a small daily batch (`openrecords.daily_limit`, default 10), never asks the same place
twice within `cooldown_days`, reads replies over IMAP, and sends one follow-up when the response deadline has passed.
A reply counts only when it comes from the domain the request went to (or a domain in
`openrecords.trusted_reply_domains`, such as the records portal a government uses); attached codes then go to
`imports/screens/ordinances/` and into the screenshot inbox, and cost estimates, denials and bounces are recorded.
Replies from anyone else are kept in `data/openrecords/unverified/` for review and change nothing. Portal-only
jurisdictions are listed for submitting by hand. The tracker is `data/openrecords/requests.json`; on GitHub it is kept
in the encrypted data archive (and committed to the repository too with `COMMIT_STATE=true`).

**Setup, once.**

1. Create a mailbox for the project (any provider with SMTP on 465/587 and IMAP on 993, with TLS; a mailbox at your own
   domain works best). Use an **app password** if the provider offers one.
2. Add the `ORR_*` secrets to the repository (above). `ORR_FROM` is the address requests come from.
3. In `config/settings.yaml`, set `openrecords.requester` (the project's name), the `law`, `citation` and
   `response_days` for your state (the Colorado example uses the Colorado Open Records Act, C.R.S. § 24-72-201 et seq.,
   three working days), and finally `openrecords.enabled: true`.
4. `opencrawl records plan --show` previews who would be asked and the exact letter; `opencrawl records status` shows
   where every request stands. The workflow runs `contacts → check → follow-up → send → status`.

Nothing is sent until all of `enabled`, `requester` and the mailbox secrets are in place.

## What needs a human

- **Municode** (many codes of ordinances) blocks crawlers: `opencrawl tasks` lists confirmed links to capture by hand
  (`opencrawl ocr`) or request from the clerk.
- **Facebook / Instagram**: no open read API; add posts by hand with a screenshot (`opencrawl posts add`).
- **State campaign finance**: most systems have no bulk API; export CSVs into `imports/finance/<subject-id>.csv` and
  confirm the system's search URL in `config/finance.yaml` after `opencrawl finance probe`.
- **Organizations**: registries say what a PAC or nonprofit is; add websites and hand notes in
  `config/organizations.yaml` for the rest.
- **Leads**, **quarantined claims** and **donor-matter patterns**: open the records and confirm before citing.
- **Transcript lines**: speaker labels are what the transcript says; items are marked `needs_review`.

## Extending

- **A new structured system**: a module in `opencrawl/adapters/` with `NAME`, `detect`, `probe`, `crawl`, `shard_key`,
  registered with `register()`; a `PORTAL_PATTERNS` entry so seed recognizes its links; a `PORTAL_SOURCES` entry so
  targets expand into it. See `docs/extending.md`.
- **A new deriver**: a module that calls `derive.register(Deriver(name, where, fn))`, listed in `settings.yaml derivers:`.
- **Another state or country**: start from `opencrawl init --example colorado-civic`, rebuild `jurisdictions.yaml` from
  your own lists with the example's `scripts/build_registry.py` (in `opencrawl/examples/colorado-civic/`), set
  `civic.state_*`, `civic.ballot_types` and the records law in `settings.yaml`.

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q                        # offline: local servers, synthetic data, generated screenshots and PDFs
python scripts/redos_audit.py    # regular expressions against catastrophic backtracking
```

## License

MIT, see `LICENSE`.
