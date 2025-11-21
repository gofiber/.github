# Reusable workflow opportunities across gofiber repositories

The following repositories were scanned for existing GitHub Actions workflows:

- `gofiber/fiber`
- `gofiber/storage`
- `gofiber/recipes`
- `gofiber/cli`
- `gofiber/contrib`
- `gofiber/template`

Several workflows appear in multiple repositories and can be centralized in this `.github` repository as reusable workflows triggered via `workflow_call`.

## High-value candidates for centralization

### Quality and testing
- **Go linters**: `linter.yml`/`golangci-lint.yml` are present in `fiber`, `cli`, `storage`, `contrib`, and `template`. Standardize on a single `golangci-lint` reusable workflow with inputs for Go version and optional module path overrides.
- **Unit/integration tests**: General `test.yml` (`fiber`, `cli`) and many `test-*.yml` matrices (`storage`, `contrib`, `template`). Provide a reusable test workflow that accepts a matrix definition as an input file or inline JSON, plus flags for Docker services (e.g., databases) to keep per-repo jobs thin.
- **Benchmark runs**: `benchmark.yml` exists in `fiber`, `storage`, and `template`. A reusable benchmark workflow could unify Go version, caching, and artifact publishing.

### Documentation and labeling
- **Doc sync**: `sync-docs.yml` appears in `fiber`, `storage`, `recipes`, `contrib`, and `template`. Centralize into a reusable workflow with inputs for source/target paths and branch protections.
- **Markdown checks**: `markdown.yml` is used in `fiber` and `cli`; a reusable job for `markdownlint`/`mdformat` keeps rules consistent.
- **Spell checking**: `spell-check.yml` is specific to `fiber` but can be generalized as a reusable spelling workflow used where needed.
- **Auto labeling**: `auto-labeler.yml` shows up in `fiber`, `storage`, `contrib`, and `template`. A reusable labeler can centralize label mappings and path globs.

### Release and dependency hygiene
- **Release drafter**: `release-drafter.yml` is in every repository scanned. Moving the workflow and configuration to this repository lets each repo just call the reusable job and, if desired, keep only a small YAML pointing to org-level config.
- **Dependabot auto-merge**: Variants (`dependabot_automerge.yml`, `dependabot-automerge.yml`, `manual-dependabot.yml`) appear across repos. A reusable workflow with inputs for allowed package ecosystems and environments would unify the behavior.

### Security
- **Code scanning**: `codeql-analysis.yml` (`fiber`, `cli`) and `gosec`/`govulncheck` (`template`) plus `vulncheck.yml` (`fiber`, `cli`). These can be merged into a single security reusable workflow with inputs to enable CodeQL, `govulncheck`, or `gosec` per repository, sharing caching and SARIF upload patterns.

### Observations and next steps
- The per-driver tests in `storage` and `contrib` (e.g., `test-mysql.yml`, `test-redis.yml`, `test-otel.yml`) mostly follow the same pattern: set up service container, run `go test ./...`. By parameterizing service images, env vars, and matrix axes, one or two reusable workflows could cover these cases.
- `sync-docs` and `release-drafter` are the most ubiquitous and lowest-risk to centralize first. Follow up with reusable lint/test/security workflows that accept inputs for Go version, cache keys, and optional matrix definitions.
- When publishing reusable workflows here, document required secrets (e.g., `GITHUB_TOKEN`, `NPM_TOKEN`, `CODECOV_TOKEN`) and default permissions so consuming repositories only need a minimal wrapper.

## Implemented reusable workflows

The following reusable workflows now live in this repository and can be called from the other gofiber projects:

- `go-lint.yml`: Runs `golangci-lint` with the pinned versions used across gofiber repositories.
- `go-test.yml`: Runs `gotestsum` across a configurable Go/platform matrix and mirrors the coverage uploads used in `gofiber/fiber`.
- `markdown-check.yml`: Provides Markdown linting via `markdownlint-cli2` with the org-standard globs.
- `release-drafter.yml`: Central Release Drafter runner with customizable config path and token.
- `dependabot-automerge.yml`: Enables auto-merge for Dependabot pull requests using the GitHub CLI.
- `security-golang.yml`: Runs `govulncheck` and optionally CodeQL for Go projects.
- `sync-docs.yml`: Syncs documentation (or any path) to another repository and branch with a provided token.
- `auto-labeler.yml`: Applies centralized label mappings and can optionally sync label definitions across repositories.

Shared configuration that accompanies these workflows:

- `.github/release-drafter.yml`: Organization-wide release notes template and categories.
- `.github/labeler.yml`: Organization-wide label globs used by the auto-labeler workflow.
- `.github/.golangci.yml`: Organization-wide golangci-lint configuration consumed by the `go-lint` workflow.
