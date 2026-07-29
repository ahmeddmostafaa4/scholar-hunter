# Scholar Hunter

An agentic assistant (LangChain + Claude) that helps graduate students find
scholarships, grants, master's programmes and workshops they are **actually
eligible for** — and then act on the best ones.

It does not just search. For every promising result it opens the opportunity's
own page, extracts the requirements that page really states, and checks them
against the student one at a time. Anything the page does not say comes back as
*"not stated"* rather than a plausible guess, and directory pages like
*"93 Scholarships in Germany"* are discarded because you cannot apply to a list.

Everything runs locally from VS Code. There is no deployment step.

---

## What it does

1. **Understands the profile** — field of study, degree level, GPA, nationality,
   interests, and optionally the student's completed undergraduate courses. If a
   key field is missing it asks instead of guessing.
2. **Searches** trusted portals and official sources first (Mastersportal,
   Erasmus+, DAAD, Chevening, Fulbright, official university pages), while still
   allowing other credible results.
3. **Extracts each opportunity's real requirements** from its own page: minimum
   GPA, degree level, eligible nationalities, language, field, deadline, funding
   scope, and any prerequisite courses.
4. **Checks eligibility requirement by requirement**, and — when the programme
   lists prerequisites and the student supplied their courses — matches courses
   **by description and content, not by title**, so "Intro to AI" and
   "Foundations of Machine Intelligence" still match.
5. **Presents a ranked shortlist** where every card has the same six parts.
6. **Only after you click** does it draft an email or save a deadline.

### Reliability rules

These are the point of the project, not a footnote:

- Never invents opportunities, amounts or deadlines; every card cites its source URL.
- Never sends an email or saves a deadline without an explicit click.
- Flags "not stated" and "unclear" honestly instead of overselling a match.
- Says plainly when nothing was found rather than manufacturing results.

---

## 1. Install

Python 3.10+ (developed on 3.12).

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Keys

```bash
cp .env.example .env               # then fill it in
```

