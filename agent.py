"""Scholar Hunter — the LangChain agent: LLM, tools, and system prompt.

Importable, so app.py reuses this exact agent instead of duplicating logic.
Also runnable on its own for a quick check without the browser:

    python agent.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_core.tools import tool
from pydantic import BaseModel, Field

# Load .env from the project root so both `python agent.py` and `python app.py`
# see the same keys. Existing environment variables win over the file.
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The Claude model, kept as a constant so it is a one-line change.
MODEL_NAME = "claude-sonnet-4-6"

# Keep responses roomy enough for a full shortlist without risking a timeout.
MAX_TOKENS = 8000

# Opportunity portals and official funders we prefer. Tavily is asked to favour
# these, but other credible results are still allowed through — an official
# university programme page is often the best source and is not on this list.
TRUSTED_DOMAINS = [
    "mastersportal.com",
    "mastersportal.eu",
    "scholarshipportal.com",
    "phdportal.com",
    "erasmus-plus.ec.europa.eu",
    "eacea.ec.europa.eu",
    "daad.de",
    "funding-guide.de",
    "chevening.org",
    "fulbright.org",
    "fulbrightonline.org",
    "britishcouncil.org",
    "studyinnorway.no",
    "studyineurope.eu",
    "campusfrance.org",
    "scholars4dev.com",
    "wemakescholars.com",
]

# How many raw results to pull back per query.
SEARCH_RESULTS = 8


class MissingKeyError(RuntimeError):
    """Raised when a required API key is absent.

    Carries a human-readable message so the Flask layer can return clean JSON
    instead of leaking a stack trace at the user.
    """


def _require_key(name: str, where: str) -> str:
    """Fetch an API key or fail with an instruction, not a traceback."""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise MissingKeyError(
            f"{name} is not set. Add it to your .env file in the project root "
            f"(copy .env.example to .env). Get a key at {where}."
        )
    return value


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def build_llm(temperature: float = 0.0, **kwargs) -> ChatAnthropic:
    """Build the Claude chat model used by every tool and by the agent itself.

    temperature defaults to 0 because eligibility reasoning should be stable and
    repeatable; this is a matching task, not a creative one.
    """
    api_key = _require_key("ANTHROPIC_API_KEY", "https://console.anthropic.com")
    return ChatAnthropic(
        model=MODEL_NAME,
        api_key=api_key,
        temperature=temperature,
        max_tokens=kwargs.pop("max_tokens", MAX_TOKENS),
        timeout=kwargs.pop("timeout", 120),
        **kwargs,
    )


def check_llm() -> dict:
    """Smoke-test the Claude connection. Returns a result dict, never raises.

    Used by the Phase 1 test and by the startup banner so a bad key shows up as
    a clear message rather than an exception mid-search.
    """
    try:
        llm = build_llm()
        reply = llm.invoke("Reply with exactly: OK")
        return {"ok": True, "model": MODEL_NAME, "reply": reply.content}
    except MissingKeyError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # network, auth, rate limit — report, don't crash
        return {"ok": False, "error": f"Could not reach Claude ({MODEL_NAME}): {exc}"}


# ---------------------------------------------------------------------------
# Tool 1: web search
# ---------------------------------------------------------------------------


def _tavily(max_results: int = SEARCH_RESULTS, trusted_only: bool = False):
    """Build the Tavily search tool. Raises MissingKeyError if the key is absent.

    Uses langchain-community's TavilySearchResults as the spec requires. It logs a
    deprecation warning on the 0.3 line (the successor lives in langchain-tavily);
    it works, and swapping it is a one-line change here.
    """
    api_key = _require_key("TAVILY_API_KEY", "https://tavily.com (free tier available)")
    # TavilySearchResults reads the key from the environment, so set it for this
    # process rather than passing it positionally (the arg name has moved between
    # langchain-community versions).
    os.environ["TAVILY_API_KEY"] = api_key

    kwargs = {
        "max_results": max_results,
        "search_depth": "advanced",  # better snippets — we extract requirements from them
        "include_answer": False,
        "include_raw_content": False,
    }
    if trusted_only:
        kwargs["include_domains"] = TRUSTED_DOMAINS
    return TavilySearchResults(**kwargs)


def _clean_results(raw) -> list[dict]:
    """Normalise Tavily output into {title, url, snippet, trusted} dicts.

    Tavily returns a list of dicts on success, but a plain string when something
    went wrong upstream — handle both rather than assuming.
    """
    if isinstance(raw, str) or not isinstance(raw, list):
        return []

    cleaned = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url.startswith("http"):
            continue  # no source URL means we cannot cite it, so we drop it
        cleaned.append(
            {
                "title": (item.get("title") or "").strip() or url,
                "url": url,
                "snippet": (item.get("content") or "").strip(),
                "trusted": any(domain in url for domain in TRUSTED_DOMAINS),
            }
        )
    return cleaned


def search_opportunities(query: str, max_results: int = SEARCH_RESULTS) -> dict:
    """Search the web for opportunities. Plain function so tests can call it directly.

    Runs a trusted-portal-only pass first, then a general pass, and merges them
    with trusted sources ranked first. Returns a dict; never raises.
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "Search query was empty.", "results": []}

    try:
        trusted = _clean_results(_tavily(max_results, trusted_only=True).invoke(query))
    except MissingKeyError as exc:
        return {"ok": False, "error": str(exc), "results": []}
    except Exception:
        trusted = []  # a failed narrow pass must not sink the general one

    try:
        general = _clean_results(_tavily(max_results, trusted_only=False).invoke(query))
    except MissingKeyError as exc:
        return {"ok": False, "error": str(exc), "results": []}
    except Exception as exc:
        if not trusted:
            return {"ok": False, "error": f"Web search failed: {exc}", "results": []}
        general = []

    # Merge, de-duplicating on URL and keeping trusted sources at the top.
    merged: dict[str, dict] = {}
    for item in trusted + general:
        merged.setdefault(item["url"], item)
    results = sorted(merged.values(), key=lambda r: not r["trusted"])[:max_results]

    return {
        "ok": True,
        "query": query,
        "count": len(results),
        "results": results,
        # An honest empty result is a valid answer — the agent must say so rather
        # than inventing opportunities to fill the gap.
        "note": "No results found for this query." if not results else "",
    }


