#!/usr/bin/env bash
# Move the crawl database between a CI runner and the "crawl-data" release.
#   scripts/ci_db.sh pull   download data/crawl.db (no-op on the very first run)
#   scripts/ci_db.sh push   upload data/crawl.db, creating the release if needed
# zstd (multi-threaded) when the runner has it: several times faster than gzip at a similar size.
# Older releases hold crawl.db.gz; pull reads either.
# OPTIMIZE_FTS=true (the weekly run) rebuilds the full-text index's segments; other nights do a bounded merge.
set -euo pipefail
TAG="${DATA_TAG:-crawl-data}"
mkdir -p data
case "${1:-}" in
  pull)
    if command -v zstd >/dev/null && gh release download "$TAG" -p crawl.db.zst -D data --clobber 2>/dev/null; then
      zstd -d -q -f --rm data/crawl.db.zst -o data/crawl.db
      echo "database: $(du -h data/crawl.db | cut -f1) (zstd)"
    elif gh release download "$TAG" -p crawl.db.gz -D data --clobber 2>/dev/null; then
      gunzip -f data/crawl.db.gz
      echo "database: $(du -h data/crawl.db | cut -f1) (gzip)"
    else
      echo "no database yet (first run)"
    fi
    ;;
  push)
    [ -f data/crawl.db ] || { echo "no database to upload"; exit 0; }
    OPT="${OPTIMIZE_FTS:-false}" python - <<'PY'
import os, sqlite3
db = sqlite3.connect("data/crawl.db")
if os.environ.get("OPT") == "true":
    db.execute("INSERT INTO records_fts(records_fts) VALUES('optimize')")      # weekly: one segment per index level
else:
    db.execute("INSERT INTO records_fts(records_fts, rank) VALUES('merge', 300)")  # nightly: bounded incremental merge
db.commit()
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # fold the write-ahead log into the file before copying
db.close()
PY
    gh release view "$TAG" >/dev/null 2>&1 || gh release create "$TAG" --title "Crawl data" --prerelease \
      --notes "The crawl database (crawl.db.zst) and the export bundle (bundle.tar.gz), updated by the crawl workflow."
    if command -v zstd >/dev/null; then
      zstd -T0 -3 -q -f data/crawl.db -o crawl.db.zst
      gh release upload "$TAG" crawl.db.zst --clobber
      gh release delete-asset "$TAG" crawl.db.gz -y >/dev/null 2>&1 || true   # one copy only
      echo "uploaded $(du -h crawl.db.zst | cut -f1) (zstd)"
    else
      gzip -1 -c data/crawl.db > crawl.db.gz
      gh release upload "$TAG" crawl.db.gz --clobber
      echo "uploaded $(du -h crawl.db.gz | cut -f1) (gzip)"
    fi
    ;;
  *) echo "usage: $0 pull|push"; exit 2 ;;
esac
