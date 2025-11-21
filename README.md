# .github

Centralized configurations and reusable GitHub Actions workflows for the gofiber organization.

## Reusable workflows

Each workflow can be consumed from other repositories with:

```yaml
jobs:
  example:
    uses: gofiber/.github/.github/workflows/<workflow-file>@main
    # with: ...
    # secrets: ...
```

### go-lint
Runs `golangci-lint` with the same defaults as the gofiber repositories.
- Inputs: `go-version` (default `1.25.x`), `working-directory` (default `.`), `golangci-version` (default `v2.5.0`), `config-path` (default `.github/.golangci.yml`), `config-repository` (default `gofiber/.github`), `config-ref` (default `main`), `use-shared-config` (default `true`).

### go-test
Runs `gotestsum` across the Go/test matrix used in `gofiber/fiber` by default.
- Inputs: `go-versions` JSON array (default `["1.25.x"]`), `platforms` JSON array (default `["ubuntu-latest", "windows-latest", "macos-latest"]`), `working-directory` (default `.`), `package-pattern` (default `./...`), `codecov-flags` (default `unittests`), `codecov-slug` (default `${{ github.repository }}`), `enable-codecov` (default `true`), `enable-repeated` (default `true`).
- Secrets: optional `codecov-token` when publishing coverage.

### markdown-check
Runs `markdownlint-cli2` against Markdown files.
- Inputs: `globs` (newline-separated list) and optional `config` path. Defaults match the organization markdown workflow.

### release-drafter
Runs Release Drafter with a configurable config path fetched from this repo by default.
- Inputs: `config-path` (default `.github/release-drafter.yml`), `config-repository` (default `gofiber/.github`), `config-ref` (default `main`).
- Secrets: optional `release-token` with `contents:write` permission; otherwise uses the default token.

### auto-labeler
Applies labels from the shared configuration in this repository.
- Inputs: `config-path` (default `.github/labeler.yml`), `config-repository` (default `gofiber/.github`), `config-ref` (default `main`).
- Secrets: optional `github-token` with `pull_requests:write` and `issues:write`; otherwise uses the default token.

### dependabot-automerge
Automatically enables auto-merge on Dependabot pull requests.
- Secrets: optional `github-token` with `pull_requests:write` and `contents:write` permissions.
- Only runs when the actor is `dependabot[bot]`.

### security-golang
Runs Go security checks.
- Inputs: `run-govulncheck` (default `true`), `run-codeql` (default `false`), `working-directory` (default `.`), `go-version` (default `stable`).
- CodeQL publishes results to code scanning when enabled.

### sync-docs
Pushes documentation (or any directory) from the caller repository to another repository.
- Inputs: `source-path`, `destination-repo`, optional `destination-branch`, `destination-path`, `commit-message`, `git-user-name`, `git-user-email`.
- Secrets: `destination-token` with push access to the destination repository.

## Shared configuration

This repository also stores configuration that should remain identical across gofiber projects:

- `.github/release-drafter.yml`: Release Drafter template and categories used by the `release-drafter` workflow.
- `.github/labeler.yml`: Label mappings used by the `auto-labeler` workflow.
- `.github/.golangci.yml`: golangci-lint rules used by the `go-lint` workflow.

When consuming the corresponding workflows, the shared configuration is automatically fetched from this repository unless you override the inputs.
