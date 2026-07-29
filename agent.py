"""Scholar Hunter — the LangChain agent: LLM, tools, and system prompt.

Importable, so app.py reuses this exact agent instead of duplicating logic.
Also runnable on its own for a quick check without the browser:

    python agent.py
"""

from __future__ import annotations

import json
import os
import re
from datetime import date as _date
from datetime import datetime as _datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from pydantic import BaseModel, Field

# Load .env from the project root so both `python agent.py` and `python app.py`
# see the same keys. Existing environment variables win over the file.
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The Claude model, kept as a constant so it is a one-line change.
#
# Routed through the iHQ LiteLLM proxy, so model ids carry a provider prefix
# ("anthropic/..."). Ask the proxy for the full list with:
#     curl https://litellm.i-hq.tech/v1/models -H "Authorization: Bearer $LITELLM_API_KEY"
#
# Budget note: keys carry a $3 lifetime cap. A full search is ~25 model calls;
# on sonnet-4-6 that is roughly $0.40, on claude-haiku-4-5 roughly $0.14. Switch
# this constant to "anthropic/claude-haiku-4-5" to make the budget stretch about
# three times further, at some cost to the eligibility reasoning.
MODEL_NAME = "anthropic/claude-sonnet-4-6"

# The OpenAI-compatible endpoint in front of the models.
LITELLM_BASE_URL = "https://litellm.i-hq.tech/v1"

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

# Social networks, forums and video sites. A post about a scholarship is not the
# scholarship, and its "requirements" cannot be trusted or cited.
BLOCKED_DOMAINS = [
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "reddit.com",
    "quora.com",
    "pinterest.com",
    "linkedin.com/posts",
    "t.me",
]


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


