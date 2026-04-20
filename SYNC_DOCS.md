# Sync Docs — Maintainer Playbook

## Overview

All GoFiber repos sync their documentation to the central [gofiber/docs](https://github.com/gofiber/docs) repository. The sync is handled by a single reusable workflow and a central script — per-repo workflow files are thin callers (~25 lines) that pass repo-specific configuration.

## Architecture

```
gofiber/.github/                              (central)
├── .github/
│   ├── workflows/sync-docs.yml               (reusable workflow)
│   └── scripts/sync_docs.sh                  (central sync script)

gofiber/<repo>/                               (per repo)
└── .github/
    └── workflows/sync-docs.yml               (thin caller, ~25 lines)
```

All logic lives in `gofiber/.github`. Repo-specific callers only define configuration (source path, destination path, version file, etc.).

## Modes

| Mode | Description | When |
|------|-------------|------|
| `push` | rsync current docs from source to docs repo | Push to main with doc changes |
| `release` | Create Docusaurus version snapshot for one tag | Release published |
| `release-all` | Fetch all latest releases, create version snapshots for each | **Manual dispatch** |

### `push` mode

Copies documentation files from the source repo to the docs repo using `rsync --delete`. Only files matching the configured patterns are synced (default: `*.md`). Handles additions, modifications, and deletions.

### `release` mode

Creates a versioned documentation snapshot using Docusaurus. The version identifier is computed from the release tag:

| Repo type | Tag format | Version identifier |
|-----------|------------|-------------------|
| single (fiber) | `v3.0.0` | `3.x` |
| multi (storage) | `redis/v3.1.0` | `redis_3.x.x` |
| multi (contrib) | `v3/websocket/v1.2.0` | `v3_websocket_1.x.x` |

### `release-all` mode (manual dispatch)

Fetches all published releases from the GitHub API and creates version snapshots for each module's latest release. Useful for:

- Initial setup of version snapshots after enabling sync-docs
- Recovery after a failed sync
- Manually re-syncing all versions

For **single-module** repos: finds the latest release and creates one version snapshot.

For **multi-module** repos: groups releases by module prefix, takes the latest release per module, and creates version snapshots for all of them in one run.

## Triggers

| Trigger | Mode | When |
|---------|------|------|
| `push` to main | `push` | Markdown files changed |
| `release: published` | `release` | Release published (manual or automated) |
| `repository_dispatch: sync-docs` | `push` | Dispatched by weekly-release post-release hooks |
| `workflow_dispatch` | user choice | Manual trigger via Actions UI |

## Per-Repo Configuration

| Repo | Type | Source | Destination | Version file | Docusaurus command | Patterns |
|------|------|--------|-------------|--------------|-------------------|----------|
| fiber | single | `docs` | `docs/core` | `versions.json` | `docs:version` | `*` (all files) |
| contrib | multi | `v3` | `docs/contrib` | `contrib_versions.json` | `docs:version:contrib` | `*.md` |
| storage | multi | `.` | `docs/storage` | `storage_versions.json` | `docs:version:storage` | `*.md` |
| template | multi | `.` | `docs/template` | `template_versions.json` | `docs:version:template` | `*.md` |
| recipes | single | `.` | `docs/recipes` | — | — | `*.md` + images |

## Manual Dispatch

### Sync current docs (push mode)

1. Go to the repo's Actions tab
2. Select "Sync docs" workflow
3. Click "Run workflow"
4. Select mode: `push`

### Re-sync all release versions

1. Go to the repo's Actions tab
2. Select "Sync docs" workflow
3. Click "Run workflow"
4. Select mode: `release-all`

This fetches all latest releases from the GitHub API and creates Docusaurus version snapshots for each module.

## Integration with Weekly Releases

During automated weekly releases:

1. The weekly-release workflow publishes modules using `GITHUB_TOKEN`
2. `GITHUB_TOKEN` does **not** trigger `release: published` workflows (GitHub limitation)
3. After all modules are published, `post-release` hooks in `release-plan.yml` dispatch a `sync-docs` event via `repository_dispatch`
4. The sync-docs workflow runs once in `push` mode, syncing the current state

For **manual releases** (human publishes via GitHub UI):
- `release: published` triggers sync-docs in `release` mode
- Docusaurus version snapshot is created for the specific tag

## Environment Variables

The central script (`sync_docs.sh`) accepts these environment variables:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TOKEN` | yes | — | Token with push access to gofiber/docs |
| `EVENT` | yes | — | Mode: `push`, `release`, or `release-all` |
| `TAG_NAME` | release only | — | Release tag name |
| `REPO_TYPE` | no | `single` | `single` or `multi` |
| `SOURCE_DIR` | no | `.` | Source directory in caller repo |
| `DESTINATION_DIR` | no | `docs` | Destination in docs repo |
| `VERSION_FILE` | no | — | Docusaurus versions JSON file |
| `DOCUSAURUS_COMMAND` | no | — | Docusaurus versioning command |
| `FILE_PATTERNS` | no | `*.md` | Comma-separated file patterns |
| `EXCLUDE_PATTERNS` | no | — | Comma-separated dirs to exclude |
| `COMMIT_URL` | no | — | Base URL for commit messages |
| `GH_REPO` | release-all | — | GitHub repo for API calls |
| `GH_TOKEN` | release-all | — | Token for GitHub API (read access) |

## Troubleshooting

### "No changes to push"

The docs repo already has the latest content. This is normal when no documentation files changed.

### "Failed to push after 5 attempts"

Another workflow is pushing to the docs repo concurrently. The script retries with `git pull --rebase` between attempts. If all 5 fail, check for conflicts in the docs repo.

### "No releases found" (release-all mode)

The repo has no published, non-draft, non-prerelease releases. Check the Releases page.

### release mode creates wrong version identifier

The version identifier is computed from the tag format. Verify the tag follows the expected pattern:
- Single: `vMAJOR.MINOR.PATCH` (e.g. `v3.0.0`)
- Multi: `MODULE/vMAJOR.MINOR.PATCH` (e.g. `redis/v3.1.0`)
- Multi with prefix: `PREFIX/MODULE/vMAJOR.MINOR.PATCH` (e.g. `v3/websocket/v1.2.0`)

## Adding a New Repo

Create `.github/workflows/sync-docs.yml` in the new repo:

```yaml
name: Sync docs
on:
  push:
    branches: [main]
    paths: ['**/*.md']
  release:
    types: [published]
  repository_dispatch:
    types: [sync-docs]
  workflow_dispatch:
    inputs:
      mode:
        type: choice
        options: [push, release-all]
        default: push
jobs:
  sync:
    uses: gofiber/.github/.github/workflows/sync-docs.yml@main
    with:
      repo-type: single           # or multi
      source-dir: '.'             # path containing docs
      destination-dir: docs/NAME  # path in docs repo
      version-file: NAME_versions.json
      docusaurus-command: npm run docusaurus -- docs:version:NAME
      commit-url: https://github.com/gofiber/NAME
      event-mode: >-
        ${{ github.event_name == 'workflow_dispatch' && inputs.mode
        || github.event_name == 'repository_dispatch' && 'push'
        || github.event_name }}
      tag-name: ${{ github.ref_name }}
    secrets:
      doc-sync-token: ${{ secrets.DOC_SYNC_TOKEN }}
```

Prerequisite: the repo must have the `DOC_SYNC_TOKEN` secret configured.
