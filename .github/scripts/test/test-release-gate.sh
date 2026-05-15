#!/usr/bin/env bash
# Tests for release-gate.sh
#
# Stubs `gh` on PATH so classify/filter/weeks tests run hermetically.
# Run from anywhere: bash .github/scripts/test/test-release-gate.sh

set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
GATE="$SCRIPT_DIR/../release-gate.sh"

if [ ! -f "$GATE" ]; then
  echo "FATAL: release-gate.sh not found at $GATE"
  exit 2
fi

SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

PATH="$SANDBOX:$PATH"
export PATH
export GH_REPO=test/repo
export RUNNER_TEMP="$SANDBOX"

# Stub gh. Reads PR / release fixtures from files in $RUNNER_TEMP.
# pr_db   format: <pr_num>\t<branch>\t<author>
# rel_db  format: <tag>\t<status>\t<published_at>
#   status: ok | missing | error
#
# Strict mode: when a *_db file exists but the lookup misses, the
# stub exits non-zero with a stderr message. This catches fixture
# typos where a test author registered the wrong PR/tag number.
# Tests that don't register any fixture (no _db file) get a safe
# default that mimics a non-bot human PR / "release not found".
#
# Argument shape validation: the stub asserts that the call carries
# both --json and --jq, since release-gate.sh always uses them. This
# catches future drift between the script and the test stub.
cat > "$SANDBOX/gh" << 'STUB'
#!/usr/bin/env bash
case "$1 $2" in
  "pr view")
    pr_num="$3"
    # Validate expected flags are present
    if ! printf '%s' "$*" | grep -q -- '--json headRefName,author'; then
      printf 'gh stub: pr view missing --json headRefName,author flag (args: %s)\n' "$*" >&2
      exit 5
    fi
    if ! printf '%s' "$*" | grep -q -- '--jq'; then
      printf 'gh stub: pr view missing --jq flag (args: %s)\n' "$*" >&2
      exit 5
    fi
    db="$RUNNER_TEMP/pr_db"
    if [ -f "$db" ]; then
      while IFS=$'\t' read -r num branch author; do
        if [ "$num" = "$pr_num" ]; then
          printf '%s\t%s\n' "$branch" "$author"
          exit 0
        fi
      done < "$db"
      printf 'gh stub: PR #%s not in fixture db (strict mode)\n' "$pr_num" >&2
      exit 4
    fi
    # No fixture file: default to non-bot human PR
    printf 'feature/x\thuman-author\n'
    ;;
  "release view")
    tag="$3"
    if ! printf '%s' "$*" | grep -q -- '--json publishedAt'; then
      printf 'gh stub: release view missing --json publishedAt (args: %s)\n' "$*" >&2
      exit 5
    fi
    db="$RUNNER_TEMP/rel_db"
    if [ -f "$db" ]; then
      while IFS=$'\t' read -r dbtag status published; do
        if [ "$dbtag" = "$tag" ]; then
          case "$status" in
            ok)               printf '%s' "$published"; exit 0;;
            missing-classic)  printf "release not found\n" >&2; exit 1;;
            missing-tagged)   printf "release with tag '%s' not found\n" "$tag" >&2; exit 1;;
            missing-graphql)  printf "Could not resolve to a Release with the tag '%s'\n" "$tag" >&2; exit 1;;
            http404)          printf "HTTP 404: Not Found\n" >&2; exit 1;;
            error)            printf 'HTTP 502 bad gateway\n' >&2; exit 1;;
            *)
              printf 'gh stub: unknown status %s for tag %s\n' "$status" "$tag" >&2
              exit 5
              ;;
          esac
        fi
      done < "$db"
      printf 'gh stub: tag %s not in fixture db (strict mode)\n' "$tag" >&2
      exit 4
    fi
    # No fixture file: default to "not found"
    printf 'release with tag %s not found\n' "$tag" >&2
    exit 1
    ;;
  "release list")
    printf 'v1.0.0\n'
    ;;
  *)
    printf 'gh stub: unsupported call: %s\n' "$*" >&2
    exit 2
    ;;
esac
STUB
chmod +x "$SANDBOX/gh"

# Source the helpers under test
# shellcheck disable=SC1090
source "$GATE"

