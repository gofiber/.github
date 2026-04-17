# Weekly Auto-Release — Maintainer Playbook

## Overview

Every GoFiber repo has a weekly scheduled workflow that automatically publishes draft releases created by `release-drafter`. The process: draft → cleanup (strip emoji, conv-commit prefixes, bot contributors) → publish. One repo per weekday, modules within a repo sequentially with a configurable delay.

## Schedule

| Day | Repo | Type | Cron (UTC) |
|-----|------|------|------------|
| Mon | cli | single | `0 8 * * 1` |
| Tue | utils | single | `0 8 * * 2` |
| Wed | template | multi | `0 8 * * 3` |
| Thu | storage | multi | `0 8 * * 4` |
| Fri | contrib | multi | `0 8 * * 5` |
| Sat | schema | single | `0 8 * * 6` |
| Sun | multi-labeler | single | `0 8 * * 0` |

08:00 UTC = 10:00 CEST (summer) / 09:00 CET (winter).

## How It Works

### Single-module repos (cli, utils, schema, multi-labeler)

1. The existing `release-drafter.yml` workflow keeps a draft release up-to-date on every push to main/master.
2. The weekly `weekly-release.yml` finds the newest draft.
3. Skips if: no draft, draft has no PR entries (`#123` references), or it's a major bump.
4. Runs the cleanup tool (emoji strip, conv-commit prefix strip, bot filter, dedupe).
5. Publishes the draft in one atomic API call (body patch + `draft: false`).

### Multi-module repos (contrib, storage, template)

Same as above, but with **wave ordering** defined in `.github/release-plan.yml`:

1. **Explicit waves** run first — modules that other modules depend on (e.g. `websocket` before `socketio`, `core` before template engines).
2. **Auto-discover wave** (`auto-discover: true`) picks up all remaining drafts that weren't in earlier waves. New packages are found automatically — no manifest change needed.
3. Modules within a wave are processed **sequentially** with a configurable delay (default 2 min) between publishes.

### Release plan manifest

Only multi-module repos need `.github/release-plan.yml`. Only list modules with ordering constraints — everything else is auto-discovered.

```yaml
# Example: gofiber/contrib
repo-type: multi
waves:
  - name: base
    modules:
      - { name: websocket, tag-prefix: "v3/websocket/" }
  - name: remaining
    auto-discover: true
```

```yaml
# Example: gofiber/storage (no constraints)
repo-type: multi
waves:
  - name: backends
    auto-discover: true
```

## Manual Dispatch

Every repo has a "Run workflow" button in the Actions tab with these inputs:

| Input | Description |
|-------|-------------|
| `draft-tags` | Comma-separated filter. Supports full tags (`v2.0.3`) and package names (`websocket`, `redis`). Matched as substrings. Empty = all drafts. |
| `dry-run` | Show cleanup diff without publishing. |
| `delay` | Minutes between module publishes (multi-module only, default 2). |

### Examples

- Publish everything: leave all fields empty, click "Run workflow".
- Publish only redis: `draft-tags: redis`
- Preview what would happen: check `dry-run: true`
- Publish websocket and jwt only: `draft-tags: websocket,jwt`

## Major Version Handling

When the weekly workflow detects a major bump (tag major segment increased OR a non-empty `## Breaking Changes` section in the draft), it:

1. **Does NOT publish** the draft.
2. Creates a tracking issue: `Major release pending: <tag>` with label `release-automation`.
3. Exits cleanly (success, not failure).

**What you do:** Review the draft, edit the release notes as needed, publish manually via the GitHub Releases UI. Close the tracking issue.

On subsequent weekly runs, the workflow sees the open tracking issue and skips that draft — your manual edits are never overwritten.

## Release Notes Cleanup Rules

Applied automatically before publishing:

1. **Emoji strip** — removes leading emoji from bullet lines (`- 🐛 fix crash` → `- fix crash`). Category headings keep their emoji.
2. **Conventional-commit prefix strip** — removes `fix:`, `feat(scope):`, `chore!:` etc. from bullet lines.
3. **Bot contributor filter** — removes bot accounts from the "Thank you @..." footer. A login is considered a bot if it contains `[bot]` or any of these keywords (case-insensitive): `claude`, `copilot`, `codex`, `dependabot`, `github`, `gemini`, `renovate`. If all contributors are bots, the entire "Thank you" line is removed.
4. **Cross-category dedupe** — if the same PR (`#123`) appears in multiple sections, keeps it in the highest-priority section. Priority: Breaking > New > Fixes > Updates > Documentation.

