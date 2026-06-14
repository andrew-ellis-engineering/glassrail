# Spec: Pre-release hygiene

## Purpose

Make the repository credible, clean, and easy to trust before public package
releases and launch pushes. This workstream is about reducing friction and
embarrassment risk: stale names, stale URLs, untracked artifacts, confusing
install instructions, accidental secrets, loose docs, and release-blocking
quality issues.

The product promise is reliability infrastructure for agentic workflows, so the
repo has to model that promise. A reviewer should be able to clone Glassrail,
run the checks, inspect the eval status, and understand what is stable without
feeling like they have to reverse-engineer the project state.

## Scope

- Clean stale naming, URLs, package metadata, and documentation drift.
- Remove or ignore local/generated artifacts that should not ship.
- Verify that the published package contents are intentional.
- Make the README, changelog, roadmap, and docs agree on release state.
- Confirm that the check suite and docs build are green before release.

## Non-goals

- New user-facing agent capabilities.
- Large internal refactors that are not needed for release confidence.
- Weakening evals to improve release numbers.
- A broad website redesign; that is owned by the product website spec.

## Release bar

Pre-release hygiene is complete when all of the following are true:

- `main` is up to date with the remote and CI is green.
- The working tree is clean except for the intentional release change.
- The full check sweep passes:

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

- The docs build passes:

```bash
uv run mkdocs build --strict
```

- Phase 1 eval status is documented as met, with the remaining eval ratchet
  clearly described as quality work rather than a release blocker.
- Package metadata, docs URLs, and README links point at the current Glassrail
  repo and, once live, the current product website.
- No generated build output, local scratch files, secrets, caches, or temporary
  eval artifacts are tracked unintentionally.

## Required cleanup

### Naming and URL audit

Search the full repository for stale project names and old repository URLs.
Expected intentional references may remain in historical changelog entries only
when they provide useful context.

Recommended searches:

```bash
rg -n "dagagent|dag-agent|DagAgent|andrewellis/glassrail|checkrail|CheckRail"
rg -n "glassrail" pyproject.toml README.md mkdocs.yml docs src tests
```

Items to confirm before release:

- `pyproject.toml` project URLs point at the current GitHub repo and, once
  live, the canonical docs site.
- `mkdocs.yml` `site_url`, `repo_url`, and `repo_name` point at the current
  Glassrail repo/docs home.
- `README.md` status text says the Phase 1 eval gate is met, APIs are still
  0.x unstable, and PyPI installation is available.

### README alignment

The README should be the release front door, not an internal status dump. Before
release, update it so a new user immediately sees:

- What Glassrail is: a DAG-planning agent that makes agentic workflows
  inspectable, validated, and repeatable.
- What is stable enough to try.
- That APIs are still 0.x and may change.
- How to install from PyPI.
- How to run from source for development.
- How to configure at least one model tier.
- Where eval results, architecture docs, and the roadmap live.

Avoid duplicating the roadmap or changelog in the README. Link to those docs
instead.

### Changelog and version audit

Before cutting a release:

- Move the version's changelog entries from unreleased status to a dated release
  section.
- Keep an `Unreleased` section at the top for future work.
- Ensure release notes name user-visible capabilities and operational
  reliability work, not every internal implementation detail.
- Confirm `pyproject.toml` version matches the release tag.

### Artifact and secret sweep

Check for files that should not be tracked or published:

- Build output: `dist/`, `build/`, `site/`.
- Coverage output: `.coverage`, `htmlcov/`, `coverage.xml`.
- Python caches and type caches: `__pycache__/`, `.pytest_cache/`,
  `.ruff_cache/`, `.pyright/`.
- Local scratch notes: `SCRATCH.md` should remain gitignored.
- Local env files: `.env`, API keys, OpenRouter keys, PyPI tokens, tracing
  endpoints with credentials.
- Eval run artifacts that are useful locally but not intended as source.

Use `git status --ignored --short` to inspect ignored and untracked files before
release. Do not delete historical tracked eval fixtures or documentation unless
they are actually obsolete.

### Package contents audit

Build the package and inspect what would ship:

```bash
uv build
tar -tf dist/glassrail-*.tar.gz | sort
python -m zipfile -l dist/glassrail-*.whl
```

Confirm the distributions include:

- `src/glassrail` package code.
- Package metadata.
- README and license.
- Any runtime data files that are actually required.

Confirm the distributions exclude:

- Tests, unless intentionally shipped.
- Eval run outputs.
- Local docs build output.
- Scratch files, local configs, and credentials.

### Clean install smoke test

Install the built wheel in a clean temporary environment and run the CLI smoke
checks:

```bash
python -m venv /tmp/glassrail-smoke
/tmp/glassrail-smoke/bin/python -m pip install dist/glassrail-*.whl
/tmp/glassrail-smoke/bin/glassrail --help
/tmp/glassrail-smoke/bin/glassrail run --help
```

If the command surface changes while preparing the release, update the README in
the same change.

## Concrete audit findings (2026-06-10) — resolve before tagging

Findings from the June 2026 full-repo audit. Checked items were fixed in the
audit's documentation change; unchecked items remain open and this sweep owns
verifying them.

- [x] Stale `andrewellis` repo URLs in `docs/evals.md` and
  `docs/deployment.md` (fixed; the in-flight release change already covered
  `mkdocs.yml`, `pyproject.toml`, and `web.py`).
- [x] `scripts/name_check.py` defaults and user-agent URL now point at the
  current `andrew-ellis-engineering/glassrail` repo.
- [x] README claimed `max_generation_tokens` defaults to `16384`; the settings
  default is `20000` (fixed).
- [x] README described the DAG viewer's layers as "parallel" while the
  executor is strictly sequential (wording fixed to "dependency layers"; real
  parallelism is `docs/specs/parallel-execution.md`).
- [x] `docs/index.md` still said "Phase 0.5 complete" (fixed — now states the
  Phase 1 gate and release prep).
- [x] `PHASE1_REMAINING.md` contradicted the roadmap's gate status (deleted;
  content absorbed into `docs/specs/eval-integrity.md`; the roadmap's "Gate
  definition and integrity caveats" section is the single source of truth).
- [x] `AGENTS.md` had drifted from `CLAUDE.md` (missing the DAG-acyclicity
  primitive; referenced a nonexistent `eval-framework/AGENTS.md`) — re-synced.
- [x] The root exception class was renamed from `DagagentError` to
  `GlassrailError` before shipping it as public API.
- [x] The `image_generate` tool (`[tools.image]`, mflux) is documented in the
  README per `docs/specs/small-fixes.md` item 11.
- [x] The CLI — including the `glassrail run --json` envelope the eval
  framework depends on — has direct tests per `docs/specs/small-fixes.md`
  item 9.
- [x] Release-blocking specs complete per the roadmap's "Release 0.1.0 —
  blocking workstream" section: `docs/specs/eval-integrity.md` is implemented
  for the 0.1.0 gate with the promotion ratchet ongoing;
  `docs/specs/security-baseline.md` items 1, 2, 5 are complete for the tag.

## Acceptance criteria

- Full Python check sweep passes.
- Docs build passes with strict mode.
- Built wheel installs in a clean environment.
- CLI help works from the installed wheel.
- README, roadmap, changelog, package metadata, and docs nav agree on release
  state.
- `git status --short` contains only intentional release changes.
- No accidental local artifacts or secrets are tracked.

## Exit decision

If the hygiene sweep finds small documentation or metadata drift, fix it in the
release branch before publishing. If it finds correctness bugs, packaging bugs,
or eval regressions that undermine the project promise, pause the release and
fix those first.
