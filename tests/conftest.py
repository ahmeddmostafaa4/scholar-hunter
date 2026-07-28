"""Shared test setup.

Tests that make real Claude calls are marked `live_llm`. They skip — rather than
fail — when the API cannot be reached, because an expired key, an exhausted
credit balance or a rate limit is not a defect in this code, and a red suite
that actually means "top up your account" is a misleading signal.

Run only the offline tests with:  pytest -m "not live_llm"
"""

import sys
from functools import lru_cache
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live_llm: needs a working Anthropic API (skipped if unreachable)"
    )


@lru_cache(maxsize=1)
def _llm_status() -> tuple[bool, str]:
    """Probe Claude once per session. Returns (reachable, reason-if-not)."""
    import agent

    if not (agent.os.getenv("ANTHROPIC_API_KEY") or "").strip():
        return False, "ANTHROPIC_API_KEY is not set"

    result = agent.check_llm()
    if result["ok"]:
        return True, ""

    error = result.get("error", "")
    if "credit balance" in error:
        return False, "Anthropic credit balance exhausted — top up to run live tests"
    if "rate" in error.lower():
        return False, "Anthropic API is rate-limiting"
    return False, f"Anthropic API unreachable: {error[:120]}"


@pytest.fixture(autouse=True)
def _skip_when_llm_unavailable(request):
    """Skip anything marked `live_llm` when the API is not answering."""
    if request.node.get_closest_marker("live_llm"):
        reachable, reason = _llm_status()
        if not reachable:
            pytest.skip(reason)
