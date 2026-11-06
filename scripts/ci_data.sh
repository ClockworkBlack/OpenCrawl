#!/usr/bin/env bash
# Move the project's data between a CI runner and the repository's "crawl-data" release, always encrypted.
#
#   scripts/ci_data.sh pull            download opencrawl-data.ocv and unpack data/, state/, imports/ (first run: nothing)
#   scripts/ci_data.sh pack OUT        tidy the database, then seal data/, state/ and imports/ into OUT
#                                      (the HTTP cache and SQLite's -wal/-shm files are left out)
#   scripts/ci_data.sh seal DIR OUT    seal any directory (artifacts passed between jobs)
#   scripts/ci_data.sh open IN DEST    unpack a sealed file into DEST
#   scripts/ci_data.sh push FILE...    upload files as release assets (creates the release, a prerelease, if needed)
#
# The key is OPENCRAWL_DATA_KEY (a repository secret; `opencrawl vault keygen` makes one). Without it this script
# refuses to run: release assets and workflow artifacts of a public repository can be downloaded by anyone.
set -euo pipefail
TAG="${DATA_TAG:-crawl-data}"
ASSET="opencrawl-data.ocv"
PY="${PYTHON:-python}"

need_key() {
  if [ -z "${OPENCRAWL_DATA_KEY:-}" ]; then
    echo "::error::OPENCRAWL_DATA_KEY is not set. Create one with 'opencrawl vault keygen' and add it as a repository secret." >&2
    exit 1
  fi
}

case "${1:-}" in
  pull)
    need_key
    mkdir -p .ci-in
    # Tell "there is no data yet" apart from "the data couldn't be read": only the first may continue, or a later
    # step would replace the stored data with an empty archive.
    if assets=$(gh release view "$TAG" --json assets --jq '.assets[].name' 2>&1); then
      if printf '%s\n' "$assets" | grep -qx "$ASSET"; then
        gh release download "$TAG" -p "$ASSET" -D .ci-in --clobber
        "$PY" -m opencrawl vault unpack ".ci-in/$ASSET" .
        rm -f ".ci-in/$ASSET"
        echo "restored data ($( [ -f data/crawl.db ] && du -h data/crawl.db | cut -f1 || echo 'no database'))"
      else
        echo "the $TAG release has no data yet"
      fi
    elif printf '%s' "$assets" | grep -qi "not found"; then
      echo "no data yet (first run)"
    else
      echo "::error::could not read the $TAG release: $(printf '%s' "$assets" | head -c 300)" >&2
      exit 1
    fi
    touch .ci-restored
    ;;
  pack)
    need_key
    out="${2:?usage: ci_data.sh pack OUT}"
    if [ ! -f .ci-restored ]; then
      echo "::error::the stored data was not restored in this job: refusing to pack (it would replace the stored data)" >&2
      exit 1
    fi
    if [ -f data/crawl.db ]; then
      OPT="${OPTIMIZE_FTS:-false}" "$PY" - <<'PY'
import os, sqlite3
db = sqlite3.connect("data/crawl.db")
db.execute("PRAGMA trusted_schema=OFF")
if os.environ.get("OPT") == "true":
    db.execute("INSERT INTO records_fts(records_fts) VALUES('optimize')")      # weekly: one segment per index level
else:
    db.execute("INSERT INTO records_fts(records_fts, rank) VALUES('merge', 300)")  # nightly: bounded incremental merge
db.commit()
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # fold the write-ahead log into the file before sealing
db.close()
PY
    fi
    mkdir -p data state imports
    "$PY" -m opencrawl vault pack "$out" data state imports \
      --exclude state/http_cache --exclude data/crawl.db-wal --exclude data/crawl.db-shm
    ;;
  seal)
    need_key
    "$PY" -m opencrawl vault pack "${3:?usage: ci_data.sh seal DIR OUT}" "${2}"
    ;;
  open)
    need_key
    "$PY" -m opencrawl vault unpack "${2:?usage: ci_data.sh open IN DEST}" "${3:?usage: ci_data.sh open IN DEST}"
    ;;
  push)
    shift
    [ "$#" -gt 0 ] || { echo "usage: ci_data.sh push FILE..."; exit 2; }
    gh release view "$TAG" >/dev/null 2>&1 || gh release create "$TAG" --title "Crawl data (encrypted)" --prerelease \
      --notes "Encrypted with the repository's OPENCRAWL_DATA_KEY secret (opencrawl vault). Updated by the crawl workflow."
    gh release upload "$TAG" "$@" --clobber
    echo "uploaded: $*"
    ;;
  *) echo "usage: $0 pull | pack OUT | seal DIR OUT | open IN DEST | push FILE..."; exit 2 ;;
esac
