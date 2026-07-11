#!/usr/bin/env bash
# Syncs the shared benchmark page into a gh-pages checkout.
#
# $1        - path to the gh-pages checkout
# $DATA_DIR - directory inside the checkout that holds the benchmark data
set -euo pipefail

CHECKOUT_DIR="$1"
DATA_DIR="${DATA_DIR:-benchmarks}"
ACTION_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="gofiber-benchmark-redirect"

cd "$CHECKOUT_DIR"
mkdir -p "$DATA_DIR"

# The shared page is always overwritten so central changes propagate on the
# next benchmark run of every repository.
cp "$ACTION_DIR/index.html" "$DATA_DIR/index.html"

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

# Make the Pages root point at the benchmarks instead of returning a 404.
write_redirect index.html "./${DATA_DIR}/"

# Multi-folder layout (one data.js per package): refresh folders.json and turn
# the per-package stock pages into stubs that preselect the package filter.
if compgen -G "$DATA_DIR/*/data.js" > /dev/null; then
  find "$DATA_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; \
    | jq -R -s -c 'split("\n") | map(select(length > 0)) | sort' > "$DATA_DIR/folders.json"
  while IFS= read -r pkg; do
    write_redirect "$DATA_DIR/$pkg/index.html" "../#package=$pkg" replace-stock
  done < <(find "$DATA_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \;)
fi

git config --local user.email "github-actions[bot]@users.noreply.github.com"
git config --local user.name "github-actions[bot]"
git add -A -- "$DATA_DIR" index.html
if git diff --staged --quiet; then
  echo "Benchmark page already up to date"
  exit 0
fi
git commit -m "Sync benchmark page"

# Parallel benchmark matrix legs may push to gh-pages at the same time.
for attempt in 1 2 3 4 5; do
  if git push; then
    exit 0
  fi
  echo "Push failed (attempt ${attempt}), rebasing onto remote gh-pages"
  if ! git pull --rebase; then
    # keep retrying on transient errors instead of dying under set -e
    git rebase --abort 2>/dev/null || true
  fi
done
echo "Failed to push the benchmark page after 5 attempts" >&2
exit 1
