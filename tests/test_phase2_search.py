"""Phase 2 tests: the Tavily search tool returns real, citable results.

Live tests are skipped without TAVILY_API_KEY. They are deliberately few —
the free tier has a request cap.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

HAS_KEY = bool(os.getenv("TAVILY_API_KEY", "").strip())
needs_key = pytest.mark.skipif(not HAS_KEY, reason="TAVILY_API_KEY not set")


def _assert_shape(outcome):
    """Every result must carry a citable https URL and a title."""
    assert outcome["ok"], outcome.get("error")
    for r in outcome["results"]:
        assert set(r) == {"title", "url", "snippet", "trusted"}
        assert r["url"].startswith("http"), r["url"]
        assert r["title"], "result has no title"


@pytest.mark.live_search
@needs_key
def test_masters_scholarship_germany():
    outcome = agent.search_opportunities(
        "Master's Data Science scholarship Germany 2026", max_results=5
    )
    _assert_shape(outcome)
    assert outcome["count"] > 0, "expected real results for a common query"


@pytest.mark.live_search
@needs_key
def test_phd_funding_egypt():
    outcome = agent.search_opportunities("PhD funding for Egyptian students", max_results=5)
    _assert_shape(outcome)
    assert outcome["count"] > 0


@pytest.mark.live_search
@needs_key
def test_nonsense_query_does_not_crash():
    """A deliberately meaningless query must return cleanly, not raise."""
    outcome = agent.search_opportunities("zzxqw plorbnat quffleglop scholarship", max_results=5)
    assert outcome["ok"] is True
    assert isinstance(outcome["results"], list)  # may legitimately be empty


@pytest.mark.live_search
@needs_key
def test_tool_output_always_carries_urls():
    """The agent must be able to cite; the tool string must contain the URLs."""
    text = agent.search_scholarships.invoke(
        {"query": "DAAD scholarship computer science master"}
    )
    assert "SEARCH FAILED" not in text
    assert "URL: http" in text or "No results found" in text


def test_empty_query_is_rejected_without_calling_the_api():
    outcome = agent.search_opportunities("   ")
    assert outcome["ok"] is False
    assert "empty" in outcome["error"].lower()
    assert outcome["results"] == []


def test_quota_exhaustion_is_reported_not_disguised_as_no_results(monkeypatch):
    """Regression: Tavily answers a used-up quota with an error, and treating that
    as an empty result list told the student "nothing matched your profile" when
    the real cause was billing."""
    class Boom:
        def invoke(self, _q):
            raise RuntimeError("432 Client Error: for url: https://api.tavily.com/search")

    monkeypatch.setattr(agent, "_tavily", lambda *a, **k: Boom())
    outcome = agent.search_opportunities("master scholarship germany")

    assert outcome["ok"] is False
    assert "quota" in outcome["error"].lower()
    assert "tavily.com" in outcome["error"]


def test_an_error_string_payload_is_also_treated_as_a_failure():
    """Tavily sometimes returns the error in the payload rather than raising."""
    with pytest.raises(agent.SearchUnavailable) as excinfo:
        agent._clean_results("This request exceeds your plan's set usage limit.")
    assert "quota" in str(excinfo.value).lower()


def test_missing_key_reports_clearly(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    outcome = agent.search_opportunities("master scholarship germany")
    assert outcome["ok"] is False
    assert "TAVILY_API_KEY" in outcome["error"]
    assert ".env" in outcome["error"]


def test_zero_results_tells_the_agent_not_to_invent(monkeypatch):
    """The no-results path must actively discourage fabrication."""
    monkeypatch.setattr(
        agent, "search_opportunities", lambda q, **kw: {"ok": True, "results": [], "count": 0}
    )
    text = agent.search_scholarships.invoke({"query": "something with no hits"})
    assert "Do not invent" in text
