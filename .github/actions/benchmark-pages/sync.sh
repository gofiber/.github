#!/usr/bin/env bash
# Publishes a benchmark run into the gh-pages data (optional) and syncs the
# shared benchmark page into a gh-pages checkout, as one commit.
#
# $1                     - path to the gh-pages checkout
# $DATA_DIR              - directory inside the checkout that holds the benchmark data
# $OUTPUT_FILE           - raw `go test -bench` output to publish; empty = sync only
# $MAX_ITEMS             - runs to keep in the data (required when publishing)
# $FORCE_PACKAGE_SUFFIX  - always append the Go package to the series names
# $TIMINGS_FILE          - shard planner wall times, stored as shard-timings.txt; empty = skip
# $SYNC_PAGE             - copy the shared page; storage's matrix legs pass false
set -euo pipefail

CHECKOUT_DIR="$1"
DATA_DIR="${DATA_DIR:-benchmarks}"
OUTPUT_FILE="${OUTPUT_FILE:-}"
TIMINGS_FILE="${TIMINGS_FILE:-}"
SYNC_PAGE="${SYNC_PAGE:-true}"
ACTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="gofiber-benchmark-redirect"

# Commit metadata comes from the repository checkout the benchmarks ran on,
# gathered before stepping into the gh-pages checkout.
COMMIT_ID=""
if [[ -n "$OUTPUT_FILE" ]]; then
  [[ "$OUTPUT_FILE" = /* ]] || OUTPUT_FILE="$PWD/$OUTPUT_FILE"
  COMMIT_ID="$(git log -1 --format=%H)"
  COMMIT_TS="$(git log -1 --format=%cI)"
  COMMIT_MSG="$(git log -1 --format=%s)"
  REPO_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}"
fi
if [[ -n "$TIMINGS_FILE" ]]; then
  [[ "$TIMINGS_FILE" = /* ]] || TIMINGS_FILE="$PWD/$TIMINGS_FILE"
fi

cd "$CHECKOUT_DIR"
mkdir -p "$DATA_DIR"

if [[ -n "$OUTPUT_FILE" ]]; then
  python3 "$ACTION_DIR/publish.py" \
    --data "$DATA_DIR/data.js" \
    --output "$OUTPUT_FILE" \
    --max-items "${MAX_ITEMS:?MAX_ITEMS is required to publish}" \
    --repo-url "$REPO_URL" \
    --commit-id "$COMMIT_ID" \
    --commit-timestamp "$COMMIT_TS" \
    --commit-message "$COMMIT_MSG" \
    --cpu "${CPU_MODEL:-}" \
    --force-package-suffix "${FORCE_PACKAGE_SUFFIX:-false}"
fi

# the shard planner reads this via the raw gh-pages URL before the next run
if [[ -n "$TIMINGS_FILE" && -s "$TIMINGS_FILE" ]]; then
  cp "$TIMINGS_FILE" "$DATA_DIR/shard-timings.txt"
fi

# Writes a redirect page, but never clobbers a hand-crafted file: the target
# is only (re)written when it is missing, carries our marker, or - when
# allowed via $3 - is a stock github-action-benchmark page.
write_redirect() {
  local file="$1" target="$2" replace_stock="${3:-no}"
  if [[ -f "$file" ]] && ! grep -q "$MARKER" "$file"; then
    if [[ "$replace_stock" != "replace-stock" ]] || ! grep -q 'github-action-benchmark' "$file"; then
      return 0
    fi
  fi
  printf '<!DOCTYPE html>\n<!-- %s -->\n<html lang="en"><head><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=%s"><title>Benchmarks</title></head><body><a href="%s">Benchmarks</a></body></html>\n' \
    "$MARKER" "$target" "$target" > "$file"
}

if [[ "$SYNC_PAGE" == "true" ]]; then
  # One data.js per package (storage) or a single data.js next to the page?
  if compgen -G "$DATA_DIR/*/data.js" > /dev/null; then
    LAYOUT=multi
  else
    LAYOUT=single
  fi

  # A hash over the data busts the Pages CDN cache: the page and its data are
  # republished together, so a fresh page never renders a cached stale data.js.
  DATA_HASH=""
  if compgen -G "$DATA_DIR/data.js" > /dev/null || compgen -G "$DATA_DIR/*/data.js" > /dev/null; then
    DATA_HASH="$(find "$DATA_DIR" -maxdepth 2 -name data.js | sort | xargs cat | git hash-object --stdin | cut -c1-12)"
  fi

  # The shared page is always overwritten so central changes propagate on the
  # next benchmark run of every repository. Baking the layout in spares the page
  # a probing folders.json request that logs a 404 on single-layout repos.
  cp "$ACTION_DIR/index.html" "$DATA_DIR/index.html"
  sed -i.sync-bak "s/<body data-layout=\"auto\" data-version=\"\">/<body data-layout=\"$LAYOUT\" data-version=\"$DATA_HASH\">/" \
    "$DATA_DIR/index.html"
  rm -f "$DATA_DIR/index.html.sync-bak"

  # Make the Pages root point at the benchmarks instead of returning a 404.
  write_redirect index.html "./${DATA_DIR}/"

  # Multi-folder layout (one data.js per package): refresh folders.json and turn
  # the per-package stock pages into stubs that preselect the package filter.
  # folders.json maps folder -> hash of ITS data.js, so the page only re-fetches
  # the packages whose data actually changed; the global body hash merely busts
  # folders.json itself.
  if [[ "$LAYOUT" == multi ]]; then
    while IFS= read -r pkg; do
      hash=""
      if [[ -f "$DATA_DIR/$pkg/data.js" ]]; then
        hash="$(git hash-object "$DATA_DIR/$pkg/data.js" | cut -c1-12)"
      fi
      printf '%s\t%s\n' "$pkg" "$hash"
    done < <(find "$DATA_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort) \
      | jq -R -s -c 'split("\n") | map(select(length > 0) | split("\t") | {(.[0]): (.[1] // "")}) | add // {}' \
      > "$DATA_DIR/folders.json"
    while IFS= read -r pkg; do
      write_redirect "$DATA_DIR/$pkg/index.html" "../#package=$pkg" replace-stock
    done < <(find "$DATA_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \;)
  fi
fi

git config --local user.email "github-actions[bot]@users.noreply.github.com"
git config --local user.name "github-actions[bot]"
git add -A -- "$DATA_DIR"
# the root redirect only exists on page syncs, and git add errors on a pathspec
# that matches nothing
if [[ -f index.html ]]; then
  git add -A -- index.html
fi
if git diff --staged --quiet; then
  echo "Benchmark page already up to date"
  exit 0
fi
if [[ -n "$COMMIT_ID" ]]; then
  git commit -m "Update benchmark data for ${COMMIT_ID:0:7}"
else
  git commit -m "Sync benchmark page"
fi

# Parallel benchmark matrix legs may push to gh-pages at the same time (storage
# finishes 33 of them within a minute); the jittered backoff spreads the herd so
# the retries stop colliding with each other, which 5 immediate ones did.
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if git push; then
    exit 0
  fi
  echo "Push failed (attempt ${attempt}), rebasing onto remote gh-pages"
  sleep "$((RANDOM % 8 + attempt))"
  if ! git pull --rebase; then
    # keep retrying on transient errors instead of dying under set -e
    git rebase --abort 2>/dev/null || true
  fi
done
echo "Failed to push the benchmark page after 10 attempts" >&2
exit 1
