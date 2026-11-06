# Security

## Reporting a vulnerability

Please report security problems privately through GitHub: the repository's **Security** tab → **Report a
vulnerability** (private vulnerability reporting). Don't open a public issue for them. Include what is affected, how
to reproduce it and what an attacker could do with it. You should get an answer within a week; fixes are released as
soon as they are ready, with credit if you want it.

## What OpenCrawl defends against

OpenCrawl reads the open web, email and files people upload, and some of what it collects concerns people. It treats
all of that as untrusted. The design assumes a hostile page, server, mailbox sender or uploaded file, and aims to keep
each from reaching the machine the crawler runs on, its secrets, or the people in its data.

| Threat | Defense | Where |
|---|---|---|
| A page or redirect points the crawler at cloud metadata services (credential theft) or, when you crawl the open web, at internal services (SSRF) | Metadata addresses always blocked by default; with `http.allow_private_networks: false`, public addresses only. Checked before each request and again at connect time against the resolved address; every redirect hop re-checked; the headless browser confined to a guard proxy with the same rule | `netguard.py`, `fetch.py`, `browser.py` |
| DNS rebinding | The connection goes to the addresses that were checked; a name that resolves to any non-public address is refused | `netguard.py` |
| Credentials leaking across hosts | API keys in headers where the API allows; credential headers dropped on cross-host redirects; secrets redacted from logs, state and exports | `fetch.py`, `redact.py` |
| Resource exhaustion (huge or slow responses, decompression bombs, pathological PDFs, ReDoS) | Size and total-time caps, read-at-a-time streaming, pixel checks before decoding, page/time/render caps for PDFs, regex audit | `fetch.py`, `ingest.py`, `scripts/redos_audit.py` |
| Spoofed or malicious email replies | Sender domain must match the request's (or a configured portal), DMARC/SPF failures rejected, attachments checked by content and size, headers fetched before bodies | `civic/openrecords.py` |
| Mailbox credential theft in transit | TLS with certificate verification for SMTP (465/STARTTLS 587) and IMAP (993); no plain-text fallback | `civic/openrecords.py` |
| Malicious database files (a delta from another runner) | SQLite `trusted_schema=OFF` and defensive mode; deltas must be plain tables with the expected columns and pass an integrity check | `store.py`, `shard.py` |
| A compromised page attacking the browser | Chromium sandbox on; minimal environment (no secrets); no downloads, service workers or permissions; trackers blocked | `browser.py` |
| Path traversal through ids, archive members or attachment names | Ids validated before they name files; archive members and attachment names sanitized | `targets.py`, `export.py`, `vault.py`, `civic/openrecords.py` |
| Workflow command injection through crawled text in CI logs | Workflow commands switched off around every step that prints crawled text; output filtered | `.github/workflows/crawl.yml`, `cli.py` |
| Script injection through workflow inputs or variables | Inputs and variables validated by one script and passed only through environment variables | `scripts/ci_plan.py` |
| Over-privileged CI tokens, poisoned dependencies | Read-only jobs except the final publish job; credentials not persisted in checkouts that process crawled content; actions pinned to commits; exact package pins installed from wheels only; Dependabot | `.github/` |
| Data exposure on GitHub (public release assets, artifacts, logs) | Everything stored or passed between jobs is encrypted (AES-256-GCM); the workflow refuses to run without a key; subject ids hashed in logs | `vault.py`, `scripts/ci_data.sh` |

`opencrawl doctor` checks a project for the settings that weaken these (an open network guard, secrets in config files,
missing encryption key in CI, readable files, old library versions).

## Operating it safely

- Local, intranet and cloud-hosted sites can be crawled by default. When you crawl the open web from a machine that
  can reach things you wouldn't publish (a work network, a server next to databases), set
  `http.allow_private_networks: false` so a hostile page can't steer the crawler there; keep `http.block_metadata` on
  everywhere, and always on CI runners.
- Put keys and passwords in environment variables or repository secrets, never in `config/`.
- Run the browser with its sandbox. `OPENCRAWL_BROWSER_ALLOW_NO_SANDBOX=1` is for containers running as root.
- Use a dedicated mailbox with an app password for records requests.
- Keep `OPENCRAWL_DATA_KEY` somewhere safe besides the repository secret: without it the stored data can't be read.
- Let Dependabot's pull requests through after `ci.yml` passes.
