# PROJECT_MEMORY — Scholar Hunter

> Living record. Update at the end of every phase. Single source of truth for project state.

## Overview

Scholar Hunter is a locally-run agentic assistant (LangChain + Claude) that helps
graduate students find scholarships, grants, master's programs, and workshops they
are actually eligible for. It reads the student's profile (field, degree level, GPA,
nationality, interests, and optionally their completed undergraduate courses),
searches the web via Tavily preferring trusted portals (Mastersportal, DAAD,
Erasmus+, Chevening, Fulbright, official university pages), extracts each
opportunity's real requirements from its own page, checks eligibility requirement by
requirement (including description-based course matching), and presents a ranked
shortlist in a uniform ~6-bullet format. Only after explicit user approval does it
create a Gmail *draft* (never sends) or save a deadline to `deadlines.json`.
Everything runs locally from VS Code (`python app.py` → http://localhost:5000).

## Architecture

- **Stack:** Python 3.12, LangChain 0.3 (`create_tool_calling_agent` + `AgentExecutor`),
  `ChatAnthropic` with model `claude-sonnet-4-6`, Tavily search, Flask + flask-cors,
  plain HTML/CSS/JS frontend. No deployment — local dev loop only.
- **Files:**
  - `agent.py` — the LangChain agent: LLM, all tools, system prompt. Importable; has a CLI mode.
  - `giu_tools.py` — optional reuse of the existing `portal-app/guc_portal` and
    `cms-app/guc_cms` packages (transcript → GPA + completed courses; CMS → course list).
  - `app.py` — Flask server: serves the frontend, exposes `/search`, `/draft_email`, `/save_deadline`.
  - `templates/index.html`, `static/style.css`, `static/script.js` — the web UI.
  - `tests/` — pytest, added per phase.
  - `deadlines.json` — created at runtime by the save_deadline tool.
- **Existing local resources (reused, not rebuilt):**
  - `portal-app/guc_portal` — GIU/GUC student portal (SIS) client. Gives
    `GucPortal.get_transcript()` → cumulative GPA + per-semester course rows,
    `available_years()`, `get_transcript_year()`, previous/current grades.
    Old and slow; rate-limits (~1 req/min), retries internally.
  - `cms-app/guc_cms` — GUC CMS client. `GucCms.list_courses()`, `find_course()`,
    `get_content()` (files by week), `fetch_bytes()`, `download()`. NTLM auth.
  - Both are plain packages (no agent knowledge); `giu_tools.py` adds them to
    `sys.path` and wraps them as optional LangChain tools. Credentials come from
    `GIU_USERNAME`/`GIU_PASSWORD` in `.env`; if absent the tools report themselves
    unavailable and the app still works with typed/uploaded courses.

## Tools

| Tool | Inputs | Outputs | Connector |
|---|---|---|---|
| `search_scholarships` | query | titles, snippets, URLs | Tavily |
| `extract_requirements` | url or text | structured requirements (GPA, degree, nationality, language, field, deadline, funding, required courses; "not stated" when absent) | requests + LLM |
| `check_eligibility` | requirements, profile | verdict + per-criterion met/not met/not stated + `deadline_status` | LLM reasoning |
| `match_courses` | student courses, required courses | per required course: matched/partial/missing + which course + confidence | LLM reasoning |
| `draft_email` | to, subject, body | Gmail draft ID (never sends) | Gmail API (OAuth) |
| `save_deadline` | name, date, url | appended entry | local `deadlines.json` |
| `get_giu_transcript` (optional) | year code | GPA + completed courses (+ a note that GUC GPA is lower-is-better) | `guc_portal` (reused) |
| `get_giu_cms_courses` (optional) | — | currently enrolled courses | `guc_cms` (reused) |

## Build status

- [x] Phase 0 — skeleton, .gitignore, GitHub repo, first commit
- [x] Phase 1 — LLM connection
- [x] Phase 2 — web search tool
- [x] Phase 3 — extraction, eligibility, course matching
- [x] Phase 4 — agent assembly
- [ ] Phase 5 — Gmail draft + save_deadline
- [ ] Phase 6 — Flask backend
- [ ] Phase 7 — web frontend
- [ ] Phase 8 — full integration pass

## Test results

- Phase 0: `pip install -r requirements.txt` succeeded in `.venv`; `agent`/`app` import; the reused `guc_portal`/`guc_cms` packages import from the repo root; a test asks **git itself** what it tracks and confirms no `.env`/`credentials.json`/`token.json`/venv is staged (`tests/test_phase0_skeleton.py`, 6 passed).
- Phase 4: agent assembles via `create_tool_calling_agent` + `AgentExecutor` with 6 tools (4 core + the 2 optional GIU ones, both live here). Live end-to-end run on a real profile called `search_scholarships`, cited real https sources, and covered deadline + eligibility; a bare "can you find me a scholarship?" made it ask for the missing profile fields instead of searching. System prompt asserted to carry the reliability rules and today's date. One real bug fixed: the agent's `output` arrived as a list of Claude content blocks, not a string, which would have broken the JSON API downstream (`tests/test_phase4_agent.py`, 9 passed).
- Phase 3: (a) fully-stated page + matching profile -> `eligible`, every row `met`, deadline `open`; (b) page requiring an already-held Master's vs a Bachelor's student -> `not_eligible` with the degree row identified as the failure; (c) page stating nothing checkable -> all fields `not stated` and verdict `unclear`. Course matching: differently-named-but-equivalent courses matched by description; a genuinely absent course reported `missing`; bare names (no descriptions) cap confidence below `high`; no courses on either side returns "not assessed" **without calling the model at all**. Two real bugs found and fixed while testing (see below). `tests/test_phase3_eligibility.py`, 17 passed; full suite 34 passed.
- Phase 2: live Tavily queries ("Master's Data Science scholarship Germany 2026", "PhD funding for Egyptian students") returned real DAAD and Mastersportal URLs with trusted sources ranked first; a nonsense query returned cleanly instead of raising; an empty query is rejected without spending an API call; a missing key gives a clear `.env` message; the zero-result path explicitly tells the agent not to invent opportunities (`tests/test_phase2_search.py`, 7 passed).
- Phase 1: live "Reply with exactly: OK" call to `claude-sonnet-4-6` returned OK; `python agent.py` prints the same. Missing/whitespace key raises a typed `MissingKeyError` whose message names the variable and points at `.env`, and `check_llm()` converts it to a result dict — no stack trace, no key echoed (`tests/test_phase1_llm.py`, 4 passed).

## Setup / keys

- `.env` (never committed): `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, optional `GIU_USERNAME`/`GIU_PASSWORD`.
- Gmail: `credentials.json` in project root (Google Cloud OAuth Desktop client, Gmail API enabled); first draft creation opens browser consent and writes `token.json`. Both git-ignored. **Email is optional** — search works without it.
- Run: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`, then `python app.py` → http://localhost:5000. CLI test mode: `python agent.py`.
- GitHub repo: https://github.com/ahmeddmostafaa4/scholar-hunter (public).

## Decisions & constraints

- Email is **draft-only** by design (human-in-the-loop); the agent never sends.
- Calendar is a local `deadlines.json` store for now; a clearly-marked TODO in
  `agent.py` shows where Google Calendar API would slot in. Calendar OAuth must not
  block the agent.
- Search + eligibility + shortlist must work with **no Gmail credentials**; only the
  Draft email button degrades (styled "email not configured" message).
- Reliability rules are baked into the system prompt: never invent opportunities or
  deadlines, always cite source URLs, flag "not stated"/unclear honestly, never
  send/save without explicit approval.
- Uniform output: every shortlist item uses the same ~6-bullet structure (name & type,
  fit, requirements & eligibility, course match, deadline & funding, source) in both
  the backend JSON and the frontend cards.
- Frontend is one static page, plain HTML/CSS/JS, white + blue, gradient hero, a few
  hand-written animations. No frameworks, no npm, no build step.
- LangChain pinned to the 0.3 line so `create_tool_calling_agent` + `AgentExecutor`
  match the spec (LangChain 1.x renamed this surface).
- `cms-app/` and `portal-app/` are **reused in place** via `sys.path` (folder names
  differ from the spec's `cms/`/`portal/` — they were already named this way).
  Their `.venv`s and `.env`s are git-ignored; the small `guc_cms`/`guc_portal`
  packages are committed with the repo.
- **Eligibility scores criteria only.** `deadline` and `funding_scope` are deliberately excluded from the met/not-met breakdown (`ELIGIBILITY_FIELDS` vs `REQUIREMENT_FIELDS`): a deadline is not something a student "has", so scoring it produced meaningless `not_stated` rows that dragged every verdict to `unclear`. The deadline is judged separately as open/expired against today's date; funding scope is descriptive.
- **Two guardrails are enforced in code, not just in the prompt**, because the model got both wrong in testing: (1) a "graduate funding" page describes the *level being funded*, not a degree the applicant must already hold — conflating them wrongly excluded exactly the students this tool exists to help; (2) when a page states no criteria, "nothing was unmet" is vacuously true and the model called it `eligible`. Silence is not permission, so an `eligible` verdict now requires at least one criterion both stated and confirmed.
- Portal is slow/rate-limited → the GIU transcript tool is opt-in (button/CLI), not
  part of the default `/search` path.

## Known issues / next steps

- Google Calendar integration still a TODO (deadline store is local JSON).
- Portal transcript fetch is slow (~1 req/min rate limit) — used only on demand.
- Tavily free tier caps requests; heavy testing can exhaust the quota. Live search tests are kept few for this reason.
- `TavilySearchResults` logs a deprecation warning on the LangChain 0.3 line (successor is the `langchain-tavily` package). Kept because the spec names it; swapping is a one-line change in `_tavily()`.
