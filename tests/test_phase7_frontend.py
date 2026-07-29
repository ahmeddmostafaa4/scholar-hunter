"""Phase 7 tests: the page behaves in a real browser.

Driven with Playwright against a live Flask server whose agent is stubbed, so
these run in seconds and test the UI rather than the model. Skipped entirely if
Playwright is not installed — it is a dev-only extra, not a runtime dependency.

    pip install playwright && playwright install chromium
"""

import json
import socket
import threading
import time

import pytest

pytest.importorskip("playwright", reason="playwright not installed (dev-only)")

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import app as flask_app  # noqa: E402

CARD = {
    "name": "DAAD Study Scholarship",
    "type": "scholarship",
    "institution": "DAAD",
    "fit": "Matches your Computer Science background and your goal of German funding.",
    "verdict": "eligible",
    "verdict_reason": "All stated requirements are met.",
    "requirements": [
        {"label": "Minimum GPA", "required": "3.0/4.0", "student": "3.6/4.0",
         "status": "met", "note": ""},
        {"label": "Language", "required": "IELTS 6.5", "student": "not provided",
         "status": "not_stated", "note": "No score supplied."},
    ],
    "course_match": {"assessed": True, "summary": "2 of 3 required courses matched",
                     "matched": 2, "partial": 0, "total": 3,
                     "confidence_capped": False, "matches": []},
    "documents": [
        "Motivation letter (max 2 pages)",
        "CV in tabular form",
        "Two academic letters of recommendation",
    ],
    "deadline": "15 January 2099",
    "deadline_status": "open",
    "funding": "Full tuition plus a monthly stipend",
    "url": "https://www2.daad.de/example",
    "trusted_source": True,
}

