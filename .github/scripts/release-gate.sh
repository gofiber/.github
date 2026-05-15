#!/usr/bin/env bash
# release-gate.sh
#
# Shared helper functions used by the weekly auto-release workflow
# (single-release + multi-release jobs) to decide whether a draft
# release should be published this run.
#
# This file is the single source of truth for:
#   - classify_draft_priority: classify a draft body as empty/low-only/has-changes
#   - is_dependabot_gha_pr:    detect Dependabot CI-workflow dependency bumps
#   - weeks_since_release:     compute weeks elapsed since a prev tag's publish
#   - detect_major_bump:       detect major-version bumps (semver + Breaking section)
#
# Sourced by both jobs of weekly-release.yml. Tests live under
# .github/scripts/test/test-release-gate.sh and run in CI.
#
# Requires: bash 4+, GNU date, jq, gh CLI authenticated.
# Reads env: GH_REPO, LOW_PRIORITY_SECTIONS (optional, default
# "Maintenance,Documentation"), RUNNER_TEMP (optional, default /tmp).

# is_dependabot_gha_pr: returns 0 if the bullet line references a PR
# opened by Dependabot for a github_actions workflow dependency bump
# (i.e. branch contains "github_actions" AND author contains
# "dependabot"). These bullets are filtered out by
# classify_draft_priority so a draft made up of nothing but CI bumps
# never triggers a release. Callers should pre-filter to bullet
# lines via the same regex used in classify_draft_priority.
is_dependabot_gha_pr() {
  local line="$1"
  local pr_num
  pr_num=$(printf '%s' "$line" | grep -oE '#[0-9]+' | head -1 | tr -d '#')
  if [ -z "$pr_num" ]; then
    pr_num=$(printf '%s' "$line" | grep -oE '/pull/[0-9]+' | head -1 | sed 's|/pull/||')
  fi
  [ -z "$pr_num" ] && return 1
  local meta branch author
  meta=$(gh pr view "$pr_num" --repo "$GH_REPO" --json headRefName,author \
           --jq '[.headRefName, .author.login] | @tsv' 2>/dev/null || echo "")
  branch=$(printf '%s' "$meta" | cut -f1)
  author=$(printf '%s' "$meta" | cut -f2)
  [[ "$branch" == *github_actions* ]] && [[ "$author" == *dependabot* ]]
}

