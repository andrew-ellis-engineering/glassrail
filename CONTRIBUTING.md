# Contributing

Thanks for your interest in `glassrail`. This project is in early development
(Phase 1); the foundation is stable but the public API may still shift.

## Development setup

Requires [uv](https://github.com/astral-sh/uv) and Python 3.12+.

```bash
uv sync --all-extras --group dev
uv run pre-commit install
```

## Pull request workflow

All changes to `main` go through pull requests. Direct pushes to `main` are
blocked for maintainers and contributors alike; work on a branch or fork, open a
PR, let CI run, resolve review threads, and merge only after the branch is green.

For outside contributors, a maintainer reviews the PR and decides whether to
merge it. For maintainer-authored work, the same PR-and-CI path applies; the
maintainer may merge their own PR once checks pass and any open discussion is
resolved.

Keep PRs focused. The description should cover:

- What changed and why.
- How you validated it.
- Whether docs, config, CLI behavior, evals, or release notes need updates.
- Any follow-up work or known risk.

Large architectural changes should start as an issue or design discussion before
code. Routine fixes, docs improvements, and tightly scoped tests can go straight
to PR.

The repository uses squash merges for a linear `main`; delete branches after
merge unless there is a clear reason to keep them.

## Before you open a PR

Run the full check sweep and make sure it's green — CI runs the same on
Linux + macOS for Python 3.12 and 3.13:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

The bar is zero test failures, zero lint findings, and a clean pyright (no
errors and no warnings). `pre-commit` enforces ruff + pyright on commit.

## Tests

`pytest` runs with `asyncio_mode = "auto"`, so async tests are just
`async def test_...` — no marker needed. Tests live under `tests/`:

- `unit/` — fast and isolated.
- `integration/` — several real components with a scripted fake LLM provider.
- `contract/` — shared suites every plugin implementation must pass. Adding a
  new `StateStore` (or other plugin) backend means adding it to the
  parametrisation; the whole contract suite then runs against it.
- `property/` — hypothesis invariants.

New behaviour should come with tests. A new plugin backend should pass the
relevant contract suite rather than ship its own bespoke tests.

Model-quality **evals** are separate from the pytest suite: they live in the
standalone [`eval-framework/`](./eval-framework) and run the real agent
end to end. See [docs/evals.md](./docs/evals.md).

## Conventions

Project conventions (layout, tooling, architectural primitives, commit style)
are documented in [`CLAUDE.md`](./CLAUDE.md) — read it before making structural
changes. In short: src-layout, `ruff` + `pyright` strict, stdlib `asyncio`,
ULID ids, `pydantic-settings`, Apache-2.0.

## Commit messages

Plain prose. One summary line, then a couple of lines of body explaining the
*why* when it isn't obvious. Keep them concise.

## Scope and direction

See [`docs/roadmap.md`](./docs/roadmap.md) for what's planned and the current
phase. If you're considering a larger change, please open an issue to discuss
it first so we can make sure it fits the architecture before you invest time.

## License

By contributing, you agree that your contributions are licensed under the
project's [Apache-2.0](./LICENSE) license.
