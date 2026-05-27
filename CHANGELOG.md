# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Phase 0.5 package skeleton: src-layout, subpackages for core / planner / executor / validator / harness / providers / state / events / channels / gateways / cli.
- Tooling: uv, ruff, pyright (strict), pytest + hypothesis, pre-commit, MkDocs + Material.
- CI: GitHub Actions, multi-OS (Linux + macOS), Python 3.12 + 3.13.
- Apache-2.0 license.

## [0.1.0] - Unreleased

Initial development release. Phase 0 prototype preserved in `agent_server.py`; port to the package layout is in progress.