# classify_draft_priority: reads a release body from stdin and prints
# one of:
#   empty       - no actionable bullets after filtering
#   low-only    - bullets only in low-priority sections
#   has-changes - at least one bullet in another section
#                 (features, fixes, updates, breaking, ...)
#
# Headings are matched case-insensitively as a substring against the
# keywords in $LOW_PRIORITY_SECTIONS (default "Maintenance,
# Documentation"), so emoji-prefixed headers like "## 🧹 Maintenance"
# work. Bullets in low-priority sections that reference a Dependabot
# github_actions PR are filtered out (see is_dependabot_gha_pr).
#
# All headings that do NOT match a low-priority keyword count as
# high-priority, so new section names added to release-drafter never
# need to be listed explicitly.
#
# Optimization: once one confirmed real low-priority bullet has been
# seen, subsequent low-priority bullets are not API-checked, since
# the result is already pinned to low-only (unless a high-priority
# bullet appears later, in which case the answer flips to
# has-changes regardless).
classify_draft_priority() {
  local section=""
  local has_low=false
  local has_high=false
  local keywords_raw="${LOW_PRIORITY_SECTIONS:-Maintenance,Documentation}"
  local -a raw_keys=()
  local -a low_keys=()
  local _k _kk
  IFS=',' read -ra raw_keys <<< "$keywords_raw"
  for _k in "${raw_keys[@]}"; do
    _k="${_k#"${_k%%[![:space:]]*}"}"
    _k="${_k%"${_k##*[![:space:]]}"}"
    [ -n "$_k" ] && low_keys+=("${_k,,}")
  done
  [ "${#low_keys[@]}" -eq 0 ] && low_keys=(maintenance documentation)
  local line
  # `|| [ -n "$line" ]` ensures the last line is processed even when
  # the input has no trailing newline (`read` returns 1 at EOF).
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^##\  ]]; then
      local header_lc="${line,,}"
      section="other"
      for _kk in "${low_keys[@]}"; do
        if [[ "$header_lc" == *"$_kk"* ]]; then
          section="low"
          break
        fi
      done
      continue
    fi
    if [[ "$line" =~ ^-\ .*(#[0-9]+|/pull/[0-9]+|/issues?/[0-9]+) ]]; then
      case "$section" in
        low)
          if [ "$has_low" = "false" ] && is_dependabot_gha_pr "$line"; then
            continue
          fi
          has_low=true
          ;;
        other)
          has_high=true
          ;;
      esac
    fi
  done
  if [ "$has_high" = "true" ]; then
    echo "has-changes"
  elif [ "$has_low" = "true" ]; then
    echo "low-only"
  else
    echo "empty"
  fi
}

# weeks_since_release: prints integer weeks since the release with the
# given tag was published. Distinguishes between:
#   tag empty / not found  -> 999  (first-ever release: cooldown does not block)
#   gh API failed otherwise -> 0    (fail-safe: hold release, retry next run)
#                                    plus a ::warning:: annotation
#   success                 -> integer weeks, clamped to >= 0
#
# The 404 detection regex is intentionally permissive because gh CLI
# emits several phrasings: "release with tag 'X' not found",
# "Not Found (HTTP 404)", "Could not resolve to a Release", etc.
#
# Requires GNU date (Ubuntu runners only).
weeks_since_release() {
  local t="$1"
  [ -z "$t" ] && { echo "999"; return; }
  local errf pub rc stderr_out=""
  errf=$(mktemp "${RUNNER_TEMP:-/tmp}/ghrv.XXXXXX")
  pub=$(gh release view "$t" --repo "$GH_REPO" --json publishedAt \
          --jq '.publishedAt // ""' 2>"$errf")
  rc=$?
  [ -s "$errf" ] && stderr_out=$(cat "$errf")
  rm -f "$errf"
  if [ "$rc" -ne 0 ]; then
    if printf '%s' "$stderr_out" | grep -qiE 'not found|does not exist|could not resolve|HTTP[/ ]?404'; then
      echo "999"; return
    fi
    echo "::warning::weeks_since_release: gh release view '${t}' failed: ${stderr_out}" >&2
    echo "0"; return
  fi
  [ -z "$pub" ] && { echo "999"; return; }
  local pub_ts now_ts weeks
  pub_ts=$(date -u -d "$pub" +%s 2>/dev/null || echo 0)
  now_ts=$(date -u +%s)
  [ "$pub_ts" -eq 0 ] && { echo "999"; return; }
  weeks=$(( (now_ts - pub_ts) / 604800 ))
  [ "$weeks" -lt 0 ] && weeks=0
  echo "$weeks"
}

# detect_major_bump: classifies a release as a major bump based on
# (a) numeric major-segment increase between prev_tag and next_tag
#     (handles both v2.3.1 and prefixed forms like websocket/v2.3.1
#      or v3/websocket/v1.2.0 - the LAST "vN" segment is the package
#      semver major).
# (b) a non-empty "Breaking Changes" section in the draft body file.
#
# Output: a single tab-separated line: <is_major>\t<reason>
#   is_major: "true" | "false"
#   reason:   human-readable explanation; empty when is_major=false
#
# Usage:
#   result=$(detect_major_bump "$prev_tag" "$next_tag" "$body_file")
#   is_major=$(printf '%s' "$result" | cut -f1)
#   reason=$(printf '%s' "$result" | cut -f2-)
detect_major_bump() {
  local prev_tag="$1" next_tag="$2" body_file="$3"
  if [ -n "$prev_tag" ]; then
    local pv nv
    pv=$(printf '%s' "$prev_tag" | grep -oE 'v[0-9]+' | tail -1 | tr -d 'v')
    nv=$(printf '%s' "$next_tag" | grep -oE 'v[0-9]+' | tail -1 | tr -d 'v')
    if [ -n "$nv" ] && [ -n "$pv" ] && [ "$nv" -gt "$pv" ] 2>/dev/null; then
      printf 'true\tmajor segment bumped: %s -> %s\n' "$prev_tag" "$next_tag"
      return
    fi
  fi
  if [ -n "$body_file" ] && [ -f "$body_file" ]; then
    if awk '/^## .*[Bb]reaking/{f=1;next}/^## /{f=0}f&&/^- /{print;exit}' "$body_file" | grep -q .; then
      printf 'true\tdraft contains a non-empty Breaking Changes section\n'
      return
    fi
  fi
  printf 'false\t\n'
}
