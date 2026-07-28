"""Phase 4 tests: the assembled agent runs end to end and obeys the reliability rules.

The live end-to-end tests hit Claude and Tavily and take a minute or two each,
so there are only two of them. They are the ones that actually prove the agent
searches instead of answering from memory.
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

HAS_ANTHROPIC = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
HAS_TAVILY = bool(os.getenv("TAVILY_API_KEY", "").strip())

needs_llm = pytest.mark.skipif(not HAS_ANTHROPIC, reason="ANTHROPIC_API_KEY not set")
needs_both = pytest.mark.skipif(
    not (HAS_ANTHROPIC and HAS_TAVILY), reason="needs both API keys"
)


def test_toolbox_contains_the_phase_3_tools():
    names = agent.describe_tools()
    for expected in [
        "search_scholarships",
        "extract_requirements",
        "check_eligibility",
        "match_courses",
    ]:
        assert expected in names, f"{expected} missing from {names}"


def test_system_prompt_carries_the_reliability_rules():
    # Collapse the prompt's line wrapping so phrases spanning a newline still match.
    prompt = re.sub(r"\s+", " ", agent.SYSTEM_PROMPT).lower()
    assert "never invent" in prompt
    assert "source url" in prompt
    assert "without explicit user approval" in prompt
    assert "description and content" in prompt  # course matching is content-based
    assert "not by title" in prompt


def test_system_prompt_is_dated_so_deadlines_can_be_judged():
    from datetime import date

    rendered = agent.SYSTEM_PROMPT.format(today=date.today().isoformat())
    assert date.today().isoformat() in rendered


@pytest.mark.live_llm
@needs_llm
def test_agent_builds_with_a_prompt_and_tools():
    executor = agent.build_agent()
    assert executor.tools
    assert executor.max_iterations >= 10


def test_output_is_flattened_to_a_string():
    """Regression: Claude returns a list of content blocks, and an unflattened list
    reached the caller — which would break the JSON API and any .lower() on it."""
    assert agent._as_text("plain") == "plain"
    assert (
        agent._as_text(
            [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "Here is "},
                {"type": "text", "text": "the shortlist."},
            ]
        )
        == "Here is the shortlist."
    )
    assert agent._as_text(None) == ""


def test_run_agent_returns_a_string_even_for_block_output(monkeypatch):
    class FakeExecutor:
        tools = []

        def invoke(self, _payload):
            return {
                "output": [{"type": "text", "text": "shortlist"}],
                "intermediate_steps": [],
            }

    monkeypatch.setattr(agent, "build_agent", lambda **kw: FakeExecutor())
    result = agent.run_agent("hello")
    assert isinstance(result["output"], str)
    assert result["output"] == "shortlist"


def test_missing_key_is_reported_not_raised(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = agent.run_agent("find me scholarships")
    assert result["ok"] is False
    assert "ANTHROPIC_API_KEY" in result["error"]


@pytest.mark.live_llm
@needs_both
def test_end_to_end_shortlist_cites_real_sources():
    """The headline test: a full profile in, a sourced shortlist out."""
    result = agent.run_agent(
        "I hold a Bachelor's in Computer Science from Egypt with a GPA of 3.6/4.0. "
        "I am looking for Master's scholarships in Germany in machine learning. "
        "Please give me a shortlist."
    )
    assert result["ok"], result.get("error")
    output = result["output"]

    # It must have actually searched, not answered from memory.
    assert "search_scholarships" in result["tools_used"], result["tools_used"]

    # Every claim needs a citable source.
    urls = re.findall(r"https?://[^\s)\]]+", output)
    assert urls, f"shortlist cites no sources:\n{output[:1500]}"

    # And the uniform format should be visible in the prose.
    lowered = output.lower()
    assert "deadline" in lowered
    assert any(word in lowered for word in ("eligib", "requirement"))


@pytest.mark.live_llm
@needs_both
def test_agent_asks_for_missing_profile_fields_instead_of_searching():
    """A bare 'find me money' must trigger questions, not invented results."""
    result = agent.run_agent("Hi, can you find me a scholarship?")
    assert result["ok"], result.get("error")
    output = result["output"].lower()

    assert "?" in result["output"], "the agent should be asking for the missing fields"
    assert any(
        field in output
        for field in ("field of study", "degree", "gpa", "nationality", "country")
    ), result["output"][:800]
