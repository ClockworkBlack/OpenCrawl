# Subject research

The research module tracks named people and organizations across everything the crawler has, the news and social
media, and keeps every finding tied to the page it came from and graded by how much it can be trusted.

## Listing subjects

`config/subjects.yaml`:

```yaml
subjects:
  - id: jane-example
    name: Jane Example
    aliases: ["Jane Q. Example", "J. Example"]
    kind: person                      # or organization
    target: example-org               # the target whose records are searched first
    targets: [example-org, co-baca]   # several, if their records all matter
    places: ["Example Organization", "Colorado"]   # an article must contain one of these besides the name
    websites: ["https://www.janeexample.example/"] # her own sites: primary evidence; the first one is the proof page
    social: ["https://bsky.app/profile/jane.example"]
    search_terms: ['"Jane Example" Acme']          # extra news queries (default: the name plus the target's name)
    news_feeds: []                                  # feeds whose items are opened and checked for the name
    priority: true                                  # researched before everyone else
```

Or import a spreadsheet: `opencrawl subjects import candidates.csv` (columns `id, name, aliases, kind, target,
targets, places, websites, social, search_terms, news_feeds, office, party, priority, key, fec_id, finance_csv`; list
columns separated by `;`). JSON works too (a list, or `{"subjects": [...]}`). Imported subjects live in
`state/subjects_imported.json`; hand entries in `subjects.yaml` win on conflicts and list fields are unioned.

Civic fields: `office`, `party`, `counties`, `races` (contests for the career timeline), `finance_csv`, `fec_id`.

## What research does

`opencrawl subjects research [--subject ID] [--max-minutes M] [--skip step,step]` runs these steps per subject:

| step | source | cost |
|---|---|---|
| mentions | full-text search over every crawled record (the subject's targets first) | none |
| votes, sponsorships | roll calls and sponsor lists already in the database (civic adapters) | none |
| posts | verified social accounts: Bluesky, YouTube, X (with a token) | only if accounts known |
| statements | the subject's own websites (crawled like any source) | only if sites known |
| articles | GDELT news index, several names per query; each article opened and checked for the name and a place word | shared across subjects |
| leads | Google News RSS (links can't be opened directly: listed, never cited) | 1 request |
| social_mentions | Bluesky public search | 1–3 requests |
| connections | endorsements on the subject's site, `imports/finance/<id>.csv`, FEC (federal candidates) | varies |

Subjects are researched in priority order (priority flag, never researched, thinnest profile, most recent race),
inside the time budget, and resume next run (`state/subject_progress.json`). `settings.yaml subjects:` sets the
budgets and switches.

## Evidence

| grade | meaning | where it goes |
|---|---|---|
| primary | a target's own record (vote, agenda item, document on its site) or the subject's own channel (website, verified social account); `.gov` / `.mil` and `evidence.primary_domains` | records, packets, profiles |
| reported | a news outlet listed in `config/outlets.yaml` | records, packets, profiles |
| lead | found in a search index but not opened (blocked, paywalled, robots, redirect-only) | packets, as a list to open by hand |
| unverified | everything else | the Rumor Mill only |

A social account is `verified` only when an official page (the subject's own site, a council bio) links to that exact
profile; the link is the proof and it is re-checked (`opencrawl posts verify`).

## The Rumor Mill

`data/rumor_mill/rumors.jsonl`: one row per unverified claim with the subject, URL, domain, claim text, origin,
status and a corroboration trail. `opencrawl rumor list|corroborate|debunk|retract` moves items; a corroborated item
still only reaches a profile through the evidence that corroborated it. Videos from YouTube channels that could not
be confirmed as a target's official account go to a separate `data/rumor_mill/videos.jsonl`.

## Outputs

- **Packets** (`dist/bundle/packets/<id>.json`): `sources[{t,u,d,kind,evidence,opened_at,excerpt}]` (title, url,
  date), votes by topic, mentions, articles, statements, endorsements, finance, posts, social mentions, leads,
  rumors. `src` lists are indexes into `sources`.
- **Profiles** (`dist/bundle/profiles/<id>.json`): positions by issue (`config/issues.yaml`), statements, ideology
  markers, endorsements, ratings, votes, bills, ordinances supported/opposed, affiliations, career timeline, money —
  each item with `src` indexes. `inference_inputs` counts the sourced items a downstream site could reason from;
  `profiles/index.json` lists the thinnest first.
- **Connections** (`dist/bundle/connections.json`): the donors / organizations / subjects / offices graph and its
  patterns (shared donors, employer clusters, org chains, officer overlap, donors who hold office, shared endorsers,
  donor-matter leads — evidence `lead`: verify first).
