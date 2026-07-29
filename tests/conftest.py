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
    config.addinivalue_line(
        "markers", "live_search: needs a working Tavily API (skipped if unreachable)"
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


@lru_cache(maxsize=1)
def _search_status() -> tuple[bool, str]:
    """Probe Tavily once per session. Returns (reachable, reason-if-not)."""
    import agent

    if not (agent.os.getenv("TAVILY_API_KEY") or "").strip():
        return False, "TAVILY_API_KEY is not set"

    outcome = agent.search_opportunities("scholarship", max_results=1)
    if outcome["ok"]:
        return True, ""

    error = outcome.get("error", "")
    if "quota" in error.lower():
        return False, "Tavily search quota exhausted — wait for reset to run live tests"
    return False, f"Tavily unreachable: {error[:120]}"


@pytest.fixture(autouse=True)
def _skip_when_backends_unavailable(request):
    """Skip live-marked tests when the backend they need is not answering.

    An exhausted quota or an expired key is not a defect in this code, and a red
    suite that really means "top up your account" is a misleading signal.
    """
    if request.node.get_closest_marker("live_llm"):
        reachable, reason = _llm_status()
        if not reachable:
            pytest.skip(reason)

    if request.node.get_closest_marker("live_search"):
        reachable, reason = _search_status()
        if not reachable:
            pytest.skip(reason)
