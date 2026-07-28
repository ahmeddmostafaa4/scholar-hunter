"""The one object you talk to: `GucPortal`.

The GUC student portal (apps.guc.edu.eg/student_ext) is trickier than the CMS,
in two ways this client hides for you:

  1. A bot-check. The first response to any page is a scrap of JavaScript that
     bounces you to the SAME url with a `?v=<token>` added. We read that token
     and follow it.
  2. The data hides behind ASP.NET dropdowns. Nothing shows until you "select"
     a value, which is really a form POST carrying a big __VIEWSTATE blob. We
     replay that POST for you.

With both handled, reading the transcript is a normal method call.
"""

from __future__ import annotations

import os
import re
import time

import requests
from bs4 import BeautifulSoup
from requests_ntlm import HttpNtlmAuth

from ._parse import (
    merge_transcript,
    parse_course_grades,
    parse_options,
    parse_transcript_year,
)
from ._sites import PORTAL_SITES
from .models import CourseGrades, Transcript

BASE_URL = "https://apps.guc.edu.eg"  # kept for backward compatibility (GUC)

# The dropdown id-SUFFIXES are the same on GUC and GIU. The full field NAME (with
# its ContentPlaceHolder prefix) differs between them, so we never hard-code it:
# `_field_name` reads it off the page by matching this suffix.
YEAR_SUFFIX = "stdYrLst"
COURSE_SUFFIX = "smCrsLst"  # same field on both grade pages
# the season dropdown id differs by site, so it lives in the site profile
# (self.site.season_suffix): "Dropdownlistseason" on GUC, "ddl_season" on GIU.


# The portal is flaky: it returns 500s, times out, drops connections, and
# sometimes hands back a stub page with no dropdown (which surfaces as a
# RuntimeError from _field_name). All of these are worth a retry.
_RETRYABLE = (requests.RequestException, RuntimeError)


def _match(options: list[tuple[str, str]], query: str) -> tuple[str | None, str | None]:
    """First (value, label) whose label contains `query`, case-insensitive."""
    needle = query.strip().lower()
    for value, label in options:
        if needle in label.lower():
            return value, label
    return None, None


def _field_name(html: str, id_suffix: str) -> str:
    """The form `name` of the dropdown whose id ends with `id_suffix`.

    This is what keeps the client portable across GUC and GIU: the id-suffix is
    shared, but the full name differs by its ContentPlaceHolder prefix, so we
    read the real name from the page rather than assuming a prefix.

    Raises RuntimeError if the field is missing, which usually means the portal
    handed back a throttle stub instead of the real page. Callers retry on that.
    """
    soup = BeautifulSoup(html, "lxml")
    element = soup.find("select", id=re.compile(id_suffix)) or soup.find(id=re.compile(id_suffix))
    if element and element.get("name"):
        return element["name"]
    raise RuntimeError(f"Could not find the {id_suffix!r} field on the page.")


def _student_name(html: str) -> str | None:
    """Best-effort: pull the logged-in student's name from a portal page."""
    soup = BeautifulSoup(html, "lxml")
    for pattern in ("lbl_Account", "LabelStudentName", "LabelName", "actor", "Account"):
        element = soup.find(id=re.compile(pattern, re.I))
        if element:
            text = element.get_text(" ", strip=True)
            if text:
                return text[:60]
    return None