# ── Test framework ───────────────────────────────────────────────────
TEST=0; PASS=0; FAIL=0
expect_eq() {
  local label="$1" expected="$2" actual="$3"
  TEST=$((TEST + 1))
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
    printf 'ok %d - %s\n' "$TEST" "$label"
  else
    FAIL=$((FAIL + 1))
    printf 'not ok %d - %s\n' "$TEST" "$label"
    printf '  expected: %s\n  got:      %s\n' "$expected" "$actual"
  fi
}
expect_true() {
  local label="$1" rc="$2"
  TEST=$((TEST + 1))
  if [ "$rc" -eq 0 ]; then
    PASS=$((PASS + 1)); printf 'ok %d - %s\n' "$TEST" "$label"
  else
    FAIL=$((FAIL + 1)); printf 'not ok %d - %s (rc=%d)\n' "$TEST" "$label" "$rc"
  fi
}
expect_false() {
  local label="$1" rc="$2"
  TEST=$((TEST + 1))
  if [ "$rc" -ne 0 ]; then
    PASS=$((PASS + 1)); printf 'ok %d - %s\n' "$TEST" "$label"
  else
    FAIL=$((FAIL + 1)); printf 'not ok %d - %s (rc=0, expected nonzero)\n' "$TEST" "$label"
  fi
}