def llm_key() -> tuple[str, bool]:
    """Return (key, use_proxy) for whichever credential is configured.

    Two setups are supported, because the same project is run both ways:
      * an iHQ LiteLLM proxy key (`LITELLM_API_KEY`) — the shared server
      * a direct Anthropic key (`ANTHROPIC_API_KEY`, the `sk-ant-` form)

    A key sitting in ANTHROPIC_API_KEY that is *not* an `sk-ant-` key is
    treated as a proxy key rather than rejected: that is exactly how the
    LiteLLM key tends to arrive, and failing on it produces a confusing
    "your key was rejected" when the key is fine and only the route is wrong.
    """
    proxy = (os.getenv("LITELLM_API_KEY") or "").strip()
    if proxy:
        return proxy, True

    anthropic_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if anthropic_key:
        return anthropic_key, not anthropic_key.startswith("sk-ant-")

    raise MissingKeyError(
        "No model key is set. Add LITELLM_API_KEY (the iHQ proxy key) — or "
        "ANTHROPIC_API_KEY for a direct Anthropic key — to your .env file in "
        "the project root. Copy .env.example to .env to start."
    )


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def build_llm(temperature: float = 0.0, **kwargs):
    """Build the chat model used by every tool and by the agent itself.

    Goes through the iHQ LiteLLM proxy (OpenAI-compatible) when a proxy key is
    configured, and straight to Anthropic when a real `sk-ant-` key is. Both
    return a LangChain chat model with the same interface, so nothing downstream
    knows or cares which route it got.

    temperature defaults to 0 because eligibility reasoning should be stable and
    repeatable; this is a matching task, not a creative one.
    """
    api_key, use_proxy = llm_key()
    max_tokens = kwargs.pop("max_tokens", MAX_TOKENS)
    timeout = kwargs.pop("timeout", 120)

    if use_proxy:
        return ChatOpenAI(
            model=MODEL_NAME,
            api_key=api_key,
            base_url=LITELLM_BASE_URL,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            **kwargs,
        )

    # Direct Anthropic takes the bare model id, without the proxy's prefix.
    return ChatAnthropic(
        model=MODEL_NAME.split("/", 1)[-1],
        api_key=api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
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


class SearchUnavailable(RuntimeError):
    """The search backend answered, but with a failure rather than results."""


def _explain_search_error(text: str) -> str:
    """Turn a Tavily failure into something a student can act on.

    Handles both shapes it arrives in: an error string in the payload, and an
    HTTPError raised by the client. 432 is Tavily's "plan usage limit reached".
    """
    lowered = text.lower()
    if "usage limit" in lowered or "exceeds your plan" in lowered or "432" in text:
        return (
            "Your Tavily search quota is used up, so no search could run. Wait for "
            "it to reset or upgrade at tavily.com — the free tier is capped."
        )
    if "unauthorized" in lowered or "invalid api key" in lowered or "401" in text:
        return "Your TAVILY_API_KEY was rejected. Check the value in your .env file."
    if "rate limit" in lowered or "429" in text:
        return "Tavily is rate-limiting requests. Wait a moment and try again."
    return f"Web search failed: {text[:200]}"


def _clean_results(raw) -> list[dict]:
    """Normalise Tavily output into {title, url, snippet, trusted} dicts.

    Tavily returns a list of dicts on success but a plain string when something
    went wrong upstream — an exhausted quota, a bad key. Treating that string as
    "no results" is how a billing problem gets reported to the student as
    "nothing matched your profile", so raise instead.
    """
    if isinstance(raw, str):
        raise SearchUnavailable(_explain_search_error(raw))
    if not isinstance(raw, list):
        return []

    cleaned = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = (item.get("url") or "").strip()
        if not url.startswith("http"):
            continue  # no source URL means we cannot cite it, so we drop it
        if any(blocked in url.lower() for blocked in BLOCKED_DOMAINS):
            continue  # a social post about a scholarship is not the scholarship
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

    unavailable = ""  # a backend failure, kept so it can be reported honestly

    try:
        trusted = _clean_results(_tavily(max_results, trusted_only=True).invoke(query))
    except MissingKeyError as exc:
        return {"ok": False, "error": str(exc), "results": []}
    except SearchUnavailable as exc:
        trusted, unavailable = [], str(exc)
    except Exception as exc:
        # A failed narrow pass must not sink the general one, but remember why
        # in case that fails too.
        trusted, unavailable = [], _explain_search_error(str(exc))

    try:
        general = _clean_results(_tavily(max_results, trusted_only=False).invoke(query))
    except MissingKeyError as exc:
        return {"ok": False, "error": str(exc), "results": []}
    except SearchUnavailable as exc:
        general, unavailable = [], str(exc)
    except Exception as exc:
        if not trusted:
            return {"ok": False, "error": _explain_search_error(str(exc)), "results": []}
        general = []

    # Quota exhausted or key rejected with nothing to show: say so, rather than
    # letting it read as "we searched and found nothing for you".
    if unavailable and not trusted and not general:
        return {"ok": False, "error": unavailable, "results": []}

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


# ---------------------------------------------------------------------------
# Shared helper: ask Claude for JSON and get a dict back
# ---------------------------------------------------------------------------

# The requirement fields we try to pull from every opportunity page. Anything we
# cannot find stays "not stated" — the agent must flag gaps, not fill them.
REQUIREMENT_FIELDS = [
    "minimum_gpa",
    "degree_level",
    "eligible_nationalities",
    "language_requirement",
    "field_of_study",
    "deadline",
    "funding_scope",
    "required_courses",
    "required_documents",
]

# Only these are *criteria the student is judged against*. `deadline` is checked
# separately (has it passed?) and `funding_scope` is descriptive — scoring either
# against the student produces meaningless "not stated" rows and would drag every
# verdict down to unclear.
ELIGIBILITY_FIELDS = [
    "minimum_gpa",
    "degree_level",
    "eligible_nationalities",
    "language_requirement",
    "field_of_study",
    "required_courses",
]

NOT_STATED = "not stated"


def _explain_model_error(exc: Exception) -> str:
    """Turn a raw API exception into something a student can act on."""
    text = str(exc)
    if "credit balance is too low" in text:
        return (
            "Your Anthropic credit balance is too low to run a search. Top it up at "
            "console.anthropic.com (Plans & Billing) and try again."
        )
    if "rate_limit" in text or "429" in text:
        return "The Anthropic API is rate-limiting requests. Wait a moment and try again."
    if "authentication" in text.lower() or "401" in text:
        return "Your ANTHROPIC_API_KEY was rejected. Check the value in your .env file."
    return f"The language model could not be reached: {text}"


def _as_text(content) -> str:
    """Flatten a Claude response into a plain string.

    Claude returns a list of content blocks (text, thinking, tool_use) rather than
    a bare string, so anything downstream — the Flask JSON, the CLI, the tests —
    gets a list unless it is flattened here.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content or "")


def _ask_json(prompt: str, *, fallback: dict) -> dict:
    """Ask Claude for a JSON object and parse it. Returns `fallback` on failure.

    The model occasionally wraps JSON in prose or a code fence, so we take the
    outermost {...} rather than trusting the whole string to parse.
    """
    try:
        raw = _as_text(build_llm().invoke(prompt).content)
    except MissingKeyError as exc:
        # `model_error` marks "the model never answered" as distinct from "the
        # model answered and the page said nothing". Without it, an outage or an
        # empty credit balance degrades into an empty shortlist that blames the
        # student's search terms.
        return {**fallback, "error": str(exc), "model_error": True}
    except Exception as exc:
        return {**fallback, "error": _explain_model_error(exc), "model_error": True}

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {**fallback, "error": "Model did not return JSON."}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return {**fallback, "error": f"Could not parse model JSON: {exc}"}
    return parsed if isinstance(parsed, dict) else {**fallback, "error": "Expected a JSON object."}


# ---------------------------------------------------------------------------
# Tool 2: requirement extraction
# ---------------------------------------------------------------------------


# How much of an opportunity page we keep and how much reaches the model.
#
# These were 12,000 / 10,000, which was too tight: on real DAAD pages the
# application-documents list sits at roughly characters 10,000-13,000, so it was
# cut off and the tool reported "this page does not list the documents required"
# about pages that plainly did. Eligibility text appears early, but documents and
# deadlines live near the bottom — the part that was being thrown away.
PAGE_FETCH_CHARS = 30000
PAGE_MODEL_CHARS = 20000


def fetch_page_text(url: str, max_chars: int = PAGE_FETCH_CHARS) -> dict:
    """Download an opportunity page and return its visible text.

    Kept separate from extraction so a fetch failure is distinguishable from an
    extraction failure — "the page would not load" and "the page did not say" are
    different answers and the student deserves the honest one.
    """
    try:
        response = requests.get(
            url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ScholarHunter/1.0)"},
        )
        response.raise_for_status()
    except Exception as exc:
        return {"ok": False, "error": f"Could not fetch {url}: {exc}", "text": ""}

    soup = BeautifulSoup(response.text, "html.parser")

    # Collect real "apply here" links from the page's own anchors before the
    # markup is thrown away. Taking the URL from the page means it can never be
    # invented — the model only ever picks from what is actually there.
    apply_links = _apply_links(soup, url)

    for noise in soup(["script", "style", "nav", "footer", "header", "form", "noscript"]):
        noise.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return {
        "ok": True,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
        "apply_links": apply_links,
    }


# Words that mark a link as the place you actually apply, rather than more prose.
_APPLY_WORDS = (
    "apply", "application", "portal", "register", "submit", "bewerbung",
    "online-bewerbung", "how to apply", "start your application",
)


def _apply_links(soup, page_url: str, limit: int = 8) -> list[dict]:
    """Anchors on the page that look like the actual application entry point."""
    from urllib.parse import urljoin

    found: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        label = " ".join(anchor.get_text(" ", strip=True).split())[:120]
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "javascript:")):
            continue

        haystack = f"{label} {href}".lower()
        if not any(word in haystack for word in _APPLY_WORDS):
            continue

        absolute = urljoin(page_url, href)
        if absolute.startswith("http") and absolute not in found:
            found[absolute] = label or absolute
        if len(found) >= limit:
            break
    return [{"label": text, "url": link} for link, text in found.items()]


def _verified_apply_url(candidate, apply_links: list[dict]) -> str:
    """Accept an application URL only if the page actually carried it.

    Sending a student to a hallucinated application portal is worse than sending
    them nowhere, so the model's answer is checked against the anchors we
    scraped rather than trusted.
    """
    candidate = str(candidate or "").strip()
    if not candidate or candidate == NOT_STATED:
        return NOT_STATED
    real = {link["url"] for link in apply_links}
    return candidate if candidate in real else NOT_STATED


def extract_requirements_from(source: str, page_text: str = "") -> dict:
    """Pull structured requirements out of an opportunity page or snippet.

    `source` is a URL (which we fetch) or raw text. Fields that the page does not
    state come back as "not stated" rather than being guessed.
    """
    source = (source or "").strip()
    if not source and not page_text:
        return {"ok": False, "error": "No URL or text supplied."}

    url = source if source.lower().startswith("http") else ""
    text = page_text
    fetch_note = ""
    apply_links: list[dict] = []

    if url and not text:
        fetched = fetch_page_text(url)
        if fetched["ok"]:
            text = fetched["text"]
            apply_links = fetched.get("apply_links", [])
        else:
            fetch_note = fetched["error"]
    if not text:
        text = source if not url else ""

    if not text.strip():
        # Nothing to read: report every field as unknown instead of inventing.
        return {
            "ok": True,
            "url": url,
            "requirements": {f: NOT_STATED for f in REQUIREMENT_FIELDS},
            "note": fetch_note or "No readable page content.",
        }

    blank = {f: NOT_STATED for f in REQUIREMENT_FIELDS}
    prompt = f"""You are extracting the stated requirements of a scholarship, grant, master's programme or workshop.

Return ONLY a JSON object with exactly these keys:
  minimum_gpa, degree_level, eligible_nationalities, language_requirement,
  field_of_study, deadline, funding_scope, required_courses, required_documents,
  opportunity_name, opportunity_type, institution, is_single_opportunity,
  application_url, application_method

FIRST decide "is_single_opportunity" (true/false):
- true  = this page describes ONE specific named opportunity a student can apply to.
- false = this page is a LIST, directory, search-results page, blog round-up or
  news post covering many opportunities (titles like "93 Scholarships in Germany",
  "Top 10 Fully Funded Masters", "Browse scholarships"), or an index page.
  A student cannot apply to a list, so this distinction matters.
If it is false, still fill in what the page states, but do not stitch together
requirements belonging to different opportunities — set those to "{NOT_STATED}".

Rules — these matter more than completeness:
- Use ONLY what the text below actually states. Never infer, never guess, never
  use outside knowledge about this programme.
- If the text does not state a field, set it to exactly "{NOT_STATED}".
- "required_courses" is a LIST of prerequisite/required course names the applicant
  must already have studied. Use [] if the text names none.
- "application_url" is the link where the applicant actually APPLIES — an online
  portal, an application form, a "apply now" link. It is often different from the
  page you are reading, which usually just describes the award. Give the full URL
  if the text contains one, otherwise "{NOT_STATED}". Never invent a URL.
- "application_method" is one of: "online portal", "email", "postal", or
  "{NOT_STATED}" — how the application is actually submitted, if the page says.
- "required_documents" is a LIST of documents the applicant must SUBMIT with the
  application (e.g. "Motivation letter (max 2 pages)", "CV in Europass format",
  "Two academic letters of recommendation", "Certified transcript of records",
  "IELTS or TOEFL certificate", "Copy of passport"). Keep any stated length,
  format or certification detail as part of the entry — that detail is what an
  applicant actually needs. Use [] if the text names none.
- "opportunity_type" must be one of: "scholarship", "master's programme",
  "workshop", "grant", or "{NOT_STATED}".
- "deadline" should be the application deadline as written (e.g. "15 January 2026").
- "funding_scope" is what the money covers (e.g. "full tuition + 992 EUR/month stipend").

LINKS FOUND ON THIS PAGE THAT MAY BE THE APPLICATION ENTRY POINT:
{json.dumps(apply_links, indent=2, ensure_ascii=False) if apply_links else "(none found)"}
For "application_url", choose the most likely one from THIS LIST, copied exactly.
If none of them is the place you apply, use "{NOT_STATED}". Do not write a URL
that is not in the list.

TEXT:
\"\"\"
{text[:PAGE_MODEL_CHARS]}
\"\"\"
"""
    parsed = _ask_json(prompt, fallback={"requirements": blank})

    requirements = {f: parsed.get(f, NOT_STATED) or NOT_STATED for f in REQUIREMENT_FIELDS}
    # Normalise the list-valued fields: the model sometimes returns a bare string.
    for list_field in ("required_courses", "required_documents"):
        value = requirements[list_field]
        if isinstance(value, str):
            requirements[list_field] = [] if value == NOT_STATED else [value]
        elif not isinstance(value, list):
            requirements[list_field] = []

    return {
        "ok": True,
        "url": url,
        "name": parsed.get("opportunity_name", NOT_STATED),
        "type": parsed.get("opportunity_type", NOT_STATED),
        "institution": parsed.get("institution", NOT_STATED),
        # Default to False: if the model did not confirm this is one specific
        # opportunity, treat it as a listing rather than assume it is applicable.
        "is_single_opportunity": parsed.get("is_single_opportunity") is True,
        # Only honour an application URL that really appeared on the page.
        "application_url": _verified_apply_url(parsed.get("application_url"), apply_links),
        "application_method": parsed.get("application_method", NOT_STATED),
        "apply_links": apply_links,
        "requirements": requirements,
        "note": parsed.get("error", fetch_note),
        "model_error": parsed.get("model_error", False),
    }


class ExtractInput(BaseModel):
    scholarship_url_or_text: str = Field(
        description="The opportunity's page URL (preferred — it will be fetched), "
        "or the raw text/snippet describing it."
    )


@tool("extract_requirements", args_schema=ExtractInput)
def extract_requirements(scholarship_url_or_text: str) -> str:
    """Read an opportunity's own page and extract its actual stated requirements.

    Returns minimum GPA, required degree level, eligible nationalities, language
    requirement, field of study, deadline, funding scope, and any required or
    prerequisite courses. Anything the page does not state comes back as
    "not stated" — never assume a requirement that is not written down.
    """
    outcome = extract_requirements_from(scholarship_url_or_text)
    if not outcome.get("ok"):
        return f"EXTRACTION FAILED: {outcome.get('error')}"
    return json.dumps(outcome, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 3: eligibility check
# ---------------------------------------------------------------------------

VERDICTS = {"eligible", "not_eligible", "unclear"}


def _is_field_of_study(row: dict) -> bool:
    """True for the 'field of study' row, which on its own proves nothing."""
    label = str(row.get("requirement", "")).strip().lower()
    return "field" in label and "study" in label


def _as_dict(value, what: str) -> dict:
    """Accept either a dict or a JSON string — the agent passes both."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {what: value}
        except json.JSONDecodeError:
            return {what: value}
    return {}


def check_eligibility_for(requirements, student_profile) -> dict:
    """Compare extracted requirements against the profile, requirement by requirement.

    Returns an overall verdict plus a per-requirement breakdown. "not stated" is a
    first-class outcome: an unstated requirement can never make a student eligible,
    it makes the verdict unclear.
    """
    requirements = _as_dict(requirements, "requirements")
    profile = _as_dict(student_profile, "profile")

    if not requirements:
        return {"ok": False, "error": "No requirements supplied to check against."}
    if not profile:
        return {"ok": False, "error": "No student profile supplied."}

    # The page may hand us the whole extraction envelope; unwrap it.
    if "requirements" in requirements and isinstance(requirements["requirements"], dict):
        requirements = requirements["requirements"]

    # Score only genuine criteria. The deadline is judged on expiry and the funding
    # scope is descriptive, so neither belongs in the met/not-met breakdown.
    criteria = {f: requirements.get(f, NOT_STATED) for f in ELIGIBILITY_FIELDS}
    deadline = requirements.get("deadline", NOT_STATED)
    today = _date.today().isoformat()

    prompt = f"""You are checking one student against one opportunity's stated eligibility criteria.

TODAY'S DATE: {today}

STUDENT PROFILE:
{json.dumps(profile, indent=2, ensure_ascii=False)}

OPPORTUNITY ELIGIBILITY CRITERIA (extracted from its own page):
{json.dumps(criteria, indent=2, ensure_ascii=False)}

APPLICATION DEADLINE (as written on the page): {deadline}

Return ONLY a JSON object:
{{
  "verdict": "eligible" | "not_eligible" | "unclear",
  "reason": "<one sentence explaining the verdict>",
  "fit": "<one sentence on why this opportunity suits THIS student — their field, "
         "level and goals. Be concrete and honest; if the fit is weak, say so.>",
  "deadline_status": "open" | "expired" | "not stated",
  "breakdown": [
    {{"requirement": "<e.g. Minimum GPA>",
      "required": "<what the opportunity asks, verbatim>",
      "student": "<the student's corresponding value, or 'not provided'>",
      "status": "met" | "not_met" | "not_stated",
      "note": "<short explanation>"}}
  ]
}}

Rules — follow these exactly:
- Judge ONLY against the criteria above. Do not import outside knowledge about
  this programme.
- Include one breakdown entry per criterion, EXCEPT: skip any criterion whose
  required value is "not stated" AND which the student also has no value for —
  that row carries no information.
- If the criterion is stated but the student did not supply the matching detail
  (e.g. an IELTS score), status is "not_stated", not "met" and not "not_met".
- "degree_level" means THE DEGREE THE APPLICANT MUST ALREADY HOLD. Do not confuse
  it with the level of study being funded. A programme described as "graduate
  funding" or "for graduate students" is a MASTER'S-LEVEL PROGRAMME — a student
  holding a Bachelor's is eligible to apply for it. Only mark degree_level
  "not_met" when the page explicitly requires an already-completed degree the
  student does not hold (e.g. "applicants must already hold a Master's degree"
  and the student holds only a Bachelor's).
- "deadline_status": "expired" only if the deadline is clearly before today's date
  ({today}); "open" if it is on or after today; "not stated" if absent or the year
  is missing.
- Verdict "not_eligible" if ANY criterion is clearly "not_met", or the deadline
  has expired.
- Verdict "eligible" ONLY if the page states real criteria AND every one of them
  is "met" AND the deadline is not expired.
- If the page states NO usable criteria at all, the verdict is "unclear", never
  "eligible". Silence is not permission — a page that says nothing cannot confirm
  that this student qualifies.
- Otherwise "unclear". Never upgrade "unclear" to "eligible" to be encouraging —
  overselling a match is a failure; saying "we could not confirm X" is a success.
"""
    parsed = _ask_json(prompt, fallback={"verdict": "unclear", "breakdown": []})

    verdict = str(parsed.get("verdict", "unclear")).strip().lower()
    if verdict not in VERDICTS:
        verdict = "unclear"

    breakdown = [b for b in parsed.get("breakdown", []) if isinstance(b, dict)]
    for row in breakdown:
        if row.get("status") not in {"met", "not_met", "not_stated"}:
            row["status"] = "not_stated"

    deadline_status = str(parsed.get("deadline_status", NOT_STATED)).strip().lower()
    if deadline_status not in {"open", "expired", NOT_STATED}:
        deadline_status = NOT_STATED

    # Defensive floor: an "eligible" claim cannot stand next to a failed row or an
    # expired deadline. Cheap to enforce here, expensive to get wrong.
    if verdict == "eligible" and (
        any(r["status"] == "not_met" for r in breakdown) or deadline_status == "expired"
    ):
        verdict = "not_eligible"

    # Silence is not permission. A page that states nothing checkable cannot make
    # a student eligible, however tempting the vacuous "nothing was unmet" logic is.
    #
    # Matching the field of study alone is not enough either: "Department of
    # Computer Science" tells us the subject and nothing about whether THIS student
    # qualifies. Claiming "eligible" on that basis is exactly the overselling the
    # reliability rules forbid, so an eligible verdict has to rest on at least one
    # criterion that actually discriminates between applicants.
    if verdict == "eligible" and not any(
        row["status"] == "met" and not _is_field_of_study(row) for row in breakdown
    ):
        verdict = "unclear"

    return {
        "ok": True,
        "verdict": verdict,
        "reason": parsed.get("reason", ""),
        "fit": parsed.get("fit", ""),
        "deadline_status": deadline_status,
        "breakdown": breakdown,
        "note": parsed.get("error", ""),
    }


class EligibilityInput(BaseModel):
    requirements: str = Field(
        description="The opportunity's extracted requirements, as a JSON object string "
        "(the output of extract_requirements)."
    )
    student_profile: str = Field(
        description="The student's profile as a JSON object string: field_of_study, "
        "degree_level, gpa, nationality, interests, and optionally courses."
    )


@tool("check_eligibility", args_schema=EligibilityInput)
def check_eligibility(requirements: str, student_profile: str) -> str:
    """Compare an opportunity's extracted requirements to the student, one by one.

    Returns an overall verdict (eligible / not_eligible / unclear) plus a
    per-requirement breakdown marking each met, not met, or not stated, with a
    short reason. Flags missing information instead of assuming the student
    qualifies.
    """
    outcome = check_eligibility_for(requirements, student_profile)
    if not outcome.get("ok"):
        return f"ELIGIBILITY CHECK FAILED: {outcome.get('error')}"
    return json.dumps(outcome, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 4: course matching (description-based, not title-based)
# ---------------------------------------------------------------------------

NOT_ASSESSED = {
    "ok": True,
    "assessed": False,
    "summary": "not assessed",
    "matches": [],
}


def _as_list(value) -> list:
    """Accept a list, a JSON array string, or a newline/comma separated string."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if not isinstance(value, str) or not value.strip():
        return []
    text = value.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    except json.JSONDecodeError:
        pass
    separator = "\n" if "\n" in text else ","
    return [part.strip() for part in text.split(separator) if part.strip()]


def match_courses_for(student_courses, required_courses) -> dict:
    """Match the student's courses to a programme's required courses by content.

    Comparison is on description and subject matter, not title, so "Intro to AI"
    and "Foundations of Machine Intelligence" match when they cover the same
    ground. Returns "not assessed" cleanly when either side is empty.
    """
    student = _as_list(student_courses)
    required = _as_list(required_courses)

    if not student:
        return {**NOT_ASSESSED, "reason": "The student did not provide their courses."}
    if not required:
        return {**NOT_ASSESSED, "reason": "The programme does not list required courses."}

    # If the student gave bare names with no descriptions, matching is weaker and
    # we must say so rather than quietly reporting a confident match.
    has_descriptions = any(
        len(course) > 60 or any(sep in course for sep in (":", " - ", " — "))
        for course in student
    )
    description_hint = (
        "Descriptions were provided."
        if has_descriptions
        else "Descriptions were NOT provided — you have only course names. Because you "
        "are matching on names and topic alone, cap every confidence at 'medium' or 'low'."
    )

    prompt = f"""You are matching a student's completed undergraduate courses against a programme's required/prerequisite courses.

STUDENT'S COMPLETED COURSES:
{json.dumps(student, indent=2, ensure_ascii=False)}

PROGRAMME'S REQUIRED COURSES:
{json.dumps(required, indent=2, ensure_ascii=False)}

Return ONLY a JSON object:
{{
  "matches": [
    {{"required_course": "<the required course>",
      "status": "matched" | "partially_matched" | "missing",
      "student_course": "<the student course that satisfies it, or null>",
      "confidence": "high" | "medium" | "low",
      "note": "<why they do or do not correspond>"}}
  ]
}}

Rules:
- Match on COURSE CONTENT AND DESCRIPTION, not on the title. Equivalent courses
  carry different names at different universities: "Intro to AI" and "Foundations
  of Machine Intelligence" are the same course; match them.
- "partially_matched" when the student's course covers some but not all of the
  required material.
- "missing" when nothing the student studied covers the requirement. Do not
  stretch a loose thematic connection into a match.
- One entry per required course, in the order given.
- {description_hint}
"""
    parsed = _ask_json(prompt, fallback={"matches": []})

    matches = []
    for row in parsed.get("matches", []):
        if not isinstance(row, dict):
            continue
        status = row.get("status")
        if status not in {"matched", "partially_matched", "missing"}:
            status = "missing"
        confidence = row.get("confidence", "low")
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        if not has_descriptions and confidence == "high":
            confidence = "medium"  # names only — never claim high confidence
        matches.append(
            {
                "required_course": row.get("required_course", ""),
                "status": status,
                "student_course": row.get("student_course"),
                "confidence": confidence,
                "note": row.get("note", ""),
            }
        )

    matched = sum(1 for m in matches if m["status"] == "matched")
    partial = sum(1 for m in matches if m["status"] == "partially_matched")
    total = len(matches)
    summary = f"{matched} of {total} required courses matched"
    if partial:
        summary += f" ({partial} partial)"

    return {
        "ok": True,
        "assessed": True,
        "summary": summary,
        "matched": matched,
        "partial": partial,
        "total": total,
        "confidence_capped": not has_descriptions,
        "matches": matches,
        "note": parsed.get("error", ""),
    }


class CourseMatchInput(BaseModel):
    student_courses: str = Field(
        description="The student's completed undergraduate courses — a JSON array "
        "string, or one course per line. Descriptions improve accuracy."
    )
    required_courses: str = Field(
        description="The programme's required/prerequisite courses — a JSON array "
        "string, or one course per line."
    )


@tool("match_courses", args_schema=CourseMatchInput)
def match_courses(student_courses: str, required_courses: str) -> str:
    """Match the student's undergraduate courses to a programme's required courses.

    Compares course descriptions and content rather than titles, so equivalent
    courses with different names across universities still count. Returns, per
    required course: matched / partially matched / missing, which student course
    satisfies it, and a confidence level (lower when only names were given).
    Returns "not assessed" if the student gave no courses or the programme lists
    none.
    """
    outcome = match_courses_for(student_courses, required_courses)
    return json.dumps(outcome, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 5: how to prepare the required documents
# ---------------------------------------------------------------------------


def document_guidance(
    documents, opportunity_name: str = "", institution: str = "", student_profile=None
) -> dict:
    """Explain how to prepare each document this opportunity asks for.

    Generated on demand — the frontend only calls this when a card is expanded,
    so a shortlist the student skims past costs nothing extra.

    The advice is tied to this opportunity and this student (their field, level
    and courses), because "write a good motivation letter" helps nobody.
    """
    docs = _as_list(documents)
    if not docs:
        return {
            "ok": True,
            "assessed": False,
            "guidance": [],
            "note": "This page does not list the documents required.",
        }

    profile = _as_dict(student_profile, "profile") if student_profile else {}

    prompt = f"""A student is preparing an application. Explain how to prepare each required document.

OPPORTUNITY: {opportunity_name or "(not named)"}{" — " + institution if institution and institution != NOT_STATED else ""}

DOCUMENTS THE APPLICATION REQUIRES:
{json.dumps(docs, indent=2, ensure_ascii=False)}

THE STUDENT:
{json.dumps(profile, indent=2, ensure_ascii=False) if profile else "(no profile given)"}

Return ONLY a JSON object:
{{
  "guidance": [
    {{"document": "<the document, copied from the list above>",
      "summary": "<one sentence on what this document must achieve here>",
      "steps": ["<concrete, specific action>", "..."],
      "watch_out": "<the mistake that most often costs applicants this document, or ''>"}}
  ]
}}

Rules:
- One entry per required document, in the same order.
- Be SPECIFIC to this opportunity and this student. Reference their actual field,
  degree level, GPA and courses where relevant. "Write clearly" is useless advice;
  "name a research group at this institute that works on your ML coursework" is not.
- Respect any stated limit (page count, word count, format, certification) and
  repeat it in the steps so the student does not have to look it up again.
- 2 to 4 steps per document. Each step an action the student can take today.
- Do NOT invent requirements the opportunity did not state. If a detail (like a
  word limit) is not given, do not make one up — say it is not specified.
"""
    parsed = _ask_json(prompt, fallback={"guidance": []})

    guidance = []
    for row in parsed.get("guidance", []):
        if not isinstance(row, dict):
            continue
        steps = [str(s).strip() for s in (row.get("steps") or []) if str(s).strip()]
        guidance.append(
            {
                "document": str(row.get("document", "")).strip(),
                "summary": str(row.get("summary", "")).strip(),
                "steps": steps[:5],
                "watch_out": str(row.get("watch_out", "")).strip(),
            }
        )

    return {
        "ok": not parsed.get("model_error", False),
        "assessed": bool(guidance),
        "guidance": guidance,
        "note": parsed.get("error", ""),
    }


class DocumentHelpInput(BaseModel):
    documents: str = Field(
        description="The documents the application requires — a JSON array string, "
        "or one per line."
    )
    opportunity_name: str = Field(default="", description="Name of the opportunity.")
    institution: str = Field(default="", description="The awarding institution.")
    student_profile: str = Field(
        default="", description="The student's profile as a JSON object string."
    )


@tool("explain_documents", args_schema=DocumentHelpInput)
def explain_documents(
    documents: str,
    opportunity_name: str = "",
    institution: str = "",
    student_profile: str = "",
) -> str:
    """Explain how to prepare each document an application requires.

    Gives per-document steps tailored to this opportunity and this student —
    what the document must achieve, concrete actions, and the mistake that most
    often costs applicants. Never invents a requirement the page did not state.
    """
    outcome = document_guidance(documents, opportunity_name, institution, student_profile)
    return json.dumps(outcome, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 6: Gmail draft (draft only — never sends)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
CREDENTIALS_FILE = PROJECT_ROOT / "credentials.json"
TOKEN_FILE = PROJECT_ROOT / "token.json"

EMAIL_UNAVAILABLE = (
    "Email drafting is not configured. Put a Google OAuth client file at "
    "credentials.json in the project root (Google Cloud → enable Gmail API → "
    "OAuth client ID → Desktop app), then run again; the first run opens a "
    "browser consent screen. Everything else — search, eligibility, deadlines — "
    "works without it."
)


def gmail_available() -> bool:
    """True when Gmail drafting could work. Cheap: no network, no OAuth."""
    return CREDENTIALS_FILE.is_file()


# Compose only. The agent holds no permission to send — a stronger guarantee
# than merely declining to.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]


def _gmail_credentials():
    """Run the standard Google desktop OAuth flow, caching the token locally.

    Deliberately does NOT use langchain-google-community's `get_gmail_credentials`
    helper. In 2.0.10 that helper misspells its own parameter
    (`client_sercret_file`) and, worse, unpacks `ServiceCredentials` from
    `google.oauth2.service_account`, where the class is actually called
    `Credentials` — so it raises on every path, service account or not. This is
    the documented flow it was wrapping anyway, and it is about ten lines.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_FILE.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)
        except Exception:
            creds = None  # a stale or hand-edited token should not be fatal

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            # Opens the consent screen in a browser on first use only.
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return creds


def create_gmail_draft(to: str, subject: str, body: str) -> dict:
    """Create a Gmail DRAFT. Never sends — that is the human-in-the-loop guarantee.

    Returns a result dict; a missing credentials file is a normal, reportable
    outcome rather than an exception, so the rest of the agent keeps working.
    """
    to, subject, body = (to or "").strip(), (subject or "").strip(), (body or "").strip()
    if not to:
        return {"ok": False, "error": "No recipient address supplied."}
    if not subject and not body:
        return {"ok": False, "error": "The email has no subject and no body."}
    if not gmail_available():
        return {"ok": False, "available": False, "error": EMAIL_UNAVAILABLE}

    try:
        from googleapiclient.discovery import build
        from langchain_google_community import GmailToolkit

        credentials = _gmail_credentials()
        toolkit = GmailToolkit(api_resource=build("gmail", "v1", credentials=credentials))

        create_draft = next(
            (t for t in toolkit.get_tools() if "draft" in t.name.lower()), None
        )
        if create_draft is None:
            return {"ok": False, "error": "The Gmail toolkit exposed no draft tool."}

        result = create_draft.invoke(
            {"to": [to], "subject": subject, "message": body}
        )
    except Exception as exc:
        return {"ok": False, "error": f"Could not create the Gmail draft: {exc}"}

    text = _as_text(result)
    draft_id = ""
    match = re.search(r"[Ii]d:?\s*([\w-]+)", text)
    if match:
        draft_id = match.group(1)

    return {
        "ok": True,
        "drafted": True,
        "sent": False,  # stated explicitly so no caller can mistake one for the other
        "to": to,
        "subject": subject,
        "draft_id": draft_id,
        "detail": text,
        "message": "Draft saved to your Gmail Drafts folder. It has NOT been sent.",
    }


class DraftEmailInput(BaseModel):
    to: str = Field(description="Recipient email address, e.g. the programme office.")
    subject: str = Field(description="Subject line.")
    body: str = Field(description="The full email body, personalised to the student.")


@tool("draft_email", args_schema=DraftEmailInput)
def draft_email(to: str, subject: str, body: str) -> str:
    """Create a Gmail DRAFT of an inquiry or application email. Never sends it.

    Only call this after the student has explicitly approved this specific email.
    The draft lands in their Gmail Drafts folder for them to review and send
    themselves. If Gmail is not configured, this reports that clearly and nothing
    else about the agent is affected.
    """
    outcome = create_gmail_draft(to, subject, body)
    if not outcome["ok"]:
        return f"DRAFT NOT CREATED: {outcome['error']}"
    return (
        f"Draft created (id: {outcome['draft_id'] or 'unknown'}) to {outcome['to']} — "
        f"subject '{outcome['subject']}'. It is saved in Gmail Drafts and has NOT been sent."
    )


# ---------------------------------------------------------------------------
# Tool 6: save a deadline
# ---------------------------------------------------------------------------

DEADLINES_FILE = PROJECT_ROOT / "deadlines.json"


def _read_deadlines(path: Path) -> list:
    """Load the store, tolerating an absent, empty, or corrupted file."""
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text() or "[]")
    except json.JSONDecodeError:
        # A half-written file must not stop the student saving a new deadline.
        return []
    return data if isinstance(data, list) else []


