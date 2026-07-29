"""Phase 11 tests: auto-fill stops at the submit button.

Most of these exist to pin down one promise — that filling a form never sends
it. The load-bearing test drives a real page carrying a submit handler and
asserts the handler never fired.
"""

import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import app as flask_app  # noqa: E402
import autofill  # noqa: E402

PROFILE = {
    "full_name": "Ahmed Mostafa",
    "email": "ahmed@example.com",
    "nationality": "Egypt",
    "field_of_study": "Computer Science",
    "degree_level": "Bachelor's",
    "gpa": "3.6/4.0",
    "university": "German International University",
}

# A form with everything auto-fill must handle, and everything it must refuse.
PORTAL_HTML = """<!doctype html><html><body>
<form id="app" action="/submitted" method="post">
  <label for="fn">First name</label><input id="fn" name="firstName">
  <label for="ln">Family name</label><input id="ln" name="lastName">
  <label for="em">E-mail address</label><input id="em" name="email" type="email">
  <label for="nat">Nationality</label><input id="nat" name="nationality">
  <label for="fos">Field of study</label><input id="fos" name="subject">
  <label for="gpa">Final grade / GPA</label><input id="gpa" name="gpa">
  <label for="uni">Home university</label><input id="uni" name="university">
  <label for="pw">Password</label><input id="pw" name="password" type="password">
  <label for="cc">Credit card number</label><input id="cc" name="cc">
  <label><input type="checkbox" id="decl" name="declaration"> I declare this is my own work</label>
  <button type="submit" id="go">Submit application</button>
</form>
<script>
  window.__SUBMITTED__ = false;
  document.getElementById('app').addEventListener('submit', function (e) {
    e.preventDefault(); window.__SUBMITTED__ = true;
  });
</script></body></html>"""


@pytest.fixture(scope="module")
def portal(tmp_path_factory):
    """Serve the fake portal over http — file:// URLs are refused by design."""
    directory = tmp_path_factory.mktemp("portal")
    (directory / "portal.html").write_text(PORTAL_HTML)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/portal.html"
    httpd.shutdown()


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as test_client:
        yield test_client


# --- the promise -----------------------------------------------------------


def test_the_module_has_no_way_to_submit():
    """There must be no code path to submitting, not merely a decision not to."""
    assert not hasattr(autofill, "submit")
    source = Path(autofill.__file__).read_text()
    assert "THE ONE RULE: this never submits." in source
    # Nothing that could send a form.
    assert ".click(" not in source
    assert "press(" not in source
    assert "keyboard" not in source
    assert "requestSubmit" not in source
    assert ".submit()" not in source


@pytest.mark.parametrize(
    "label,element_type",
    [
        ("Submit application", "submit"),
        ("Send", ""),
        ("Apply now", ""),
        ("Confirm", ""),
        ("Absenden", ""),
        ("I declare this is my own work", "checkbox"),
        ("anything", "submit"),
        ("anything", "image"),
    ],
)
def test_submit_controls_are_recognised(label, element_type):
    assert autofill._is_submit_control(label.lower(), element_type)


@pytest.mark.parametrize(
    "label", ["Password", "Passwort", "Credit card number", "IBAN", "Captcha", "CVV"]
)
def test_sensitive_fields_are_never_filled(label):
    assert autofill.match_field({"label": label, "name": "x", "type": "text"}, PROFILE) is None


