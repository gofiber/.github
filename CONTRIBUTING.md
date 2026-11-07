# Contributing

Thank you for helping to improve Fiber. These guidelines apply to every
repository of the [gofiber](https://github.com/gofiber) organization that has
no `CONTRIBUTING.md` of its own. Repositories with their own file, such as
[gofiber/fiber](https://github.com/gofiber/fiber),
[gofiber/recipes](https://github.com/gofiber/recipes) and
[gofiber/awesome-fiber](https://github.com/gofiber/awesome-fiber), describe
their additional rules there.

Please follow our [Code of Conduct][coc] in all your interactions with the
project.

## Before you start

- Search the existing issues and pull requests first. Someone may already be
  working on the same change.
- For anything larger than a small fix, open an issue or start a discussion on
  [Discord](https://gofiber.io/discord) before you write code, so the change
  can be agreed with the maintainers first.
- For usage questions, see [SUPPORT.md][support].
- Never report a security problem in a public issue or pull request. Report it
  privately with **Report a vulnerability** in the **Security** tab of the
  repository, as described in the [security policy][security].

## Reporting issues

Open the issue in the repository where the affected code lives and use one of
its issue templates. Include the version you use, a minimal example that
reproduces the problem, and the result you expected.

## Documentation

The website [docs.gofiber.io](https://docs.gofiber.io/) is built from
[gofiber/docs](https://github.com/gofiber/docs), but most of its content is
written in the repository it describes and synced into gofiber/docs from
there. Change it at the source:

- Fiber core (API, guides, built-in middleware): `docs/` in gofiber/fiber
- Contrib middleware: `v3/<module>/README.md` in gofiber/contrib
- Storage drivers: `<driver>/README.md` in gofiber/storage
- Template engines: `<engine>/README.md` in gofiber/template
- Recipes: `<recipe>/README.md` in gofiber/recipes

Only changes to the website itself (layout, navigation, build) belong in
gofiber/docs.

## Pull requests

1. Fork the repository and create a branch from its default branch.
2. Keep each pull request focused on one change. Unrelated refactoring,
   formatting or dependency updates belong in a pull request of their own.
3. Add or update tests for every feature and bug fix. Pull requests that add a
   feature or fix a bug without tests are not merged.
4. Update the documentation (README, `docs/`, doc comments) when you change
   behavior or public API.
5. Commit generated files, such as code generation output or a bundled
   `dist/`, together with the change that requires them. CI fails when they
   are out of date.
6. Run the checks below and make sure they pass.
7. Fill in the pull request template if the repository has one, and link the
   issue the pull request resolves, for example `Fixes #123`.

New dependencies must be agreed with the maintainers before they are added.

Write code, comments, commit messages and pull request text in English.

### Pull request titles

Pull request titles become the entries of the release notes and decide which
labels the auto-labeler adds, so they follow the Conventional Commits format:

```text
type(scope): short description
```

- `type` is one of `feat`, `fix`, `perf`, `refactor`, `test`, `docs`,
  `style`, `build`, `ci`, `chore` or `revert`.
- `scope` is optional and names the package, module or area, for example
  `fix(cache): drop the stale entry` or `docs(readme): add the zap middleware`.
- Add `!` after the type or scope for a breaking change, for example
  `feat(session)!: remove the deprecated option`, and describe the migration
  in the pull request description.

Some repositories put an emoji in front of the type, for example `🐛 bug:` in
gofiber/fiber. Their own `CONTRIBUTING.md` lists the prefixes they use.

### Checks to run locally

Run the same checks as CI before you push:

- In Go repositories, use the Makefile targets `make lint` and `make test`
  where present (`make help` lists every target, `make format` formats the
  code). In repositories without these targets, such as the multi-module
  repositories gofiber/contrib and gofiber/storage, run `go test ./...` and
  `golangci-lint run` inside each module you changed. golangci-lint picks up
  the repository's `.golangci.yml` on its own.
- In Node.js repositories, use the scripts from `package.json`, for example
  `npm run lint`, `npm test` and `npm run build`.
- For Markdown changes, run `make markdown` where present. It runs
  `markdownlint-cli2` with the repository's `.markdownlint.yml`.

In the multi-module repositories gofiber/contrib, gofiber/storage and
gofiber/template, a new module needs its own directory with a `go.mod`, a
`README.md` that starts with the `id` front matter used by the docs sync, a
`test-<module>.yml` workflow and an entry in `.github/dependabot.yml`. Copy an
existing module as the starting point.

Some repositories describe further conventions in their README or in an
`AGENTS.md` file. Please read them before you start.

## Support the project

If you want to say **thank you** or support the active development of Fiber:

1. Add a GitHub star to the repositories you use.
2. Write a review or tutorial on [Medium](https://medium.com/),
   [Dev.to](https://dev.to/) or your personal blog.
3. Become a [GitHub Sponsor](https://github.com/sponsors/gofiber).

[coc]: https://github.com/gofiber/.github/blob/main/CODE_OF_CONDUCT.md
[support]: https://github.com/gofiber/.github/blob/main/SUPPORT.md
[security]: https://github.com/gofiber/.github/blob/main/SECURITY.md