def save_deadline_entry(
    scholarship_name: str, deadline_date: str, url: str = "", path: Path | None = None
) -> dict:
    """Append a deadline to deadlines.json, keeping the file valid JSON.

    TODO (Google Calendar): swap this body for a Calendar API insert —
    build a service with the calendar.events scope, then
    service.events().insert(calendarId="primary", body={
        "summary": f"Deadline: {scholarship_name}",
        "start": {"date": <ISO date>}, "end": {"date": <ISO date>},
        "description": url,
    }).execute()
    It is kept local on purpose: Calendar OAuth must never block the whole agent,
    and a JSON file is a real, working store to demo against.
    """
    path = path or DEADLINES_FILE
    name = (scholarship_name or "").strip()
    when = (deadline_date or "").strip()
    if not name:
        return {"ok": False, "error": "No scholarship name supplied."}
    if not when:
        return {"ok": False, "error": "No deadline date supplied."}

    entry = {
        "scholarship_name": name,
        "deadline_date": when,
        "url": (url or "").strip(),
        "saved_at": _datetime.now().isoformat(timespec="seconds"),
    }

    entries = _read_deadlines(path)
    entries.append(entry)
    try:
        # Write via a temp file and replace, so an interrupted write cannot leave
        # a truncated deadlines.json behind.
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
        temp.replace(path)
    except Exception as exc:
        return {"ok": False, "error": f"Could not write {path.name}: {exc}"}

    return {"ok": True, "saved": True, "entry": entry, "total_saved": len(entries)}