## Adding a New Repo

### Single-module

Add one file: `.github/workflows/weekly-release.yml`

```yaml
name: Weekly release
on:
  schedule:
    - cron: '0 8 * * N'  # pick a free day
  workflow_dispatch:
    inputs:
      draft-tags: { type: string, default: '' }
      dry-run: { type: boolean, default: false }
concurrency:
  group: weekly-release-${{ github.repository }}
  cancel-in-progress: false
permissions:
  contents: write
  pull-requests: read
  issues: write
jobs:
  release:
    uses: gofiber/.github/.github/workflows/weekly-release.yml@main
    with:
      repo-type: single
      dry-run: ${{ inputs.dry-run || false }}
      draft-tags: ${{ inputs.draft-tags || '' }}
```

Prerequisite: the repo must have a working `release-drafter.yml` workflow that creates draft releases.

### Multi-module

Same caller workflow as above but with `repo-type: multi` and an extra `delay` dispatch input. Plus a `.github/release-plan.yml`:

```yaml
repo-type: multi
waves:
  # List modules that must be released before others.
  - name: base
    modules:
      - { name: dependency-pkg, tag-prefix: "prefix/" }
  # Everything else is auto-discovered.
  - name: remaining
    auto-discover: true
```

## Pausing Releases

- **One repo, one week:** Disable the workflow in the Actions tab → Settings → disable.
- **One module:** Add the module's tag to a `Major release pending: <tag>` issue (open, label `release-automation`) — the workflow skips it.
- **All repos:** Disable each workflow individually, or push a change to the central `weekly-release.yml` that short-circuits early.

## Troubleshooting

### "No draft release found"
The legacy `release-drafter.yml` hasn't run since the last publish. Either no PRs were merged, or the workflow is broken. Check the repo's Actions tab for the "Release Drafter" workflow.

### "Draft has no PR entries — skipped"
The draft exists but contains no `#123` references or `/pull/` links. This happens when release-drafter creates a placeholder before any PRs are merged. Normal — the draft will be picked up next week after PRs land.

### "Major bump — skipped"
The resolved tag has a higher major version than the last published release, or the draft body contains a non-empty Breaking Changes section. Intentional — review and publish manually.

### "FAILED to publish"
The `gh api --method PATCH` call failed. Common causes:
- **403 Forbidden:** The `GITHUB_TOKEN` doesn't have `contents: write`. Check the caller workflow's `permissions:` block.
- **404 Not Found:** The draft was deleted or published between the lookup and the PATCH. Re-run the workflow.
- **422 Unprocessable Entity:** The tag already exists as a non-draft release. Investigate manually.

### Multi-module: "no draft (prefix: ...)"
The release-drafter hasn't created a draft for that module. Either no PRs touched that module's directory since the last release, or the release-drafter config doesn't include a path filter for it.

### Timeout
The default job timeout is 360 min (6h). With 2 min delay: up to 180 modules fit. If you hit the limit, reduce the delay via the `delay` dispatch input.

## Architecture

```
gofiber/.github/                          (central)
├── .github/
│   ├── workflows/weekly-release.yml      (reusable orchestrator)
│   └── actions/clean-release-notes/      (composite action + Go cleanup tool)
│       ├── action.yml
│       ├── go.mod, main.go
│       └── cleanup/
│           ├── cleanup.go                (parse, emoji, prefix, bot, dedupe)
│           └── cleanup_test.go

gofiber/<repo>/                           (per repo)
├── .github/
│   ├── workflows/weekly-release.yml      (thin caller, ~35 lines)
│   ├── release-plan.yml                  (multi-module only)
│   ├── release-drafter.yml               (config, already exists)
│   └── workflows/release-drafter.yml     (legacy drafter, already exists)
```

All heavy logic lives in `gofiber/.github`. Repo-specific files are minimal and rarely need updates.