GUIDANCE = {
    "ok": True,
    "assessed": True,
    "guidance": [
        {
            "document": "Motivation letter (max 2 pages)",
            "summary": "Convince DAAD your CS background fits.",
            "steps": ["Open with a concrete moment from your AI coursework.",
                      "Name a specific German research group."],
            "watch_out": "A generic letter that could suit any country.",
        }
    ],
}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    """A real Flask server with the agent stubbed out.

    The originals are restored on teardown. Assigning to `agent.*` without
    restoring leaks the stubs into every test that runs afterwards, which
    produced failures that only appeared in a full-suite run and vanished when
    the offending file was run on its own.
    """
    originals = {
        name: getattr(agent, name)
        for name in (
            "run_shortlist",
            "save_deadline_entry",
            "gmail_available",
            "document_guidance",
        )
    }
    agent.document_guidance = lambda docs, **kw: dict(GUIDANCE)

    agent.run_shortlist = lambda profile, **kw: {
        "ok": True, "results": [CARD], "considered": 9,
        "skipped_listings": 3, "skipped_ineligible": 5,
        "message": "Skipped 5 clearly ineligible or expired and 3 directory page(s).",
    }
    agent.save_deadline_entry = lambda *a, **kw: {
        "ok": True, "saved": True, "entry": {}, "total_saved": 1
    }
    agent.gmail_available = lambda: False  # the common local case

    port = _free_port()
    thread = threading.Thread(
        target=lambda: flask_app.app.run(host="127.0.0.1", port=port, use_reloader=False),
        daemon=True,
    )
    thread.start()
    time.sleep(1.5)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        for name, original in originals.items():
            setattr(agent, name, original)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binary not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    page = browser.new_page(viewport={"width": 1100, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server, wait_until="networkidle")
    yield page
    assert not errors, f"JavaScript errors on the page: {errors}"
    page.close()


def _fill_valid(page):
    page.fill("#field_of_study", "Computer Science")
    page.select_option("#degree_level", "Bachelor's")
    page.fill("#nationality", "Egypt")


# --- initial state ---------------------------------------------------------


def test_hero_and_form_render(page):
    assert page.is_visible(".hero__title")
    assert page.inner_text(".hero__title") == "Scholar Hunter"
    assert page.is_visible("#profile-form")
    assert page.locator(".chip").count() == 3


def test_error_banner_and_loading_are_hidden_on_load(page):
    """Regression: a display rule on .banner beat the [hidden] attribute, so the
    error banner greeted every visitor before they had done anything."""
    assert not page.is_visible("#error-banner")
    assert not page.is_visible("#loading")


def test_course_tabs_switch_panels(page):
    assert page.is_visible("#courses")
    assert not page.is_visible("#courses_file")

    page.click('.tab[data-tab="upload"]')
    assert page.is_visible("#courses_file")
    assert not page.is_visible("#courses")


# --- validation ------------------------------------------------------------


def test_missing_required_fields_shows_the_styled_banner_not_an_alert(page):
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    page.click("#submit-btn")
    assert page.is_visible("#error-banner")
    assert "required" in page.inner_text("#error-banner").lower()
    assert not dialogs, "validation must not use a browser alert"


# --- the happy path --------------------------------------------------------


def test_submitting_renders_a_uniform_card(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    page.click(".toggle")  # the detail lives behind View more
    page.wait_for_selector(".result__details:visible", timeout=5000)

    text = page.locator(".result").first.inner_text()

    # Every section, all present.
    assert "DAAD Study Scholarship" in text
    assert "FIT" in text
    assert "REQUIREMENTS & ELIGIBILITY" in text
    assert "COURSE MATCH" in text
    assert "DOCUMENTS TO SUBMIT" in text
    assert "FUNDING" in text
    assert "https://www2.daad.de/example" in text


# --- collapse / view more --------------------------------------------------


def test_cards_start_collapsed_showing_only_the_essentials(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)

    # Hidden until asked for.
    assert not page.is_visible(".result__details")
    assert "View more" in page.inner_text(".toggle")

    # But enough to judge and act on stays visible.
    card = page.locator(".result").first.inner_text()
    assert "DAAD Study Scholarship" in card
    assert "Eligible" in card
    assert "15 January 2099" in card
    assert "https://www2.daad.de/example" in card


def test_view_more_expands_and_collapses_again(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    toggle = page.locator(".toggle").first

    toggle.click()
    page.wait_for_selector(".result__details:visible", timeout=5000)
    assert toggle.get_attribute("aria-expanded") == "true"
    assert "View less" in toggle.inner_text()

    toggle.click()
    assert not page.is_visible(".result__details")
    assert toggle.get_attribute("aria-expanded") == "false"
    assert "View more" in toggle.inner_text()


# --- documents and guidance ------------------------------------------------


def test_required_documents_are_listed(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    page.click(".toggle")
    page.wait_for_selector(".docs li", timeout=5000)

    assert page.locator(".docs li").count() == 3
    assert "Motivation letter (max 2 pages)" in page.inner_text(".docs")


def test_documents_appear_above_the_funding_section(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    page.click(".toggle")
    page.wait_for_selector(".docs", timeout=5000)

    docs_y = page.locator(".docs").bounding_box()["y"]
    funding_y = page.locator(".facts").last.bounding_box()["y"]
    assert docs_y < funding_y


def test_guidance_loads_only_when_the_card_is_expanded(page):
    """It costs a model call, so a shortlist the student skims must not pay for it."""
    calls = []
    page.route("**/document_help", lambda route: (
        calls.append(1),
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(GUIDANCE)),
    )[-1])

    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    page.wait_for_timeout(600)
    assert calls == [], "guidance must not be fetched for a collapsed card"

    page.click(".toggle")
    page.wait_for_selector(".doc-guide", timeout=10000)
    assert len(calls) == 1

    # Collapsing and re-expanding must not pay for it twice.
    page.click(".toggle")
    page.click(".toggle")
    page.wait_for_timeout(600)
    assert len(calls) == 1, "guidance should be cached after the first load"


def test_guidance_shows_steps_and_the_warning(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    page.click(".toggle")
    page.wait_for_selector(".doc-guide", timeout=10000)

    text = page.inner_text(".doc-help")
    assert "Motivation letter" in text
    assert "German research group" in text
    assert "Watch out" in text


def test_a_guidance_failure_shows_a_styled_message_not_a_crash(page):
    page.route("**/document_help", lambda route: route.fulfill(
        status=502, content_type="application/json",
        body='{"ok": false, "error": "Could not prepare document guidance."}'))

    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    page.click(".toggle")
    page.wait_for_selector(".doc-help__error", timeout=10000)

    assert "Could not prepare" in page.inner_text(".doc-help__error")


def test_eligibility_badge_uses_icon_and_label_not_colour_alone(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".badge", timeout=15000)

    badge = page.locator(".badge").first
    assert "Eligible" in badge.inner_text()
    assert "✓" in badge.inner_text()
    assert "badge--eligible" in badge.get_attribute("class")


def test_requirement_rows_show_per_requirement_status(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    page.click(".toggle")  # the checklist lives behind View more
    page.wait_for_selector(".reqs li:visible", timeout=5000)

    rows = page.locator(".reqs li")
    assert rows.count() == 2
    assert "met" in rows.nth(0).inner_text()
    assert "not stated" in rows.nth(1).inner_text()


def test_course_match_summary_and_bar(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)
    page.click(".toggle")  # the course match lives behind View more
    page.wait_for_selector(".course-match:visible", timeout=5000)

    assert "2 of 3 required courses matched" in page.inner_text(".course-match")
    width = page.locator(".bar__fill").first.get_attribute("style")
    assert "67%" in width or "66%" in width


def test_summary_reports_what_was_skipped(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector("#results-summary:visible", timeout=15000)

    summary = page.inner_text("#results-summary")
    assert "9 pages checked" in summary
    assert "directory" in summary


# --- card actions ----------------------------------------------------------


def test_save_deadline_shows_an_inline_confirmation(page):
    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)

    button = page.locator('[data-action="deadline"]').first
    button.click()
    page.wait_for_selector(".btn.is-done", timeout=10000)
    assert "Deadline saved" in button.inner_text()


def test_save_deadline_is_disabled_when_the_page_states_none(page):
    """A button that cannot work should say so before the click, not after."""
    page.route("**/search", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"ok": true, "results": [{"name": "No Deadline Programme",'
             '"type": "scholarship", "institution": "X", "fit": "f",'
             '"verdict": "unclear", "verdict_reason": "", "requirements": [],'
             '"course_match": {"assessed": false, "summary": "not assessed"},'
             '"deadline": "not stated", "deadline_status": "not stated",'
             '"funding": "not stated", "url": "https://x.org/a",'
             '"trusted_source": false}], "considered": 1}'))

    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)

    button = page.locator('[data-action="deadline"]').first
    assert button.is_disabled()
    assert "no deadline" in (button.get_attribute("title") or "").lower()


def test_draft_email_without_gmail_explains_itself_on_the_card(page):
    """No popup, no silent failure — the message belongs on the card it concerns."""
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".result", timeout=15000)

    page.locator('[data-action="draft"]').first.click()
    page.wait_for_selector(".action-note", timeout=10000)

    note = page.inner_text(".action-note")
    assert "not configured" in note
    assert "credentials.json" in note
    assert not dialogs, "an unconfigured Gmail must not raise a browser alert"


# --- errors and layout -----------------------------------------------------


def test_server_error_shows_the_styled_banner(page, monkeypatch):
    page.route("**/search", lambda route: route.fulfill(
        status=500, content_type="application/json",
        body='{"ok": false, "error": "The search could not be completed."}'))

    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector("#error-banner:visible", timeout=15000)

    assert "could not be completed" in page.inner_text("#error-banner")
    assert not page.is_visible("#loading"), "the spinner must stop on failure"


def test_empty_results_say_nothing_was_invented(page):
    page.route("**/search", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"ok": true, "results": [], "message": "Nothing has been invented to fill the gap."}'))

    _fill_valid(page)
    page.click("#submit-btn")
    page.wait_for_selector(".empty", timeout=15000)
    assert "invented" in page.inner_text(".empty")


def test_layout_reflows_to_one_column_on_mobile(browser, server):
    page = browser.new_page(viewport={"width": 390, "height": 780})
    page.goto(server, wait_until="networkidle")

    # The two-up field grid collapses to a single column.
    columns = page.evaluate(
        "getComputedStyle(document.querySelector('.grid')).gridTemplateColumns"
    )
    assert len(columns.split()) == 1, columns

    # And nothing overflows horizontally.
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"page scrolls sideways by {overflow}px"
    page.close()
