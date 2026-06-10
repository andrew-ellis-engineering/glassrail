# Spec: PyPI release

## Purpose

Publish Glassrail as an installable Python package so people can try the agent
without cloning the repository. The first release should communicate maturity
where it exists - validated plans, deterministic tier routing, fresh context,
telemetry, evals - while being explicit that the API is still 0.x and subject
to change.

The release is not just distribution. It is a credibility signal: Glassrail is a
serious AI infrastructure project with a repeatable check suite, documented eval
gate, and a clean operational surface.

## Scope

- Publish `glassrail` to PyPI.
- Verify package metadata and README rendering.
- Validate install and CLI smoke tests from the published package.
- Create a GitHub release and version tag.
- Record release status in the changelog and docs.

## Non-goals

- Claiming production readiness.
- Stabilizing every Python API.
- Shipping a hosted service.
- Launching broad marketing before install and docs are verified.

## Release prerequisites

- Pre-release hygiene spec is complete.
- `main` is up to date and CI is green.
- The Phase 1 eval gate is documented as met in the roadmap.
- The remaining eval ratchet is documented and not a release blocker.
- The package name `glassrail` is available or already controlled on PyPI.
- PyPI publishing is configured using either trusted publishing or a scoped API
  token.
- TestPyPI credentials are available if using the dry-run path.
- `README.md` includes PyPI install instructions, while source install remains
  documented for contributors.

## Version policy

Use SemVer 0.x:

- `0.1.0` is the first public package release.
- Breaking changes are allowed during 0.x, but should be called out clearly.
- Patch releases fix release, packaging, documentation, or correctness issues.
- Minor releases may add capabilities or change APIs with release notes.

Use the alpha classifier until the project has external users, a clearer API
compatibility policy, and a stronger field record.

## Release steps

### 1. Prepare the release branch

- Start from current `main`.
- Apply final hygiene fixes.
- Update `CHANGELOG.md`: date the `0.1.0` section.
- Confirm `pyproject.toml` version is `0.1.0`.
- Confirm package URLs and docs URLs are current.
- Run the full check sweep and docs build.

### 2. Build distributions

```bash
uv build
```

Inspect the source distribution and wheel contents before upload:

```bash
tar -tf dist/glassrail-*.tar.gz | sort
python -m zipfile -l dist/glassrail-*.whl
```

Optional but recommended:

```bash
uv run python -m twine check dist/*
```

If `twine` is not present, add it to the release environment rather than the
runtime dependencies.

### 3. Test install from built wheel

```bash
python -m venv /tmp/glassrail-wheel-smoke
/tmp/glassrail-wheel-smoke/bin/python -m pip install dist/glassrail-*.whl
/tmp/glassrail-wheel-smoke/bin/glassrail --help
/tmp/glassrail-wheel-smoke/bin/glassrail run --help
```

This verifies the entry point and package metadata before anything leaves the
machine.

### 4. Optional TestPyPI dry run

Publish to TestPyPI first if the packaging path has changed or the PyPI project
has not been used before.

```bash
uv run python -m twine upload --repository testpypi dist/*
```

Then install in a clean environment using TestPyPI as the package source and
repeat the CLI smoke checks. If dependencies are not available through TestPyPI,
install with PyPI as an extra index.

### 5. Publish to PyPI

Preferred path: GitHub Actions trusted publishing from a tagged release or
manual workflow approval. The release workflow lives at
`.github/workflows/publish.yml`; it builds the distributions, inspects the
artifacts, installs the wheel into a clean environment, runs CLI help smoke
checks, and then publishes to PyPI using trusted publishing.

Manual fallback:

```bash
uv run python -m twine upload dist/*
```

After upload, verify:

- The PyPI page exists.
- README renders correctly.
- Project URLs are correct.
- Classifiers and license are correct.
- `pip install glassrail` works in a clean environment.

### 6. Tag and create GitHub release

Create and push the version tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Create a GitHub release using the changelog as source material. The release
notes should emphasize:

- DAG plans instead of opaque agent loops.
- Fresh context per node.
- Deterministic model tier routing.
- Validation, execution semantics, and typed event streams.
- OpenTelemetry support.
- Eval gate met and remaining known ratchet.
- 0.x API instability.

## Rollback and recovery

PyPI releases are effectively immutable. If a bad artifact is uploaded:

- Yank the broken version if users should avoid it.
- Fix forward with `0.1.1`.
- Document the issue in the changelog.
- Do not delete GitHub tags or rewrite release history unless no public release
  was actually published.

## Acceptance criteria

- `glassrail==0.1.0` is installable from PyPI.
- The installed `glassrail` command exposes help successfully.
- The README renders correctly on PyPI.
- GitHub has a `v0.1.0` tag and release.
- `CHANGELOG.md` records the dated release.
- Docs and README point users to the release and next steps.
- Known limitations are explicit and not buried.

## Post-release checks

Within the first hour:

- Install from PyPI in a fresh environment on macOS.
- Install from PyPI in a fresh Linux container if available.
- Run a no-backend CLI help smoke test.
- Run one real task with a configured model tier.
- Watch GitHub issues for install failures or confusion.

Within the first week:

- Fix documentation friction quickly.
- Cut `0.1.1` for packaging or install bugs if needed.
- Avoid bundling unrelated feature work into release fixes.