class SaveDeadlineInput(BaseModel):
    scholarship_name: str = Field(description="Name of the scholarship or programme.")
    deadline_date: str = Field(
        description="The application deadline as stated on its page, e.g. '15 January 2027'."
    )
    url: str = Field(default="", description="Source URL for the opportunity.")


@tool("save_deadline", args_schema=SaveDeadlineInput)
def save_deadline(scholarship_name: str, deadline_date: str, url: str = "") -> str:
    """Save an opportunity's deadline as a reminder in deadlines.json.

    Only call this after the student has explicitly approved saving this deadline.
    Never save a deadline you did not actually read from the opportunity's page.
    """
    outcome = save_deadline_entry(scholarship_name, deadline_date, url)
    if not outcome["ok"]:
        return f"DEADLINE NOT SAVED: {outcome['error']}"
    entry = outcome["entry"]
    return (
        f"Saved: {entry['scholarship_name']} — deadline {entry['deadline_date']}"
        f"{' (' + entry['url'] + ')' if entry['url'] else ''}. "
        f"{outcome['total_saved']} deadline(s) now stored."
    )


# ---------------------------------------------------------------------------
# The agent: system prompt, tools, executor
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Scholar Hunter, an agent that helps graduate students find and apply for scholarships, grants, and travel funding.

