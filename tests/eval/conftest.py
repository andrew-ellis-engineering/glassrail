"""Eval-suite conftest.

Prints the aggregate score table once the run finishes. The eval tests
append their :class:`~tests.eval.harness.ScenarioResult` to a stash list as
they go; this hook renders them. It no-ops when no eval scenarios ran, so it
stays silent during the ordinary ``uv run pytest`` sweep.
"""

from __future__ import annotations

import pytest

from tests.eval.harness import (
    EVAL_LIVE_RESULTS_KEY,
    EVAL_RESULTS_KEY,
    format_summary,
)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    del exitstatus
    for key, label in ((EVAL_RESULTS_KEY, "deterministic"), (EVAL_LIVE_RESULTS_KEY, "live")):
        if key not in config.stash:
            continue
        results = config.stash[key]
        if results:
            terminalreporter.write_line(format_summary(results, label=label))
