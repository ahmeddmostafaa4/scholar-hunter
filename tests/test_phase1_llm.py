"""Phase 1 tests: the Claude connection works, and a missing key fails cleanly.

The live test is skipped when ANTHROPIC_API_KEY is absent so the suite still
runs on a machine without keys.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

HAS_KEY = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
needs_key = pytest.mark.skipif(not HAS_KEY, reason="ANTHROPIC_API_KEY not set")


def test_model_name_is_a_module_constant():
    """The spec asks for the model to be easy to change in one place."""
    assert agent.MODEL_NAME == "claude-sonnet-4-6"


@needs_key
def test_live_call_returns_ok():
    result = agent.check_llm()
    assert result["ok"], result.get("error")
    assert "OK" in result["reply"]


def test_missing_key_gives_a_clear_message_not_a_stack_trace(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # build_llm raises a typed error carrying an actionable message...
    with pytest.raises(agent.MissingKeyError) as excinfo:
        agent.build_llm()
    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert ".env" in message

    # ...and check_llm converts it into a result dict rather than propagating.
    result = agent.check_llm()
    assert result["ok"] is False
    assert "ANTHROPIC_API_KEY" in result["error"]


def test_missing_key_message_does_not_leak_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")  # whitespace counts as unset
    result = agent.check_llm()
    assert result["ok"] is False
    assert "sk-ant" not in result["error"]
