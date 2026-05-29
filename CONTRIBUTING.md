# Contributing

Thanks for your interest in `dagagent`. This project is in early development
(Phase 1); the foundation is stable but the public API may still shift.

## Development setup

Requires [uv](https://github.com/astral-sh/uv) and Python 3.12+.

```bash
uv sync --all-extras --group dev
uv run pre-commit install
```

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
- `eval/` — eval harness (run with `-m eval`).

New behaviour should come with tests. A new plugin backend should pass the
relevant contract suite rather than ship its own bespoke tests.

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
