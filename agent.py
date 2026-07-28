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
        return {**fallback, "error": str(exc)}
    except Exception as exc:
        return {**fallback, "error": f"Model call failed: {exc}"}

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


def fetch_page_text(url: str, max_chars: int = 12000) -> dict:
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
    for noise in soup(["script", "style", "nav", "footer", "header", "form", "noscript"]):
        noise.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return {"ok": True, "text": text[:max_chars], "truncated": len(text) > max_chars}


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

    if url and not text:
        fetched = fetch_page_text(url)
        if fetched["ok"]:
            text = fetched["text"]
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
  field_of_study, deadline, funding_scope, required_courses, opportunity_name,
  opportunity_type, institution

Rules — these matter more than completeness:
- Use ONLY what the text below actually states. Never infer, never guess, never
  use outside knowledge about this programme.
- If the text does not state a field, set it to exactly "{NOT_STATED}".
- "required_courses" is a LIST of prerequisite/required course names the applicant
  must already have studied. Use [] if the text names none.
- "opportunity_type" must be one of: "scholarship", "master's programme",
  "workshop", "grant", or "{NOT_STATED}".
- "deadline" should be the application deadline as written (e.g. "15 January 2026").
- "funding_scope" is what the money covers (e.g. "full tuition + 992 EUR/month stipend").

TEXT:
\"\"\"
{text[:10000]}
\"\"\"
"""
    parsed = _ask_json(prompt, fallback={"requirements": blank})

    requirements = {f: parsed.get(f, NOT_STATED) or NOT_STATED for f in REQUIREMENT_FIELDS}
    if isinstance(requirements["required_courses"], str):
        # Normalise a stringified list into a real list.
        value = requirements["required_courses"]
        requirements["required_courses"] = [] if value == NOT_STATED else [value]

    return {
        "ok": True,
        "url": url,
        "name": parsed.get("opportunity_name", NOT_STATED),
        "type": parsed.get("opportunity_type", NOT_STATED),
        "institution": parsed.get("institution", NOT_STATED),
        "requirements": requirements,
        "note": parsed.get("error", fetch_note),
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
    stated = any(
        str(criteria.get(f, NOT_STATED)).strip().lower() not in {NOT_STATED, "", "[]", "none"}
        for f in ELIGIBILITY_FIELDS
    )
    confirmed = any(r["status"] == "met" for r in breakdown)
    if verdict == "eligible" and not (stated and confirmed):
        verdict = "unclear"

    return {
        "ok": True,
        "verdict": verdict,
        "reason": parsed.get("reason", ""),
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
# Tool 5: Gmail draft (draft only — never sends)
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
        from langchain_google_community import GmailToolkit
        from langchain_google_community.gmail.utils import (
            build_resource_service,
            get_gmail_credentials,
        )

        credentials = get_gmail_credentials(
            token_file=str(TOKEN_FILE),
            client_secrets_file=str(CREDENTIALS_FILE),
            # Compose scope only, so the agent cannot send even if asked to —
            # holding no send permission is a stronger guarantee than declining.
            scopes=["https://www.googleapis.com/auth/gmail.compose"],
        )
        toolkit = GmailToolkit(api_resource=build_resource_service(credentials=credentials))

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


def describe_tools() -> list[str]:
    """Tool names for the startup banner."""
    return [t.name for t in build_tools()]


def status() -> dict:
    """What is wired up right now — drives the startup banner and /health."""
    try:
        import giu_tools

        giu = giu_tools.availability()
    except Exception:
        giu = {"portal": False, "cms": False}

    return {
        "model": MODEL_NAME,
        "anthropic_key": bool((os.getenv("ANTHROPIC_API_KEY") or "").strip()),
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
    print(f"  Anthropic key    : {tick(state['anthropic_key'])}")
    print(f"  Tavily search    : {tick(state['tavily_key'])}")
    print(f"  Gmail drafting   : {tick(state['gmail_drafting'])}"
          f"{'' if state['gmail_drafting'] else '  (optional — add credentials.json)'}")
    print(f"  GIU portal / CMS : {tick(state['giu_portal'])}/{tick(state['giu_cms'])}"
          f"{'' if state['giu_portal'] else '  (optional — set GIU_USERNAME/GIU_PASSWORD)'}")
    print(f"  Tools active     : {', '.join(state['tools'])}")

    if not state["anthropic_key"]:
        print("\n  ! ANTHROPIC_API_KEY is missing — copy .env.example to .env and add it.")
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
