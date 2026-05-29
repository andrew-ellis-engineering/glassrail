"""Paths, version, and defaults.

``HARNESS_VERSION`` is semver and MUST be bumped on any behavioral change to
generation or grading — every trial record stamps it, and results from
different harness versions are not directly comparable (principle 10).
"""

from __future__ import annotations

from pathlib import Path

# Bump on any behavioral change to running or grading.
HARNESS_VERSION = "0.1.0"

# Framework root = parent of the evalkit/ package.
FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = FRAMEWORK_ROOT / "results"

# Defaults (overridable per-suite, per-task, or via CLI flags).
DEFAULT_MODEL = "sonnet"
DEFAULT_GRADER_MODEL = "sonnet"
DEFAULT_TRIALS = 3
DEFAULT_TIMEOUT_S = 180
DEFAULT_MAX_TURNS = 10
DEFAULT_PROMOTION_THRESHOLD = 5

# Virtual target meaning "the agent's final text output" rather than a file.
RESULT_TEXT_TARGET = "__result_text__"