Today's date is {today}. Use it to judge whether a deadline has passed.

Work in this order:

1. UNDERSTAND THE PROFILE — field of study, degree level, GPA, nationality/country,
   interests, and optionally the student's completed undergraduate courses. If a key
   field is missing, ASK the student for it before continuing rather than guessing.
   If they did not give their courses you may proceed, but note that providing
   courses improves course-matching accuracy.

2. SEARCH — use search_scholarships to find relevant scholarships, grants, master's
   programmes and workshops. Prefer trusted portals and official sources
   (Mastersportal.eu, Erasmus+/Erasmus Mundus, DAAD, Chevening, Fulbright, official
   university programme pages) while still allowing other credible results. Then, for
   each promising result, call extract_requirements on its URL to read that page's
   ACTUAL stated requirements — minimum GPA, degree level, eligible nationalities,
   language requirement, field, deadline, funding scope, and any required or
   prerequisite courses. Pull these from the opportunity's own page, never from
   assumption or memory.

3. CHECK ELIGIBILITY — call check_eligibility to compare each opportunity's extracted
   requirements against the student, requirement by requirement. When the programme
   lists required/prerequisite courses AND the student provided their undergraduate
   courses, also call match_courses: it matches by course DESCRIPTION AND CONTENT,
   not by title, so equivalent courses with different names across universities still
   count. Keep the matches; discard anything clearly ineligible or expired.

