"""The eval suite: ``uv run pytest -m eval``.

The deterministic suite is the gate — it scripts the LLM, so it's offline and
reproducible, and CI runs it on every change. The live suite grades the same
scenarios against the configured providers; it's opt-in via
``DAGAGENT_EVAL_LIVE=1`` (it hits external services and is non-deterministic,
so it never blocks CI). Both append to a stash the conftest renders as a
score table at the end of the run.
"""

from __future__ import annotations

import os

import pytest

from dagagent.config import Settings
from dagagent.providers import router_from_settings
from tests.eval.harness import (
    EVAL_LIVE_RESULTS_KEY,
    EVAL_RESULTS_KEY,
    Scenario,
    run_scenario,
)
from tests.eval.scenarios import SCENARIOS

pytestmark = pytest.mark.eval

_LIVE = os.getenv("DAGAGENT_EVAL_LIVE") == "1"
_LIVE_SCENARIOS = [s for s in SCENARIOS if not s.deterministic_only]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_scenario_deterministic(scenario: Scenario, request: pytest.FixtureRequest) -> None:
    result = await run_scenario(scenario)
    request.config.stash.setdefault(EVAL_RESULTS_KEY, []).append(result)

    assert result.leftover_script == 0, (
        f"{scenario.id}: script left {result.leftover_script} responses unused — "
        "the fixture is out of sync with the engine's call sequence"
    )
    assert result.passed, (
        f"{scenario.id} scored {result.score:.2f} < {scenario.pass_threshold:.2f}\n"
        + "\n".join(f"  x {c.name}: {c.detail}" for c in result.failures())
    )


@pytest.mark.skipif(not _LIVE, reason="set DAGAGENT_EVAL_LIVE=1 to grade live providers")
@pytest.mark.parametrize("scenario", _LIVE_SCENARIOS, ids=[s.id for s in _LIVE_SCENARIOS])
async def test_scenario_live(scenario: Scenario, request: pytest.FixtureRequest) -> None:
    router = router_from_settings(Settings())
    result = await run_scenario(scenario, router=router, content=False)
    request.config.stash.setdefault(EVAL_LIVE_RESULTS_KEY, []).append(result)

    # Live quality is reported, not gated (model output varies); we only
    # assert the harness drove a real stack to a terminal state.
    assert result.status is not None
