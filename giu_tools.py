"""Optional GIU/GUC tools — reuses the existing local packages, does not rebuild them.

The project folder already ships two working clients:

  * ``portal-app/guc_portal``  — the student portal / SIS: transcript, GPA, grades
  * ``cms-app/guc_cms``        — the CMS: enrolled courses and their material

Neither knows anything about agents (that was left as the wrapping job), so this
module does exactly that wrapping and nothing else: it puts both packages on the
import path and exposes them as LangChain tools that fill in a student's GPA and
completed courses automatically.

Everything here is optional by design. Without ``GIU_USERNAME``/``GIU_PASSWORD``
the tools report themselves unavailable and Scholar Hunter carries on with
typed or uploaded courses — the portal must never be a hard dependency.

A note inherited from the portal package: the SIS is old and rate-limits at
roughly one request a minute. Fetching a full transcript is therefore slow, so
these tools are opt-in rather than part of the default search path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

# Reuse the packages in place. The folders arrived named `cms-app`/`portal-app`
# (the spec calls them cms/ and portal/); we import from where they actually are
# rather than copying the code into this project.
_ROOT = Path(__file__).resolve().parent
for _folder in ("portal-app", "cms-app"):
    _path = str(_ROOT / _folder)
    if (_ROOT / _folder).is_dir() and _path not in sys.path:
        sys.path.append(_path)

try:
    from guc_portal import GucPortal

    PORTAL_IMPORTED = True
except Exception:  # the folder may be absent in a trimmed checkout
    GucPortal = None
    PORTAL_IMPORTED = False

try:
    from guc_cms import GucCms

    CMS_IMPORTED = True
except Exception:
    GucCms = None
    CMS_IMPORTED = False


def credentials() -> tuple[str, str]:
    """GIU credentials from the environment, or ("", "")."""
    return (
        (os.getenv("GIU_USERNAME") or "").strip(),
        (os.getenv("GIU_PASSWORD") or "").strip(),
    )


def availability() -> dict:
    """What the GIU integration can currently do — used by the startup banner."""
    username, password = credentials()
    has_credentials = bool(username and password)
    return {
        "portal": PORTAL_IMPORTED and has_credentials,
        "cms": CMS_IMPORTED and has_credentials,
        "packages_found": PORTAL_IMPORTED or CMS_IMPORTED,
        "credentials_set": has_credentials,
    }


def _unavailable(what: str) -> dict | None:
    """Return an explanatory result when the integration cannot run, else None."""
    imported = PORTAL_IMPORTED if what == "portal" else CMS_IMPORTED
    if not imported:
        folder = "portal-app/guc_portal" if what == "portal" else "cms-app/guc_cms"
        return {
            "ok": False,
            "available": False,
            "error": f"The {folder} package was not found next to this project.",
        }
    username, password = credentials()
    if not (username and password):
        return {
            "ok": False,
            "available": False,
            "error": (
                "GIU credentials are not set. Add GIU_USERNAME and GIU_PASSWORD to "
                ".env to auto-load your GPA and courses, or just type/upload your "
                "courses instead — both work."
            ),
        }
    return None


# ---------------------------------------------------------------------------
# Portal: transcript -> GPA + completed courses
# ---------------------------------------------------------------------------


def fetch_transcript(year_value: str = "") -> dict:
    """Read the transcript from the portal and reduce it to what matching needs.

    `year_value` fetches a single academic year (one request, fast). Empty means
    the most recent year. We deliberately avoid `get_transcript()`, which walks
    every year with a 60-second wait between them.
    """
    blocked = _unavailable("portal")
    if blocked:
        return blocked

    username, password = credentials()
    try:
        portal = GucPortal(username, password)
        if not year_value:
            years = portal.available_years()
            if not years:
                return {"ok": False, "available": True, "error": "No transcript years found."}
            year_value = years[0][0]  # most recent first
        transcript = portal.get_transcript_year(year_value)
    except Exception as exc:
        return {
            "ok": False,
            "available": True,
            "error": f"Could not read the portal (it is slow and rate-limits): {exc}",
        }

    courses = [
        {"course": row.course, "grade": row.grade, "semester": row.semester}
        for row in transcript.rows
        if row.course
    ]
    return {
        "ok": True,
        "available": True,
        "cumulative_gpa": transcript.cumulative_gpa,
        "course_count": len(courses),
        "courses": courses,
        # GUC grades are numeric-lower-is-better; say so rather than let the model
        # assume a 4.0 scale and misjudge every GPA requirement.
        "gpa_note": (
            "This is a GUC/GIU cumulative GPA on the German scale, where LOWER is "
            "better (1.0 is excellent, 5.0 is failing). Do not compare it directly "
            "against a 4.0-scale requirement without converting."
        ),
    }


class TranscriptInput(BaseModel):
    year_value: str = Field(
        default="",
        description="Optional academic-year code from the portal. Leave empty for "
        "the most recent year (one request; the portal rate-limits).",
    )


@tool("get_giu_transcript", args_schema=TranscriptInput)
def get_giu_transcript(year_value: str = "") -> str:
    """Load the student's GPA and completed courses from their GIU/GUC portal.

    Use this only when the student asks you to pull their record automatically
    and has GIU credentials configured. It is slow (the portal rate-limits), so
    prefer courses the student typed or uploaded when those are available. If it
    is unavailable, say so and continue — it is optional.
    """
    return json.dumps(fetch_transcript(year_value), indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CMS: currently enrolled courses
# ---------------------------------------------------------------------------


def fetch_cms_courses() -> dict:
    """List the courses the student is enrolled in, from the CMS."""
    blocked = _unavailable("cms")
    if blocked:
        return blocked

    username, password = credentials()
    try:
        courses = GucCms(username, password).list_courses()
    except Exception as exc:
        return {"ok": False, "available": True, "error": f"Could not read the CMS: {exc}"}

    return {
        "ok": True,
        "available": True,
        "course_count": len(courses),
        "courses": [
            {"code": c.code, "title": c.title, "active": c.active} for c in courses
        ],
    }


@tool("get_giu_cms_courses")
def get_giu_cms_courses() -> str:
    """List the courses the student is currently enrolled in, from the GIU/GUC CMS.

    Useful for filling in recent coursework the transcript does not show yet.
    Optional — reports itself unavailable when credentials are absent.
    """
    return json.dumps(fetch_cms_courses(), indent=2, ensure_ascii=False)


# Only surface tools whose underlying package actually imported.
def optional_tools() -> list:
    tools = []
    if PORTAL_IMPORTED:
        tools.append(get_giu_transcript)
    if CMS_IMPORTED:
        tools.append(get_giu_cms_courses)
    return tools
