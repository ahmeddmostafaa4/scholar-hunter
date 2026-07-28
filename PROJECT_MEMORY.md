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
- [x] Phase 5 — Gmail draft + save_deadline
- [x] Phase 6 — Flask backend
- [x] Phase 7 — web frontend
- [x] Phase 8 — full integration pass

## Test results

- Phase 0: `pip install -r requirements.txt` succeeded in `.venv`; `agent`/`app` import; the reused `guc_portal`/`guc_cms` packages import from the repo root; a test asks **git itself** what it tracks and confirms no `.env`/`credentials.json`/`token.json`/venv is staged (`tests/test_phase0_skeleton.py`, 6 passed).
- Phase 8: full journey re-run live in a browser (`python app.py` -> submit -> cards). 12 candidates -> 3 genuine opportunity pages (DAAD scholarship database, for9a, eliza.school), every card citing a real source URL, all honestly marked *Unclear* because language requirements could not be confirmed. Human-in-the-loop verified directly: `deadlines.json` did not exist before the click and contained the real name/date/URL after it; "Draft email" without Gmail explained itself on the card; **zero browser alerts** used anywhere. No JS errors. Two more issues found and fixed (see below). Final suite: **95 passed, 12 skipped** (the skips are the live-Claude tests — the account's credit balance ran out during Phase 8 testing; all 12 passed earlier in Phases 1/3/4).
- Phase 7: driven in a real browser with Playwright against a live server (stubbed agent), 14 tests — hero + form render, error banner and spinner hidden on load, course tabs switch, missing fields show the styled banner (never a browser alert), a submitted profile renders the uniform 6-bullet card, the badge carries an icon **and** a label rather than colour alone, per-requirement met/not-stated rows show, the course-match bar reflects 2/3, the summary reports what was skipped, "Save deadline" flips to an inline confirmation, "Draft email" with no Gmail explains itself on the card, a 500 shows the banner and stops the spinner, empty results say nothing was invented, and at 390px the grid collapses to one column with no sideways scroll. Three UI bugs found and fixed (see below). Full suite 102 passed.
- Phase 6: all four endpoints tested against the HTTP contract — `/` serves HTML, `/health` reports configuration, `/search` returns the uniform 6-field cards, and bad input (non-JSON body, missing profile fields, upstream exception, missing API key) always returns clean JSON with the right status and never a stack trace. Uploads parse `.txt`, `.csv` (rows joined) and `.pdf` (round-tripped through a real generated PDF; a text-free scan is reported honestly), and typed + uploaded courses combine. `/draft_email` without Gmail is a 503 naming `credentials.json`; `/save_deadline` refuses when the page states no deadline. Pipeline behaviour covered too: listing pages skipped, social results dropped, one broken page not sinking the shortlist, eligible ranked above unclear (`tests/test_phase6_app.py`, 28 passed). Live run: 12 candidates -> 2 genuine DAAD opportunity pages in ~33s. Full suite 86 passed.
- Phase 5: `save_deadline` re-parses the store after **every** one of 12 appends and it stays valid JSON; a deliberately corrupted store does not lock the student out; a rejected save creates no file and leaves no `.tmp` behind (writes go through a temp file + atomic replace). `draft_email` with no `credentials.json` returns a clear message naming the file and saying the rest still works, and the agent's other tools stay registered. A source-level test asserts only the `gmail.compose` scope is ever requested — the agent holds no send permission at all. The `/search` toolset is verified to exclude both side-effect tools (`tests/test_phase5_actions.py`, 13 passed).
- Phase 4: agent assembles via `create_tool_calling_agent` + `AgentExecutor` with 6 tools (4 core + 2 optional GIU ones, both live here). Live end-to-end run on a real profile called `search_scholarships`, cited real https sources, and covered deadline + eligibility; a bare "can you find me a scholarship?" made it ask for the missing profile fields instead of searching. System prompt asserted to carry the reliability rules and today's date. One real bug fixed: the agent's `output` arrived as a list of Claude content blocks, not a string, which would have broken the JSON API downstream (`tests/test_phase4_agent.py`, 9 passed).
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
- **A model outage is reported, never disguised as "no results".** When the Anthropic credit balance ran out mid-Phase-8, the pipeline degraded into an empty shortlist telling the student to *"try broadening your field or interests"* — blaming their search for an outage. `_ask_json` now marks `model_error`, the pipeline propagates it, and `_explain_model_error` turns the raw 400 into "Your Anthropic credit balance is too low… top it up at console.anthropic.com". The UI shows that in the error banner.
- **Live tests skip, not fail, when the API is unreachable.** `tests/conftest.py` probes Claude once per session; anything marked `live_llm` skips with the reason (credit balance, rate limit, bad key). A red suite that actually means "top up your account" is a misleading signal. Offline-only run: `pytest -m "not live_llm"`.
- **A card's "Save deadline" button is disabled when the page states no deadline**, with the reason in its tooltip — the backend already refused these, but only after the click.
- **Port 5000 is contested on macOS.** AirPlay Receiver (ControlCenter) listens on `*:5000`, so Flask binds `127.0.0.1:5000` happily while `http://localhost:5000` resolves to AirPlay and returns `403 Forbidden` — the app looks broken though it is running. `app.py` probes the port at startup and falls back to 5050, printing the URL it actually served and why, **and opens the browser itself** — typing `localhost:5000` from habit lands on a blank 403 that reads as a broken app. `PORT` overrides the port, `NO_BROWSER=1` skips the auto-open.
- **`/search` runs a deterministic pipeline (`run_shortlist`), not the conversational agent loop.** It orchestrates the same tool functions in a fixed order so every card is guaranteed to carry the same six fields; a form -> cards UI has no conversation in which to recover from a malformed reply. The `AgentExecutor` remains for the CLI/chat path. Candidates are assessed in parallel (~33s for 12).
- **Directory and round-up pages are discarded, not shown.** Extraction returns `is_single_opportunity`, and anything that is a list ("93 Scholarships in Germany"), search-results page or blog round-up is skipped, with the count surfaced in `message`. Without this they were shown as ELIGIBLE: a student cannot apply to a list, and a listing's blanket criteria ("Computer Science", "Masters") match almost anyone. Social/forum domains are dropped at search time for the same reason. Unconfirmed pages default to *listing*, not applicable.
- **Three guardrails are enforced in code, not just in the prompt**, because the model got both wrong in testing: (1) a "graduate funding" page describes the *level being funded*, not a degree the applicant must already hold — conflating them wrongly excluded exactly the students this tool exists to help; (2) when a page states no criteria, "nothing was unmet" is vacuously true and the model called it `eligible` — silence is not permission; (3) a page whose only match was "Department of Computer Science" earned an `eligible` off that one trivially-matching row, so an `eligible` verdict now requires at least one *discriminating* criterion (GPA, degree level, nationality, language) to be met — subject alone says nothing about whether this student qualifies.
- Portal is slow/rate-limited → the GIU transcript tool is opt-in (button/CLI), not
  part of the default `/search` path.

## Known issues / next steps

- Playwright is a **dev-only** extra for the Phase 7 browser tests; those tests skip themselves when it is absent, and it is left commented out in `requirements.txt`.
- Google Calendar integration still a TODO (deadline store is local JSON).
- Portal transcript fetch is slow (~1 req/min rate limit) — used only on demand.
- **The Anthropic credit balance is currently exhausted** (ran out during Phase 8 testing). The 12 `live_llm` tests skip until it is topped up; they passed in Phases 1/3/4 before that. The app itself reports the condition clearly rather than failing oddly.
- Tavily free tier caps requests; heavy testing can exhaust the quota. Live search tests are kept few for this reason.
- `TavilySearchResults` logs a deprecation warning on the LangChain 0.3 line (successor is the `langchain-tavily` package). Kept because the spec names it; swapping is a one-line change in `_tavily()`.