4. PRESENT A RANKED SHORTLIST — best matches first. Every item uses the SAME ~6-bullet
   format so results are easy to compare:
     1. Name & type (scholarship / master's programme / workshop) and institution
     2. Fit — one line on why it matches this student
     3. Requirements & eligibility — the key requirements, each marked met / not met / not stated
     4. Course match — e.g. "4 of 5 required courses matched", or "not assessed"
     5. Deadline & funding — the deadline (or "not stated") and what the funding covers
     6. Source — the URL

5. ACT ONLY AFTER APPROVAL — only once the student explicitly approves may you draft
   an inquiry/application email or save a deadline reminder.

RELIABILITY RULES — these matter more than being helpful or impressive:
- NEVER invent scholarships, amounts, deadlines, or requirements. Report only what
  you actually found, and always include the source URL.
- NEVER send an email or save a calendar/deadline entry without explicit user
  approval in the conversation. Drafting is not sending; you may only ever draft.
- If eligibility is unclear or information is missing, SAY SO and flag it. Do not
  oversell a match and do not fill gaps with guesses. "The page does not state a
  minimum GPA" is a better answer than a plausible number.
- When uncertain, state the uncertainty plainly.
- If a search returns nothing, say that nothing was found. Do not manufacture
  results to seem useful."""


def build_tools(include_actions: bool = True) -> list:
    """Assemble the agent's toolbox.

    `include_actions` covers the tools with side effects (email drafts, saved
    deadlines); Phase 5 adds them. Optional GIU portal/CMS tools are appended when
    those packages are present.
    """
    tools = [
        search_scholarships,
        extract_requirements,
        check_eligibility,
        match_courses,
        explain_documents,
    ]
    if include_actions:
        tools.extend(ACTION_TOOLS)

    try:
        import giu_tools

        tools.extend(giu_tools.optional_tools())
    except Exception:
        pass  # the GIU integration is optional and must never block the agent
    return tools


# Tools with side effects. Both require explicit user approval first — that rule
# lives in the system prompt, and the /search endpoint omits these entirely so the
# shortlist path cannot touch them even by accident.
ACTION_TOOLS: list = [draft_email, save_deadline]


def build_agent(verbose: bool = False, max_iterations: int = 25) -> "AgentExecutor":
    """Build the tool-calling agent plus its executor.

    Raises MissingKeyError if ANTHROPIC_API_KEY is absent — callers convert that
    into a clean message rather than letting it surface as a stack trace.
    """
    from langchain.agents import AgentExecutor, create_tool_calling_agent
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT.format(today=_date.today().isoformat())),
            MessagesPlaceholder("chat_history", optional=True),
            ("human", "{input}"),
            MessagesPlaceholder("agent_scratchpad"),
        ]
    )

    tools = build_tools()
    agent_runnable = create_tool_calling_agent(build_llm(), tools, prompt)
    return AgentExecutor(
        agent=agent_runnable,
        tools=tools,
        verbose=verbose,
        max_iterations=max_iterations,
        # Surface tool errors to the model so it can recover, rather than dying.
        handle_parsing_errors=True,
        return_intermediate_steps=True,
    )


def run_agent(user_input: str, chat_history: list | None = None, verbose: bool = False) -> dict:
    """Run one turn of the agent. Returns a result dict; never raises."""
    try:
        executor = build_agent(verbose=verbose)
    except MissingKeyError as exc:
        return {"ok": False, "error": str(exc), "output": ""}

    try:
        result = executor.invoke(
            {"input": user_input, "chat_history": chat_history or []}
        )
    except Exception as exc:
        return {"ok": False, "error": f"The agent run failed: {exc}", "output": ""}

    # Record which tools ran — the UI and the tests both want to see that the
    # agent actually searched rather than answering from memory.
    steps = result.get("intermediate_steps", []) or []
    tools_used = []
    for action, _observation in steps:
        name = getattr(action, "tool", None)
        if name and name not in tools_used:
            tools_used.append(name)

    return {
        "ok": True,
        "output": _as_text(result.get("output", "")),
        "tools_used": tools_used,
        "steps": len(steps),
    }


# ---------------------------------------------------------------------------
# The shortlist pipeline (what /search runs)
# ---------------------------------------------------------------------------

# Profile fields we need before searching is worthwhile.
REQUIRED_PROFILE_FIELDS = {
    "field_of_study": "field of study",
    "degree_level": "current degree level",
    "nationality": "country or nationality",
}

# Human labels for the requirement rows shown on a card.
REQUIREMENT_LABELS = {
    "minimum_gpa": "Minimum GPA",
    "degree_level": "Degree level",
    "eligible_nationalities": "Eligible nationalities",
    "language_requirement": "Language",
    "field_of_study": "Field of study",
    "required_courses": "Required courses",
}


def _label_key(label: str) -> str:
    """Normalise a requirement label so equivalent wordings collapse together.

    The model writes "Language Requirement" where our fallback label is
    "Language"; comparing the raw strings let both through and the card showed
    the same requirement twice.
    """
    key = re.sub(r"[^a-z]", "", str(label).lower())
    for filler in ("requirement", "minimum", "eligible", "required"):
        key = key.replace(filler, "")
    return key or "requirement"


def _already_covered(label: str, seen: set[str]) -> bool:
    """True if the eligibility breakdown already has a row for this requirement.

    Compares by containment, not equality: the model writes labels like
    "Eligible Nationalities / Groups" where our fallback label is "Eligible
    nationalities", and an exact match let both onto the card as duplicate rows.
    """
    key = _label_key(label)
    return any(key in other or other in key for other in seen if other)


def missing_profile_fields(profile: dict) -> list[str]:
    """Key fields the student has not filled in — the agent must ask before searching."""
    return [
        label
        for key, label in REQUIRED_PROFILE_FIELDS.items()
        if not str(profile.get(key, "") or "").strip()
    ]


def build_query(profile: dict) -> str:
    """Turn a profile into a search query aimed at real opportunity pages."""
    parts = []
    degree = str(profile.get("degree_level", "")).strip()
    # A Bachelor's holder wants Master's funding, not more Bachelor's funding.
    next_level = {
        "bachelor's": "Master's",
        "bachelors": "Master's",
        "master's": "PhD",
        "masters": "PhD",
    }.get(degree.lower(), degree or "graduate")

    parts.append(f"{next_level} scholarship")
    if field := str(profile.get("field_of_study", "")).strip():
        parts.append(field)
    if interests := str(profile.get("interests", "")).strip():
        parts.append(interests)
    if nationality := str(profile.get("nationality", "")).strip():
        parts.append(f"for {nationality} students")
    parts.append("eligibility requirements deadline")
    return " ".join(parts)


def _assess_one(result: dict, profile: dict, courses: list) -> dict | None:
    """Extract, check and course-match a single search result into a card.

    Returns None for anything clearly ineligible or expired — the spec says to
    discard those rather than show them.
    """
    extracted = extract_requirements_from(result["url"])
    requirements = extracted.get("requirements", {})

    # The model never answered — say so upstream rather than reporting this page
    # as "nothing found", which would blame the student's search for an outage.
    if extracted.get("model_error"):
        return {"_skipped": "model_error", "_error": extracted.get("note", "")}

    # A directory or "Top 10 scholarships" round-up is not something a student can
    # apply to, and its vague blanket criteria ("Computer Science", "Masters")
    # match almost anyone — which produced confidently wrong "eligible" cards.
    if not extracted.get("is_single_opportunity", False):
        return {"_skipped": "listing"}

    eligibility = check_eligibility_for(requirements, profile)
    if not eligibility.get("ok"):
        return None

    verdict = eligibility["verdict"]
    if verdict == "not_eligible":
        return {"_skipped": "ineligible"}  # clearly ineligible or expired

    course_match = match_courses_for(courses, requirements.get("required_courses", []))

    # Build the requirement rows, keeping the extracted values so a card shows what
    # the page actually said next to the verdict.
    rows = []
    seen = set()
    for row in eligibility.get("breakdown", []):
        label = str(row.get("requirement", "")).strip() or "Requirement"
        seen.add(_label_key(label))
        rows.append(
            {
                "label": label,
                "required": str(row.get("required", NOT_STATED)),
                "student": str(row.get("student", "not provided")),
                "status": row["status"],
                "note": str(row.get("note", "")),
            }
        )
    # Surface any stated requirement the breakdown skipped, so nothing is hidden.
    for key, label in REQUIREMENT_LABELS.items():
        value = requirements.get(key, NOT_STATED)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value) if value else NOT_STATED
        if _already_covered(label, seen) or str(value).strip().lower() in {
            NOT_STATED,
            "",
            "none",
        }:
            continue
        rows.append(
            {
                "label": label,
                "required": str(value),
                "student": "not provided",
                "status": "not_stated",
                "note": "Stated by the programme; not confirmed against your profile.",
            }
        )

    name = extracted.get("name", NOT_STATED)
    if not name or name == NOT_STATED:
        name = result["title"]

    return {
        # 1. Name & type
        "name": name,
        "type": extracted.get("type", NOT_STATED),
        "institution": extracted.get("institution", NOT_STATED),
        # 2. Fit
        "fit": eligibility.get("fit") or eligibility.get("reason", ""),
        # 3. Requirements & eligibility
        "verdict": verdict,
        "verdict_reason": eligibility.get("reason", ""),
        "requirements": rows,
        # 4. Course match
        "course_match": {
            "assessed": course_match.get("assessed", False),
            "summary": course_match.get("summary", "not assessed"),
            "matched": course_match.get("matched", 0),
            "partial": course_match.get("partial", 0),
            "total": course_match.get("total", 0),
            "confidence_capped": course_match.get("confidence_capped", False),
            "matches": course_match.get("matches", []),
        },
        # 5. Documents the application requires (guidance is fetched on demand,
        #    when the student expands the card)
        "documents": _as_list(requirements.get("required_documents", [])),
        # Where the student actually applies — verified to exist on the page.
        "application_url": extracted.get("application_url", NOT_STATED),
        "application_method": extracted.get("application_method", NOT_STATED),
        # 6. Deadline & funding
        "deadline": str(requirements.get("deadline", NOT_STATED)),
        "deadline_status": eligibility.get("deadline_status", NOT_STATED),
        "funding": str(requirements.get("funding_scope", NOT_STATED)),
        # 6. Source
        "url": result["url"],
        "trusted_source": result.get("trusted", False),
    }


def run_shortlist(profile: dict, max_results: int = 5, candidates: int = 12) -> dict:
    """Profile in, ranked uniform shortlist out. This is what POST /search runs.

    Orchestrates the same tool functions the conversational agent uses, but in a
    fixed order, so every card is guaranteed to carry the same six fields. A form
    -> cards UI has no conversation in which to recover from a malformed reply, so
    determinism matters more than flexibility here.
    """
    profile = profile or {}

    missing = missing_profile_fields(profile)
    if missing:
        return {
            "ok": False,
            "needs_profile": missing,
            "error": "Please fill in: " + ", ".join(missing) + ".",
            "results": [],
        }

    search = search_opportunities(build_query(profile), max_results=candidates)
    if not search["ok"]:
        return {"ok": False, "error": search["error"], "results": []}
    if not search["results"]:
        return {
            "ok": True,
            "results": [],
            "query": search.get("query", ""),
            "message": (
                "No opportunities were found for this profile. Nothing has been "
                "invented to fill the gap — try broadening your field or interests."
            ),
        }

    courses = _as_list(profile.get("courses", []))

    # Each candidate needs a page fetch plus two model calls, so run them in
    # parallel — serially this takes the better part of a minute.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(6, len(search["results"]))) as pool:
        assessed = list(
            pool.map(lambda r: _safe_assess(r, profile, courses), search["results"])
        )

    items = [item for item in assessed if item and "_skipped" not in item]
    skipped_listings = sum(1 for a in assessed if a and a.get("_skipped") == "listing")
    skipped_ineligible = sum(1 for a in assessed if a and a.get("_skipped") == "ineligible")

    # If nothing survived and the model was failing throughout, the honest answer
    # is "the search could not run", not "we found nothing for your profile".
    model_errors = [a for a in assessed if a and a.get("_skipped") == "model_error"]
    if not items and model_errors:
        return {
            "ok": False,
            "error": model_errors[0].get("_error")
            or "The language model could not be reached, so nothing could be assessed.",
            "results": [],
        }

    # Rank: confirmed eligible first, then by how much of the course list matched,
    # then trusted sources ahead of the rest.
    order = {"eligible": 0, "unclear": 1}
    items.sort(
        key=lambda i: (
            order.get(i["verdict"], 2),
            -(i["course_match"]["matched"]),
            not i["trusted_source"],
        )
    )
    items = items[:max_results]

    notes = []
    if skipped_ineligible:
        notes.append(f"{skipped_ineligible} clearly ineligible or expired")
    if skipped_listings:
        notes.append(f"{skipped_listings} directory/round-up page(s) you cannot apply to")

    if items:
        message = f"Skipped {' and '.join(notes)}." if notes else ""
    else:
        message = (
            "No opportunity you can directly apply to was found for this profile"
            + (f" — skipped {' and '.join(notes)}." if notes else ".")
            + " Nothing has been invented to fill the gap; try broadening your "
            "field or interests."
        )

    return {
        "ok": True,
        "query": search.get("query", ""),
        "results": items,
        "considered": len(search["results"]),
        "skipped_listings": skipped_listings,
        "skipped_ineligible": skipped_ineligible,
        "message": message,
    }


def _safe_assess(result: dict, profile: dict, courses: list) -> dict | None:
    """_assess_one, but one bad page cannot sink the whole shortlist."""
    try:
        return _assess_one(result, profile, courses)
    except Exception:
        return None


def describe_tools() -> list[str]:
    """Tool names for the startup banner."""
    return [t.name for t in build_tools()]


def proxy_budget() -> dict:
    """Ask the LiteLLM proxy how much of the key's budget is left.

    Keys carry a $3 lifetime cap and simply start failing once it is gone, so
    it is worth seeing the number before a demo rather than after.
    """
    try:
        key, use_proxy = llm_key()
    except MissingKeyError:
        return {"available": False}
    if not use_proxy:
        return {"available": False}

    try:
        response = requests.get(
            LITELLM_BASE_URL.rsplit("/v1", 1)[0] + "/key/info",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
        response.raise_for_status()
        info = response.json()
        info = info.get("info", info)
        spend = float(info.get("spend") or 0)
        budget = info.get("max_budget")
        return {
            "available": True,
            "spend": spend,
            "max_budget": float(budget) if budget is not None else None,
            "remaining": (float(budget) - spend) if budget is not None else None,
        }
    except Exception:
        return {"available": False}  # never let a budget check block startup


def status() -> dict:
    """What is wired up right now — drives the startup banner and /health."""
    try:
        import giu_tools

        giu = giu_tools.availability()
    except Exception:
        giu = {"portal": False, "cms": False}

    try:
        _, use_proxy = llm_key()
        has_key = True
    except MissingKeyError:
        use_proxy, has_key = False, False

    return {
        "model": MODEL_NAME,
        "route": "iHQ LiteLLM proxy" if use_proxy else "Anthropic direct",
        "anthropic_key": has_key,
        "tavily_key": bool((os.getenv("TAVILY_API_KEY") or "").strip()),
        "gmail_drafting": gmail_available(),
        "giu_portal": giu.get("portal", False),
        "giu_cms": giu.get("cms", False),
        "tools": describe_tools(),
    }


def print_banner() -> None:
    """Friendly startup summary: what works, what is missing, and why that's fine."""
    state = status()
    tick = lambda on: "on " if on else "off"  # noqa: E731

    print(f"  Model            : {state['model']}")
    print(f"  Route            : {state['route']}")
    print(f"  Model key        : {tick(state['anthropic_key'])}")
    print(f"  Tavily search    : {tick(state['tavily_key'])}")

    budget = proxy_budget()
    if budget.get("available") and budget.get("max_budget"):
        left = budget["remaining"]
        warn = "   <-- running low" if left is not None and left < 0.50 else ""
        print(
            f"  Proxy budget     : ${budget['spend']:.4f} spent of "
            f"${budget['max_budget']:.2f}  (${left:.4f} left){warn}"
        )
    print(f"  Gmail drafting   : {tick(state['gmail_drafting'])}"
          f"{'' if state['gmail_drafting'] else '  (optional — add credentials.json)'}")
    print(f"  GIU portal / CMS : {tick(state['giu_portal'])}/{tick(state['giu_cms'])}"
          f"{'' if state['giu_portal'] else '  (optional — set GIU_USERNAME/GIU_PASSWORD)'}")
    print(f"  Tools active     : {', '.join(state['tools'])}")

    if not state["anthropic_key"]:
        print("\n  ! No model key — put LITELLM_API_KEY in .env (copy .env.example).")
    if not state["tavily_key"]:
        print("  ! TAVILY_API_KEY is missing — search will not run without it.")


if __name__ == "__main__":
    print(f"Scholar Hunter — checking Claude connection ({MODEL_NAME})...")
    result = check_llm()
    if not result["ok"]:
        print("  FAILED:", result["error"])
        raise SystemExit(1)
    print("  OK:", result["reply"])
    print("  Tools:", ", ".join(describe_tools()))

    print("\nCLI test mode. Describe your profile, or press Enter for a sample.\n")
    try:
        typed = input("> ").strip()
    except EOFError:
        typed = ""
    profile = typed or (
        "I have a Bachelor's in Computer Science from Egypt, GPA 3.6/4.0. "
        "I want Master's scholarships in Germany in machine learning."
    )
    if not typed:
        print(f"(using sample: {profile})")

    outcome = run_agent(profile, verbose=True)
    print("\n" + "=" * 70)
    print(outcome["output"] if outcome["ok"] else f"FAILED: {outcome['error']}")
