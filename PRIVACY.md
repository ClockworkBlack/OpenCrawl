# Privacy

OpenCrawl collects public records and research about public figures and organizations. Some of it concerns people,
so it follows a few rules by default. They protect the people in the data and the people running the crawler.

## Whose information

- **Public figures and organizations only.** A person is researched only when `subjects.yaml` gives them an `office`
  (held or sought), a `public_role` or a `title`. Anyone else is skipped with a warning
  (`privacy.public_figures_only`, on by default).
- **Not the public at large.** Searching social media for posts that name a subject collects what members of the public
  wrote, so it is off unless `subjects.bluesky_search` is turned on. Screenshots of social media also show other people:
  `privacy.export_screenshot_text: false` keeps their OCR text out of exports.
- **No photos of people** are collected.

## What is kept, and for how long

- Articles are kept as excerpts, not whole copies, and are not stored in the HTTP cache.
- Unverified claims are kept apart in the quarantine, never in profiles or packets, and are not exported unless
  `privacy.export_quarantine` is true.
- Replies to records requests keep a short excerpt with email addresses and phone numbers masked.
- `privacy.retention_days` sets how long research, screenshots, quarantined claims, reply excerpts and cached pages are
  kept; `opencrawl purge --expired` (run by the workflow every night) deletes what is older.
- The HTTP cache holds full copies of fetched pages to save bandwidth; `opencrawl cache prune` empties it at any time.

## Erasure

`opencrawl purge --subject ID` removes everything held about one subject: their research records, quarantined
claims, verified accounts and posts, research progress, links from screenshots and their files in the export bundle.
Deleted rows are overwritten (SQLite `secure_delete`), the full-text index is optimized and the database vacuumed, so
their text is gone from the database file and its write-ahead log. Remove the subject from `subjects.yaml` (and any
imported list) too, or the next run researches them again; on GitHub, the next workflow run stores the purged data.

## Where data lives

- On your machine: files are created readable by their owner only (`umask 077`; the database is `0600`).
- On GitHub (the workflow): the database, state, received records and the export bundle are encrypted with your
  `OPENCRAWL_DATA_KEY` (AES-256-GCM) before they are stored as release assets or passed between jobs. Plain-text copies
  exist only when you choose them: `COMMIT_STATE=true` commits small state files and received records to the
  repository, `PUBLIC_BUNDLE=true` publishes the export bundle.
- In logs: subjects appear as short hashes in the workflow's logs (`OPENCRAWL_REDACT_LOGS=1`), and secrets never appear.

## What the crawler tells the sites it visits

Its User-Agent names the project (set `http.user_agent` to your repository). A contact address is added only if you set
`http.contact` or `OPENCRAWL_CONTACT`; use a project or role address, never a personal one. The records-request
mailbox address is never used for this.