class SearchInput(BaseModel):
    query: str = Field(
        description=(
            "A web search query for scholarships, grants, master's programmes or "
            "workshops. Include field of study, degree level, and country when known, "
            "e.g. 'Master's scholarship data science Germany for Egyptian students 2026'."
        )
    )


@tool("search_scholarships", args_schema=SearchInput)
def search_scholarships(query: str) -> str:
    """Search the web for scholarships, grants, master's programmes and workshops.

    Prefers trusted opportunity portals and official funders (Mastersportal,
    Erasmus+, DAAD, Chevening, Fulbright, official university pages) but also
    returns other credible results. Returns titles, snippets and source URLs.
    Always cite the returned URL — never state an opportunity without one.
    """
    outcome = search_opportunities(query)
    if not outcome["ok"]:
        return f"SEARCH FAILED: {outcome['error']}"
    if not outcome["results"]:
        return (
            f"No results found for '{query}'. Do not invent opportunities — "
            "either try a different query or report that nothing was found."
        )

    lines = [f"{outcome['count']} result(s) for '{query}':"]
    for i, r in enumerate(outcome["results"], 1):
        tag = " [trusted portal]" if r["trusted"] else ""
        lines.append(f"\n{i}. {r['title']}{tag}\n   URL: {r['url']}\n   {r['snippet'][:600]}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(f"Scholar Hunter — checking Claude connection ({MODEL_NAME})...")
    result = check_llm()
    print("  OK:", result["reply"]) if result["ok"] else print("  FAILED:", result["error"])
