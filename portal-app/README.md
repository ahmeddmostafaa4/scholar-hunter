# guc_portal

A tiny Python client for the GUC/GIU student portal / SIS. It reads your
**transcript**, your **previous grades** (the detailed coursework marks), and
your **current grades**. It logs in, hands back plain dataclasses, and knows
nothing about agents, so wrapping it as tools is a small job.

There is a notebook that does exactly that: **`portal-tools.ipynb`** wraps the
package as LangChain tools (terms, courses in a term, grades, transcript years,
transcript for a year) and lets an agent answer questions in plain words. Run it
with `uv sync` then `uv run jupyter lab`.

## What it does

### Transcript

```python
from guc_portal import GucPortal

portal = GucPortal("your.username", "your.password")   # or GUC_USERNAME / GUC_PASSWORD

portal.available_years()          # [("22", "2024-2025"), ("21", "2023-2024"), ...]

t = portal.get_transcript_year("22")   # one year, fast (one request)
print(t.cumulative_gpa)                # "0.97"
for semester, rows in t.by_semester().items():
    for r in rows:
        print(semester, r.grade, r.course)

full = portal.get_transcript()    # the whole thing, slow (waits between years)
```

### Previous grades (detailed coursework)

```python
portal.available_seasons()             # [("64", "Winter 2024"), ...]
portal.list_previous_courses("64")     # courses that season: [("168", "... MATH501 ..."), ...]

marks = portal.get_previous_grades("64", "168")
print(marks.course, marks.percentages) # overall % for every course that season
for item in marks.items:
    print(item.assessment, item.element, item.grade, item.evaluator)  # HW01 Question1 "3 / 3" "Dr X"
```

### Current grades (this term)

```python
portal.list_current_courses()          # [] between terms, or [(value, label), ...]
portal.get_current_grades(value)       # same CourseGrades shape as above
```

### The data you get back

- `TranscriptRow` : `semester`, `course`, `grade` (A+), `numeric` (GUC scale, e.g. 0.7),
  `hours`, `group`
- `Transcript` : `.rows`, `.cumulative_gpa`, and `.by_semester()`
- `GradeItem` : `assessment`, `element`, `grade`, `evaluator`
- `CourseGrades` : `.course`, `.season`, `.items`, `.percentages`

## Two things this portal does that the CMS did not

The client hides both, but you should know they are there:

1. **A bot-check.** The first response to any page is a scrap of JavaScript that
   bounces you to the same URL with a `?v=<token>` added. A scraper that does not
   follow it just gets a tiny stub. `_get` reads the token and follows it.
2. **The data hides behind ASP.NET dropdowns.** Nothing shows until you "select"
   a study year, which is really a form POST carrying a big `__VIEWSTATE` blob.
   `get_transcript_year` replays that POST for you.

## It is slow, and that is not a bug

The portal is rate-limited. If you hit it several times quickly it starts
returning `500`s. It needs roughly **a minute between requests**. So:

- `get_transcript_year(value)` is one request: use it for a quick look.
- `get_transcript()` walks every year and **waits ~60s between them** on purpose,
  which makes it take a few minutes. That pacing is what keeps it from failing.

## GUC or GIU

GUC and GIU run the same portal software, so one client serves both. Pick with a
flag (or the `GUC_SITE` env var); the default is GUC.

```python
GucPortal(site="guc")   # default: apps.guc.edu.eg
GucPortal(site="giu")   # portal.giu-uni.de/GIUb
```

Only the host and a few page paths differ (they live in `_sites.py`). The
dropdown ids are the same on both, and the postback field names are read off the
page instead of hard-coded, so nothing else changes. **GUC is verified; the GIU
profile is a starting point** taken from a GIU project on the same software: its
transcript path is known, its two grades paths are guesses, both to be confirmed
once a GIU login is available.

## The login

Same Windows (NTLM) login as the CMS, just a different host. `requests_ntlm`
does the handshake. Never hard-code your password; pass it in or set
`GUC_USERNAME` / `GUC_PASSWORD`.

## Scope, honestly

Read-only. Working now: transcript, previous grades, current grades. `current`
returns nothing between terms (no courses registered), but shares the exact same
page shape as `previous`, which is verified against real data.

Mapped but not built yet:

- **Financial balance** (`Financial/BalanceView_001.aspx`): a "payment requests"
  table (currency, amount, due date). The page works, but was empty on the test
  account, so nothing to model yet.
- **Attendance** (`Attendance/ClassAttendance_ViewStudentAttendance_001.aspx`):
  same mechanism (pick a current course, postback), but it only has data during a
  live term, so its exact columns are not captured yet.