def test_live_form_is_filled_but_never_submitted(portal):
    """The load-bearing test: the page's own submit handler must never fire."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.goto(portal)

        result = autofill._fill_page(page, PROFILE)

        # It did its job...
        assert result["ok"]
        assert len(result["filled"]) == 7
        assert page.input_value("#fn") == "Ahmed"
        assert page.input_value("#ln") == "Mostafa"
        assert page.input_value("#em") == "ahmed@example.com"
        assert page.input_value("#nat") == "Egypt"

        # ...and did not do the one thing it must never do.
        assert page.evaluate("window.__SUBMITTED__") is False
        assert result["submitted"] is False
        assert "portal.html" in page.url, "the page navigated — something submitted"

        # Nothing sensitive was touched.
        assert page.input_value("#pw") == ""
        assert page.input_value("#cc") == ""
        assert page.is_checked("#decl") is False

        browser.close()


def test_a_prefilled_field_is_left_alone(portal):
    """The student's own typing must not be overwritten."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page()
        page.goto(portal)
        page.fill("#fn", "Someone Else")

        result = autofill._fill_page(page, PROFILE)

        assert page.input_value("#fn") == "Someone Else"
        assert any("already filled" in s["why"] for s in result["skipped"])
        browser.close()


# --- matching --------------------------------------------------------------


def test_the_name_is_split_across_first_and_last():
    assert autofill.match_field({"label": "First name", "name": "f", "type": "text"},
                                PROFILE) == ("first_name", "Ahmed")
    assert autofill.match_field({"label": "Family name", "name": "l", "type": "text"},
                                PROFILE) == ("last_name", "Mostafa")


def test_the_longest_match_wins():
    """'First name' must not be captured by a bare 'name' rule."""
    match = autofill.match_field({"label": "First name", "name": "name", "type": "text"},
                                 PROFILE)
    assert match[0] == "first_name"


def test_an_unrecognised_field_is_left_empty():
    assert autofill.match_field({"label": "Favourite colour", "name": "c", "type": "text"},
                                PROFILE) is None


def test_a_field_with_no_value_in_the_profile_is_skipped():
    assert autofill.match_field({"label": "Nationality", "name": "n", "type": "text"},
                                {"full_name": "X"}) is None


# --- endpoints -------------------------------------------------------------


def test_open_needs_a_real_url(client):
    response = client.post("/autofill/open", json={"url": ""})
    assert response.status_code == 400
    assert "no application link" in response.get_json()["error"].lower()


def test_a_non_http_url_is_refused():
    outcome = autofill.open_portal("file:///etc/passwd")
    assert outcome["ok"] is False
    assert "not a valid" in outcome["error"]


def test_missing_playwright_explains_itself(client, monkeypatch):
    monkeypatch.setattr(autofill, "available", lambda: False)
    response = client.post("/autofill/open", json={"url": "https://portal.daad.de/apply"})
    assert response.status_code == 503
    body = response.get_json()
    assert body["configured"] is False
    assert "pip install playwright" in body["error"]
    assert "works without it" in body["error"]


def test_fill_reports_that_nothing_was_submitted(client, monkeypatch):
    monkeypatch.setattr(
        autofill, "fill_form",
        lambda profile: {"ok": True, "filled": [{"field": "First name", "value": "Ahmed",
                                                 "matched": "first_name"}],
                         "skipped": [], "submitted": False,
                         "message": "Fields filled. Nothing has been submitted"},
    )
    response = client.post("/autofill/fill", json={"profile": PROFILE})
    assert response.status_code == 200
    body = response.get_json()
    assert body["submitted"] is False
    assert "not been submitted" in body["message"] or "Nothing has been submitted" in body["message"]


def test_profile_carries_the_identity_fields(client, monkeypatch):
    """The form fields exist so a portal form can be filled from them."""
    captured = {}
    monkeypatch.setattr(
        agent, "run_shortlist",
        lambda profile, **kw: (captured.setdefault("p", profile), {"ok": True, "results": []})[1],
    )
    client.post("/search", data={
        "field_of_study": "Computer Science", "degree_level": "Bachelor's",
        "nationality": "Egypt", "full_name": "Ahmed Mostafa",
        "email": "ahmed@example.com", "university": "GIU",
    }, content_type="multipart/form-data")

    assert captured["p"]["full_name"] == "Ahmed Mostafa"
    assert captured["p"]["email"] == "ahmed@example.com"
    assert captured["p"]["university"] == "GIU"