class GucPortal:
    """A logged-in connection to a student portal / SIS (GUC or GIU).

    Same Windows (NTLM) login as the CMS. Pick which university with `site="guc"`
    (default) or `site="giu"`, or set the GUC_SITE environment variable. GUC is
    verified; GIU is a starting point. Credentials come from the arguments, or
    from GUC_USERNAME / GUC_PASSWORD in the environment.
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        site: str | None = None,
        base_url: str | None = None,
        verify: bool = True,
        timeout: int = 60,
    ) -> None:
        username = username or os.environ.get("GUC_USERNAME")
        password = password or os.environ.get("GUC_PASSWORD")
        if not username or not password:
            raise ValueError(
                "Missing credentials: pass username/password, or set "
                "GUC_USERNAME and GUC_PASSWORD in your environment."
            )
        site = (site or os.environ.get("GUC_SITE") or "guc").lower()
        if site not in PORTAL_SITES:
            raise ValueError(f"Unknown site {site!r}. Known sites: {list(PORTAL_SITES)}.")
        self.site = PORTAL_SITES[site]
        self.base_url = (base_url or self.site.base_url).rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = HttpNtlmAuth(username, password)
        self.session.verify = verify

    # -- the two portal quirks, handled once --------------------------------

    def _get(self, path: str) -> tuple[str, str]:
        """GET a page, following the bot-check redirect. Returns (html, url).

        On GIU a cold session bounces some pages (the grade pages) to Home with a
        302; that first hit warms the session, so if we land somewhere other than
        what we asked for, we simply ask again and it stays put. GUC never does
        this, so the retry is a no-op there.
        """
        response = self.session.get(self.base_url + path, timeout=self.timeout)
        response.raise_for_status()
        wanted = path.split("?")[0].rsplit("/", 1)[-1].lower()
        landed = response.url.split("?")[0].rsplit("/", 1)[-1].lower()
        if response.history and wanted and wanted != landed:
            # Bounced elsewhere (GIU sends the grade pages to Home on a cold
            # session). A DIRECT load of the dashboard warms the session; then the
            # page opens. Landing on Home via the bounce is not enough.
            self.session.get(self.base_url + self.site.home_path, timeout=self.timeout)
            response = self.session.get(self.base_url + path, timeout=self.timeout)
            response.raise_for_status()
        token = re.search(r"sTo\('([^']+)'\)", response.text)
        if not token:
            return response.text, self.base_url + path
        sep = "&" if "?" in path else "?"
        url = f"{self.base_url}{path}{sep}v={token.group(1)}"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        return response.text, url

    def _postback(
        self,
        url: str,
        html: str,
        target: str,
        value: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> str:
        """Replay an ASP.NET dropdown selection (a read-only view postback).

        We resend every hidden input AND each dropdown that already has a
        selection, so a cascading form (pick a season, then a course) keeps the
        earlier choice. We do NOT send a dropdown that has no selected option
        (like the still-empty course list while you are choosing the season):
        posting an empty value for it makes ASP.NET reject the whole request with
        a 500. `value` sets the target dropdown; `extra` overrides other fields.
        """
        soup = BeautifulSoup(html, "lxml")
        data: dict[str, str] = {}
        for inp in soup.find_all("input"):
            name = inp.get("name")
            if name:
                data[name] = inp.get("value", "")
        for select in soup.find_all("select"):
            name = select.get("name")
            chosen = select.find("option", selected=True)
            if name and chosen:  # skip dropdowns with nothing selected yet
                data[name] = chosen.get("value", "")
        if value is not None:
            data[target] = value
        if extra:
            data.update(extra)
        data["__EVENTTARGET"] = target
        data["__EVENTARGUMENT"] = ""
        response = self.session.post(url, data=data, timeout=self.timeout)
        response.raise_for_status()
        return response.text

    # -- login check --------------------------------------------------------

    def verify_login(self) -> str:
        """Actually confirm the login worked, instead of assuming it did.

        Building a `GucPortal` makes no network call, so a wrong password is not
        noticed until the first real request. This makes that request now: it
        hits an authenticated page and, if the portal rejects the credentials
        (HTTP 401/403), raises a clear error. Returns a short confirmation, with
        your name if the page shows it.
        """
        resp = self.session.get(
            self.base_url + self.site.prev_grades_path, timeout=self.timeout
        )
        if resp.status_code in (401, 403):
            raise PermissionError(
                "Login failed: the portal did not accept your username or password."
            )
        resp.raise_for_status()
        html = resp.text
        token = re.search(r"sTo\('([^']+)'\)", html)  # follow the bot-check for the real page
        if token:
            sep = "&" if "?" in self.site.prev_grades_path else "?"
            resp = self.session.get(
                f"{self.base_url}{self.site.prev_grades_path}{sep}v={token.group(1)}",
                timeout=self.timeout,
            )
            if resp.status_code in (401, 403):
                raise PermissionError(
                    "Login failed: the portal did not accept your username or password."
                )
            html = resp.text
        name = _student_name(html)
        return f"Logged in as {name}." if name else "Logged in (verified with the portal)."

    # -- transcript ---------------------------------------------------------

    def available_years(self) -> list[tuple[str, str]]:
        """The study years the transcript offers: [(value, label), ...].

        The label is what you read ("2024-2025"); the value is what you pass to
        `get_transcript_year`. One request.
        """
        html, _ = self._get(self.site.transcript_path)
        year_select = BeautifulSoup(html, "lxml").find("select", id=re.compile(YEAR_SUFFIX))
        if not year_select:
            raise RuntimeError("Could not find the study-year dropdown on the page.")
        return [
            (opt.get("value"), opt.get_text(strip=True))
            for opt in year_select.find_all("option")
            if opt.get("value") and opt.get("value").strip()
        ]

    def get_transcript_year(
        self, year_value: str, *, tries: int = 3, delay: float = 60.0
    ) -> Transcript:
        """Transcript for ONE study year (its semesters, plus the GPA label).

        Normally a single page load and one postback. Pick a `year_value` from
        `available_years()`.

        The transcript endpoint is the flakiest one: under load it sometimes
        hands back a stub with no dropdown instead of the real page. So this
        retries after a `delay` wait on either a 500 or a missing-field stub.
        """
        last: Exception | None = None
        for _ in range(tries):
            try:
                html, url = self._get(self.site.transcript_path)
                year_html = self._postback(url, html, _field_name(html, YEAR_SUFFIX), year_value)
                rows, gpa = parse_transcript_year(year_html)
                return Transcript(rows=rows, cumulative_gpa=gpa)
            except _RETRYABLE as err:
                last = err
                time.sleep(delay)
        raise last  # type: ignore[misc]

    def get_transcript(self, *, delay: float = 60.0, tries: int = 2) -> Transcript:
        """The FULL transcript, walking every study year with the GPA.

        Because the portal needs breathing room, this paces itself: it waits
        `delay` seconds between years. That makes it slow (minutes), on purpose.
        For a quick look at one year, use `get_transcript_year` instead.

        Newest years are often empty (future) and oldest are before the student
        enrolled, so we skip while empty, collect the real years, and stop after
        two empty years in a row.
        """
        years = self.available_years()
        per_year: list = []
        started = False
        empty_streak = 0

        for index, (value, _label) in enumerate(years):
            if index:
                time.sleep(delay)  # give the server room between requests
            rows, gpa = self._year_with_retry(value, tries=tries, delay=delay)
            per_year.append((rows, gpa))
            if rows:
                started, empty_streak = True, 0
            elif started:
                empty_streak += 1
                if empty_streak >= 2:
                    break
        return merge_transcript(per_year)

    def _year_with_retry(self, value: str, *, tries: int, delay: float):
        """Fetch one year, retrying on a 500 or a missing-field stub after a wait."""
        last: Exception | None = None
        for attempt in range(tries):
            try:
                html, url = self._get(self.site.transcript_path)
                year_html = self._postback(url, html, _field_name(html, YEAR_SUFFIX), value)
                return parse_transcript_year(year_html)
            except _RETRYABLE as err:
                last = err
                time.sleep(delay)
        raise last  # type: ignore[misc]

    # -- previous grades (per-course coursework marks) ----------------------

    def available_seasons(self) -> list[tuple[str, str]]:
        """Past seasons on the previous-grades page: [(value, label), ...]."""
        html, _ = self._get(self.site.prev_grades_path)
        return parse_options(html, self.site.season_suffix)

    def list_previous_courses(self, season_value: str) -> list[tuple[str, str]]:
        """Courses taught in a past season: [(value, label), ...].

        Selecting a season fills the course dropdown, so this is one postback.
        """
        html, url = self._get(self.site.prev_grades_path)
        after_season = self._postback(url, html, _field_name(html, self.site.season_suffix), season_value)
        return parse_options(after_season, COURSE_SUFFIX)

    def get_previous_grades(
        self, season_value: str, course_value: str, *, tries: int = 3, delay: float = 60.0
    ) -> CourseGrades:
        """Coursework marks for one course in a past season.

        Two selections in a row: the season (which fills the course list), then
        the course (which shows its marks). Pick values from `available_seasons`
        and `list_previous_courses`.

        The portal 500s under load, so this retries the whole lookup after a
        `delay` wait if the server balks.
        """
        last: Exception | None = None
        for _ in range(tries):
            try:
                html, url = self._get(self.site.prev_grades_path)
                season_field = _field_name(html, self.site.season_suffix)
                after_season = self._postback(url, html, season_field, season_value)
                course_field = _field_name(after_season, COURSE_SUFFIX)
                after_course = self._postback(
                    url, after_season, course_field, course_value, extra={season_field: season_value}
                )
                course_label = dict(parse_options(after_season, COURSE_SUFFIX)).get(
                    course_value, course_value
                )
                season_label = dict(parse_options(html, self.site.season_suffix)).get(season_value)
                return parse_course_grades(after_course, course=course_label, season=season_label)
            except _RETRYABLE as err:
                last = err
                time.sleep(delay)
        raise last  # type: ignore[misc]

    def get_grades_by_name(
        self, semester_name: str, course_name: str, *, tries: int = 3, delay: float = 60.0
    ) -> CourseGrades:
        """Like `get_previous_grades`, but you pass names instead of ids.

        `semester_name` matches a season label ("Winter 2024") and `course_name`
        matches a course label ("Discrete Math"), both case-insensitive substring.
        This is the convenient one to wrap as a tool: the model speaks names, not
        the portal's numeric ids. It does the whole cascade in three requests.
        """
        last: Exception | None = None
        for _ in range(tries):
            try:
                html, url = self._get(self.site.prev_grades_path)
                seasons = parse_options(html, self.site.season_suffix)
                season_value, season_label = _match(seasons, semester_name)
                if not season_value:
                    raise ValueError(f"No season matches {semester_name!r}. Seasons: {seasons}")
                season_field = _field_name(html, self.site.season_suffix)
                after_season = self._postback(url, html, season_field, season_value)
                courses = parse_options(after_season, COURSE_SUFFIX)
                course_value, course_label = _match(courses, course_name)
                if not course_value:
                    raise ValueError(
                        f"No course matches {course_name!r} in {season_label}. Courses: {courses}"
                    )
                course_field = _field_name(after_season, COURSE_SUFFIX)
                after_course = self._postback(
                    url, after_season, course_field, course_value, extra={season_field: season_value}
                )
                return parse_course_grades(after_course, course=course_label, season=season_label)
            except _RETRYABLE as err:
                last = err
                time.sleep(delay)
        raise last  # type: ignore[misc]

    # -- current grades (this term) -----------------------------------------

    def list_current_courses(self) -> list[tuple[str, str]]:
        """Courses in the current term on the check-grades page.

        Empty between terms, when nothing is registered.
        """
        html, _ = self._get(self.site.curr_grades_path)
        return parse_options(html, COURSE_SUFFIX)

    def get_current_grades(
        self, course_value: str, *, tries: int = 3, delay: float = 60.0
    ) -> CourseGrades:
        """Coursework marks for one current-term course.

        Same page shape as previous grades, minus the season step. Retries on the
        portal's occasional 500 after a `delay` wait.
        """
        last: Exception | None = None
        for _ in range(tries):
            try:
                html, url = self._get(self.site.curr_grades_path)
                after_course = self._postback(url, html, _field_name(html, COURSE_SUFFIX), course_value)
                course_label = dict(parse_options(html, COURSE_SUFFIX)).get(course_value, course_value)
                return parse_course_grades(after_course, course=course_label, season=None)
            except _RETRYABLE as err:
                last = err
                time.sleep(delay)
        raise last  # type: ignore[misc]
