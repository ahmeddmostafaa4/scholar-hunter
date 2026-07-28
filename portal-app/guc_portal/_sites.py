"""Which university portal are we talking to?

GUC (Cairo) and GIU run the same SIS/portal software, so one client works for
both. Only the host and a few page paths differ, and they live here. Pick a site
with `GucPortal(site="guc")` or the GUC_SITE env var.

The dropdown id-suffixes (stdYrLst, cmGpaLbl, Dropdownlistseason, smCrsLst) are
the SAME on both, which is why the parsing needs no per-site tweak. Only the
control-id PREFIX differs, and we handle that by reading each field's real name
off the page instead of hard-coding it.

GUC is verified against the live portal. GIU is a starting point taken from a GIU
project on the same software: the transcript path is known, the grades paths are
best guesses. Confirm them once a GIU login is available.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortalSite:
    base_url: str
    transcript_path: str
    prev_grades_path: str
    curr_grades_path: str


PORTAL_SITES: dict[str, PortalSite] = {
    "guc": PortalSite(
        base_url="https://apps.guc.edu.eg",
        transcript_path="/student_ext/Grade/Transcript_001.aspx",
        prev_grades_path="/student_ext/Grade/CheckGradePerviousSemester_01.aspx",
        curr_grades_path="/student_ext/Grade/CheckGrade_01.aspx",
    ),
    # UNVERIFIED. Host and transcript path are from a GIU project on the same
    # portal; the two grades paths below are GUESSES following GIU's "_m.aspx"
    # naming and MUST be confirmed with a GIU login before trusting them.
    "giu": PortalSite(
        base_url="https://portal.giu-uni.de/GIUb",
        transcript_path="/EXTStudent/Transcript_m.aspx",
        prev_grades_path="/EXTStudent/CheckGradePreviousSemester_m.aspx",  # GUESS
        curr_grades_path="/EXTStudent/CheckGrade_m.aspx",  # GUESS
    ),
}