# ── Fixture helpers ──────────────────────────────────────────────────
set_pr()  { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$SANDBOX/pr_db"; }
set_rel() { printf '%s\tok\t%s\n' "$1" "$2" >> "$SANDBOX/rel_db"; }
set_rel_status() { printf '%s\t%s\t\n' "$1" "$2" >> "$SANDBOX/rel_db"; }
reset_fixtures() { rm -f "$SANDBOX/pr_db" "$SANDBOX/rel_db"; }

# Run classify_draft_priority in a subshell with optional env overrides.
# Usage: classify_run "<body>" [VAR=val ...]
classify_run() {
  local body="$1"; shift
  (
    for kv in "$@"; do export "$kv"; done
    printf '%s' "$body" | classify_draft_priority
  )
}

# ── classify_draft_priority ──────────────────────────────────────────
printf '# classify_draft_priority\n'

reset_fixtures
expect_eq "empty body" "empty" "$(classify_run '')"

reset_fixtures
expect_eq "features only" "has-changes" \
  "$(classify_run "$(printf '## Features\n- New API (#10)\n')")"

reset_fixtures
set_pr 1 'dependabot/github_actions/checkout' 'dependabot[bot]'
set_pr 2 'dependabot/github_actions/setup-go' 'dependabot[bot]'
expect_eq "only dependabot github_actions" "empty" \
  "$(classify_run "$(printf '## Maintenance\n- Bump (#1)\n- Bump (#2)\n')")"

reset_fixtures
set_pr 1 'github_actions/manual-thing' 'human-author'
expect_eq "github_actions branch but non-dependabot author" "low-only" \
  "$(classify_run "$(printf '## Maintenance\n- Manual (#1)\n')")"

reset_fixtures
set_pr 1 'dependabot/npm_and_yarn/foo' 'dependabot[bot]'
expect_eq "dependabot but not github_actions branch" "low-only" \
  "$(classify_run "$(printf '## Maintenance\n- Bump npm dep (#1)\n')")"

reset_fixtures
set_pr 1 'dependabot/github_actions/checkout' 'dependabot[bot]'
set_pr 3 'feature/x' 'human-author'
expect_eq "mix github_actions + real maintenance" "low-only" \
  "$(classify_run "$(printf '## Maintenance\n- Bump (#1)\n- Real (#3)\n')")"

reset_fixtures
set_pr 1 'dependabot/github_actions/checkout' 'dependabot[bot]'
expect_eq "github_actions + features" "has-changes" \
  "$(classify_run "$(printf '## Maintenance\n- Bump (#1)\n## Features\n- API (#7)\n')")"

reset_fixtures
expect_eq "emoji-prefixed maintenance header" "low-only" \
  "$(classify_run "$(printf '## 🧹 Maintenance\n- Real (#9)\n')")"

reset_fixtures
expect_eq "documentation only" "low-only" \
  "$(classify_run "$(printf '## Documentation\n- Fix typo (#5)\n')")"

reset_fixtures
set_pr 50 'dependabot/github_actions/checkout' 'dependabot[bot]'
expect_eq "/pull/ URL form filtered" "empty" \
  "$(classify_run "$(printf '## Maintenance\n- Bump https://github.com/x/y/pull/50\n')")"

reset_fixtures
expect_eq "bullet without PR reference is ignored" "empty" \
  "$(classify_run "$(printf '## Features\n- No PR reference here\n')")"

reset_fixtures
expect_eq "configurable LOW_PRIORITY_SECTIONS: only Chores low" "low-only" \
  "$(classify_run "$(printf '## Chores\n- Tidy (#99)\n')" "LOW_PRIORITY_SECTIONS=Chores")"

reset_fixtures
expect_eq "configurable LOW_PRIORITY_SECTIONS: Maintenance now high" "has-changes" \
  "$(classify_run "$(printf '## Maintenance\n- Bump (#1)\n')" "LOW_PRIORITY_SECTIONS=Chores")"

reset_fixtures
expect_eq "empty LOW_PRIORITY_SECTIONS falls back to default" "low-only" \
  "$(classify_run "$(printf '## Maintenance\n- Real (#3)\n')" "LOW_PRIORITY_SECTIONS=")"

# Edge cases for header / bullet patterns
reset_fixtures
expect_eq "headings only, no bullets" "empty" \
  "$(classify_run "$(printf '## Features\n## Maintenance\n## Documentation\n')")"

reset_fixtures
expect_eq "mixed case heading (## MAINTENANCE)" "low-only" \
  "$(classify_run "$(printf '## MAINTENANCE\n- Bump (#3)\n')")"

reset_fixtures
set_pr 5 'feature/y' 'rene'
expect_eq "same PR in two sections, one high wins" "has-changes" \
  "$(classify_run "$(printf '## Maintenance\n- Bump (#5)\n## Features\n- Same (#5)\n')")"

# ── is_dependabot_gha_pr (direct) ────────────────────────────────────
printf '# is_dependabot_gha_pr\n'

reset_fixtures
set_pr 1 'dependabot/github_actions/checkout' 'dependabot[bot]'
set_pr 2 'github_actions/manual'              'human-author'
set_pr 3 'dependabot/npm_and_yarn/x'          'dependabot[bot]'
set_pr 4 'feature/y'                          'human-author'

is_dependabot_gha_pr "- Bump (#1)"; expect_true  "dependabot + github_actions => true" $?
is_dependabot_gha_pr "- Manual (#2)"; expect_false "github_actions branch, non-dependabot => false" $?
is_dependabot_gha_pr "- npm bump (#3)"; expect_false "dependabot, non-github_actions branch => false" $?
is_dependabot_gha_pr "- regular (#4)"; expect_false "regular human PR => false" $?
is_dependabot_gha_pr "- no number here"; expect_false "no PR number => false" $?

# ── weeks_since_release ──────────────────────────────────────────────
printf '# weeks_since_release\n'

reset_fixtures
expect_eq "empty tag returns 999" "999" "$(weeks_since_release '')"

# Each gh CLI 404 phrasing should map to 999 (first-release-friendly)
reset_fixtures
set_rel_status 'v9.9.9' 'missing-classic'
expect_eq "phrasing 'release not found' => 999" "999" "$(weeks_since_release 'v9.9.9')"

reset_fixtures
set_rel_status 'v9.9.9' 'missing-tagged'
expect_eq "phrasing 'release with tag X not found' => 999" "999" "$(weeks_since_release 'v9.9.9')"

reset_fixtures
set_rel_status 'v9.9.9' 'missing-graphql'
expect_eq "phrasing 'Could not resolve to a Release' => 999" "999" "$(weeks_since_release 'v9.9.9')"

reset_fixtures
set_rel_status 'v9.9.9' 'http404'
expect_eq "phrasing 'HTTP 404' => 999" "999" "$(weeks_since_release 'v9.9.9')"

reset_fixtures
set_rel_status 'v1.0.0' 'error'
result=$(weeks_since_release 'v1.0.0' 2>/dev/null)
expect_eq "transient API error => 0 (fail-safe hold)" "0" "$result"

reset_fixtures
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HAS_GNU_DATE=$(date -u -d "$NOW" +%s 2>/dev/null && echo yes || echo no)
if [ "$HAS_GNU_DATE" != "no" ]; then
  set_rel 'v2.0.0' "$NOW"
  expect_eq "freshly published => 0 weeks" "0" "$(weeks_since_release 'v2.0.0')"
else
  printf '# SKIP: GNU date -d not available, skipping freshly-published test\n'
fi

reset_fixtures
PAST_30D=$(date -u -d '30 days ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")
if [ -n "$PAST_30D" ]; then
  set_rel 'v2.1.0' "$PAST_30D"
  expect_eq "30 days ago => 4 weeks" "4" "$(weeks_since_release 'v2.1.0')"
else
  printf '# SKIP: GNU date -d not available, skipping 30-day test\n'
fi

# Tag with prefix (multi-module style)
if [ "$HAS_GNU_DATE" != "no" ]; then
  reset_fixtures
  set_rel 'websocket/v1.0.0' "$NOW"
  expect_eq "prefixed tag (multi-module) => 0 weeks" "0" "$(weeks_since_release 'websocket/v1.0.0')"
fi

# ── detect_major_bump ────────────────────────────────────────────────
printf '# detect_major_bump\n'

dmb_result() {
  detect_major_bump "$1" "$2" "$3" | cut -f1
}
dmb_reason() {
  detect_major_bump "$1" "$2" "$3" | cut -f2-
}

# No prev tag and no breaking section -> false
EMPTY_BODY="$SANDBOX/empty_body.md"
: > "$EMPTY_BODY"
expect_eq "no prev tag, no body => false" "false" "$(dmb_result '' 'v1.0.0' "$EMPTY_BODY")"

# Numeric major bump on single-module tag
expect_eq "v1.2.3 -> v2.0.0 is major" "true" "$(dmb_result 'v1.2.3' 'v2.0.0' "$EMPTY_BODY")"

# Same major, no breaking -> not major
expect_eq "v1.2.3 -> v1.3.0 is not major" "false" "$(dmb_result 'v1.2.3' 'v1.3.0' "$EMPTY_BODY")"

# Prefixed (multi-module) tag bump
expect_eq "websocket/v2.3.1 -> websocket/v3.0.0 is major" "true" \
  "$(dmb_result 'websocket/v2.3.1' 'websocket/v3.0.0' "$EMPTY_BODY")"
expect_eq "websocket/v2.3.1 -> websocket/v2.4.0 is not major" "false" \
  "$(dmb_result 'websocket/v2.3.1' 'websocket/v2.4.0' "$EMPTY_BODY")"

# Double-prefixed tag (e.g. v3/websocket/v1.2.0) uses LAST vN
expect_eq "v3/websocket/v1.2.0 -> v3/websocket/v2.0.0 is major" "true" \
  "$(dmb_result 'v3/websocket/v1.2.0' 'v3/websocket/v2.0.0' "$EMPTY_BODY")"
expect_eq "v3/websocket/v1.2.0 -> v3/websocket/v1.3.0 is not major" "false" \
  "$(dmb_result 'v3/websocket/v1.2.0' 'v3/websocket/v1.3.0' "$EMPTY_BODY")"

# Two-digit major: v9 -> v10
expect_eq "v9.0.0 -> v10.0.0 is major (two-digit compare)" "true" \
  "$(dmb_result 'v9.0.0' 'v10.0.0' "$EMPTY_BODY")"

# Breaking Changes section in body triggers major even on minor bump
BREAKING_BODY="$SANDBOX/breaking_body.md"
printf '## Features\n- something (#1)\n## Breaking Changes\n- removed API (#2)\n' > "$BREAKING_BODY"
expect_eq "minor bump but Breaking Changes section => major" "true" \
  "$(dmb_result 'v1.2.3' 'v1.3.0' "$BREAKING_BODY")"

# Empty Breaking Changes section (heading present but no bullets) does NOT trigger
EMPTY_BREAKING_BODY="$SANDBOX/empty_breaking_body.md"
printf '## Breaking Changes\n## Features\n- something (#1)\n' > "$EMPTY_BREAKING_BODY"
expect_eq "empty Breaking Changes heading => not major" "false" \
  "$(dmb_result 'v1.2.3' 'v1.3.0' "$EMPTY_BREAKING_BODY")"

# Reason text on major
reason=$(detect_major_bump 'v1.2.3' 'v2.0.0' "$EMPTY_BODY" | cut -f2-)
printf '%s' "$reason" | grep -q 'major segment bumped'
expect_true "major reason contains 'major segment bumped'" $?

reason=$(detect_major_bump 'v1.2.3' 'v1.3.0' "$BREAKING_BODY" | cut -f2-)
printf '%s' "$reason" | grep -q 'Breaking Changes'
expect_true "breaking reason mentions Breaking Changes" $?

# Summary
printf '\n1..%d\n' "$TEST"
printf '%d tests, %d passed, %d failed\n' "$TEST" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
