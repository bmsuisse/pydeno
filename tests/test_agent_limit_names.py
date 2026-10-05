"""Agent-facing limit errors use the names accepted by the agent API."""

import math

import pytest

from pydeno import AgentSandbox, AsyncAgentSandbox
from pydeno._limits import limit_seconds


@pytest.mark.parametrize("agent", [AgentSandbox, AsyncAgentSandbox])
@pytest.mark.parametrize("name", ["timeout", "max_pause"])
def test_invalid_agent_duration_names_public_argument(agent, name):
    with pytest.raises(ValueError, match=rf"^{name} must"):
        agent({}, **{name: float("nan")})


def test_allowed_zero_duration_is_normalized():
    seconds = limit_seconds("grace", -0.0, allow_zero=True)
    assert seconds == 0.0
    assert math.copysign(1.0, seconds) == 1.0