| Variable | Where to get it | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) → API Keys | **Yes** |
| `TAVILY_API_KEY` | [tavily.com](https://tavily.com) — free tier is plenty | **Yes** |
| `GIU_USERNAME` / `GIU_PASSWORD` | Your GIU/GUC portal login | Optional |

`.env` is git-ignored. Never commit it.

### Gmail (optional — for drafting only)

Search, eligibility and deadlines all work **without** this. Set it up only if
you want the "Draft email" button:

1. Go to the [Google Cloud Console](https://console.cloud.google.com) and create
   a project (or pick an existing one).
2. **APIs & Services → Library →** enable the **Gmail API**.
3. **APIs & Services → OAuth consent screen →** choose *External*, fill in the
   basics, and **add your own email address as a Test user**. Without this step
   Google blocks the login.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →
   Application type: Desktop app.**
5. Download the JSON and save it as **`credentials.json`** in the project root.
6. The first time you click "Draft email", a browser consent screen opens and a
   **`token.json`** is written next to it. Both files are git-ignored.

The app requests the `gmail.compose` scope only, so it holds no permission to
send — drafting is not a promise it declines to break, it is the only thing it
can do.

## 3. Run it

Open the project folder in VS Code, open the integrated terminal, and:

```bash
python app.py
```

**Your browser opens automatically** at the right address. If it does not, open
the URL printed in the terminal — normally <http://localhost:5050> on macOS,
<http://localhost:5000> elsewhere. Set `NO_BROWSER=1` to stop it opening.

This is a **local dev server**. Nothing is deployed anywhere.

> **macOS note.** On macOS, AirPlay Receiver occupies port 5000, and
> `http://localhost:5000` will return `403 Forbidden` even though Flask is
> running fine. The app detects this at startup, moves to port **5050**, and
> prints the URL it actually served. To use 5000 instead, turn AirPlay Receiver
> off in *System Settings → General → AirDrop & Handoff*. Set `PORT` to choose
> your own.

### Optional CLI mode

Handy for a quick check without the browser — it runs the conversational agent:

```bash
python agent.py
```

---

## Using the page

Fill in field of study, degree level and country (the three required fields),
plus GPA and interests if you have them.

**Undergraduate courses are optional but improve accuracy.** You can either
paste them (one per line — *descriptions help most*) or upload a transcript or
course list as `.txt`, `.csv` or `.pdf`. Leave it empty and course matching is
simply reported as "not assessed".

Cards start **collapsed**, showing only what you need to judge them: name,
eligibility badge, deadline and source. Click **View more** to open one up:

1. **Name & type** and institution, with an eligibility badge
2. **Fit** — one line on why it suits you
3. **Requirements & eligibility** — each one ✓ met, ✕ not met, or – not stated
4. **Course match** — e.g. "4 of 5 required courses matched", or "not assessed"
5. **Documents to submit** — the documents the application actually asks for
6. **How to prepare these** — per-document steps tailored to this opportunity
   and to you: what it must achieve, concrete actions, and the mistake that
   most often costs applicants
7. **Where to apply** — the real application portal link, taken from the page
8. **Deadline & funding** — deadlines under 30 days away are tinted amber
9. **Source** — the URL it came from

### Building an application pack

Most programmes take applications through a web portal, not email, and most want
**one combined PDF**. Under "Documents to submit" each required document gets its
own attach button. Attach the files **you already have**, click **Build combined
PDF**, and you get back a single ordered PDF with a contents sheet — then open
the portal and submit it yourself.

It tells you what is missing and what it could not merge (a `.docx`, a
password-protected PDF), because finding a gap after submitting is the failure
this exists to prevent.

### Auto-filling a portal form

**Auto-fill in a browser** opens the application portal in its own visible
window, waits while you log in, then types what it can from your profile into
the fields it recognises — name, email, nationality, field of study, GPA,
university. Every field it touches is outlined in blue and listed back to you.

**It stops at the submit button, and that is enforced in code rather than
intended.** `autofill.py` has no `submit()` function and no code path to one; it
never clicks a button, never presses Enter, and never touches a checkbox or radio
— on an application form those are declarations, and only you may tick them.
Passwords, payment fields and captchas are skipped, and anything you have already
typed is left alone. A test drives a real form carrying a submit handler and
asserts the handler never fires.

You review the form and submit it yourself.

Auto-fill needs Playwright (`pip install playwright && playwright install
chromium`). Without it the button explains itself and everything else still works.

**What it deliberately will not do.** It does not write your documents, and it
does not submit for you. A motivation letter has to be your own words; a
transcript or a reference letter is issued by someone else — generating either
is forgery, and submitting an AI-written letter as your own is academic
misconduct that gets applicants disqualified and banned. The declaration that an
application is truthful is yours to make, so the pack comes back to you.

The document guidance is generated **only when you expand a card**, so a
shortlist you merely skim costs nothing extra.

"Draft email" and "Save deadline" act only on click.

---

## Project layout

```
agent.py            the LangChain agent: LLM, all six tools, system prompt,
                    and the shortlist pipeline. Importable; has a CLI mode.
giu_tools.py        wraps the existing cms-app/ and portal-app/ packages as
                    optional tools (see below)
app.py              Flask server: serves the page, exposes the JSON endpoints
templates/index.html
static/style.css    gradient hero, cards, the handful of animations
static/script.js    form submit, card rendering, button confirmations
tests/              pytest, one file per phase
deadlines.json      created at runtime
```

### Endpoints

| Method | Path | Does |
|---|---|---|
| `GET` | `/` | serves the page |
| `GET` | `/health` | what is configured (the page uses it to explain a disabled button) |
| `POST` | `/search` | profile in, ranked uniform shortlist out |
| `POST` | `/document_help` | explains how to prepare a card's required documents |
| `POST` | `/application_pack` | merges the student's own files into one ordered PDF |
| `POST` | `/autofill/open` | opens the portal in a visible browser |
| `POST` | `/autofill/fill` | fills recognised fields from the profile — **never submits** |
| `POST` | `/draft_email` | creates a Gmail **draft** |
| `POST` | `/save_deadline` | appends to `deadlines.json` |

---

## The `cms-app/` and `portal-app/` folders

These arrived with the project and are **reused as they are, not rebuilt**. Each
is a small, working client that logs in and hands back plain dataclasses, with no
knowledge of agents — wrapping them was left as the job, and `giu_tools.py` is
that wrapper and nothing more.

- **`portal-app/guc_portal`** — the student portal / SIS: transcript, cumulative
  GPA, per-semester courses, coursework marks.
- **`cms-app/guc_cms`** — the CMS: enrolled courses and their material.

`giu_tools.py` puts both on the import path and exposes two optional tools:
`get_giu_transcript` (fills in your GPA and completed courses) and
`get_giu_cms_courses`. They need `GIU_USERNAME` / `GIU_PASSWORD`; without them
they report themselves unavailable and everything else carries on. The portal is
old and rate-limits at roughly one request a minute, so these are opt-in rather
than part of the default search.

---

## Design decisions worth knowing

- **Email is draft-only, by design.** Human-in-the-loop is the point; the app
  requests no send permission at all.
- **The deadline store is a local `deadlines.json`.** A clearly-marked
  `TODO (Google Calendar)` in `agent.py` shows where a Calendar API call would
  slot in. It is local on purpose — Calendar OAuth must never block the agent.
- **Search works with no Gmail credentials.** Only the "Draft email" button
  degrades, and it explains itself on the card.
- **`/search` runs a deterministic pipeline, not the chat loop.** A form → cards
  UI has no conversation in which to recover from a malformed reply, so the
  pipeline orchestrates the same tools in a fixed order and every card is
  guaranteed to carry the same six fields. The conversational `AgentExecutor` is
  still there for the CLI.
- **Directory and round-up pages are discarded.** They match almost anyone and
  you cannot apply to them.
- **An `eligible` verdict needs a real criterion.** Matching only the field of
  study, or a page that states nothing at all, gives "unclear" — silence is not
  permission.
- **The frontend is one page of plain HTML/CSS/JS.** No framework, no bundler,
  no npm. The animations are a handful of hand-written `@keyframes`.

---

## Tests

```bash
pytest -q                      # everything
pytest -q -m "not live_llm"    # offline only — no API calls, runs in seconds
pytest tests/test_phase3_eligibility.py -q     # one phase
```

Tests marked `live_llm` make real Claude calls. They **skip rather than fail**
when the API cannot be reached — a missing key, an exhausted credit balance or a
rate limit is not a defect in this code, and a red suite that really means "top
up your account" is a misleading signal. If you see

```
SKIPPED — Anthropic credit balance exhausted — top up to run live tests
```

that is what happened; add credits and re-run to exercise them.

The Phase 7 browser tests need Playwright, which is dev-only:

```bash
pip install playwright && playwright install chromium
```

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Blank page / `403` at `localhost:5000` | macOS AirPlay Receiver holds that port and serves an empty 403, which looks like the app is down. Use the port the app prints (5050) — it now opens the browser for you — or turn AirPlay Receiver off |
| "ANTHROPIC_API_KEY is not set" | Copy `.env.example` to `.env` and fill it in |
| "Email drafting is not configured" | Expected without `credentials.json` — everything else still works |
| Google blocks the OAuth login | Add your own email as a **Test user** on the OAuth consent screen |
| Search returns very few results | Working as intended: directory pages and ineligible options are discarded, and the card summary says how many |
| Search is slow (~30–60s) | Each candidate is a page fetch plus two model calls; they run in parallel |
| "Your Anthropic credit balance is too low" | Exactly what it says — top up at console.anthropic.com. The app reports this rather than pretending no scholarships exist |
