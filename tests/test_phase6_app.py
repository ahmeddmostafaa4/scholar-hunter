"""Phase 6 tests: the Flask endpoints.

The agent is mocked here on purpose — Phase 4 already proved the real agent
works, and these tests are about the HTTP contract: right shape on success,
clean JSON on failure, never a stack trace.
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import app as flask_app  # noqa: E402


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as test_client:
        yield test_client


VALID_PROFILE = {
    "field_of_study": "Computer Science",
    "degree_level": "Bachelor's",
    "gpa": "3.6/4.0",
    "nationality": "Egypt",
    "interests": "machine learning funding in Germany",
}

# One card in the uniform shape the frontend renders.
SAMPLE_ITEM = {
    "name": "DAAD Study Scholarship",
    "type": "scholarship",
    "institution": "DAAD",
    "fit": "Matches your CS background and your goal of Master's funding in Germany.",
    "verdict": "eligible",
    "verdict_reason": "All stated requirements are met.",
    "requirements": [
        {"label": "Minimum GPA", "required": "3.0/4.0", "student": "3.6/4.0",
         "status": "met", "note": ""}
    ],
    "course_match": {"assessed": True, "summary": "2 of 3 required courses matched",
                     "matched": 2, "partial": 0, "total": 3,
                     "confidence_capped": False, "matches": []},
    "deadline": "15 January 2027",
    "deadline_status": "open",
    "funding": "Full tuition plus monthly stipend",
    "url": "https://daad.de/example",
    "trusted_source": True,
}


# --- GET / -----------------------------------------------------------------


def test_index_serves_html(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Scholar Hunter" in response.data


def test_health_reports_configuration(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert "gmail_drafting" in body
    assert "tools" in body


# --- POST /search ----------------------------------------------------------


def test_search_returns_uniform_items(client, monkeypatch):
    monkeypatch.setattr(
        agent, "run_shortlist",
        lambda profile, **kw: {"ok": True, "results": [SAMPLE_ITEM], "query": "q"},
    )
    response = client.post("/search", json=VALID_PROFILE)
    assert response.status_code == 200

    body = response.get_json()
    assert body["ok"] is True
    item = body["results"][0]
    # The six bullets the frontend renders, all present on every card.
    for field in ("name", "type", "fit", "verdict", "requirements",
                  "course_match", "deadline", "funding", "url"):
        assert field in item, f"card is missing {field}"


def test_search_without_a_body_is_a_clean_400(client):
    response = client.post("/search", data="not json", content_type="text/plain")
    assert response.status_code == 400
    body = response.get_json()
    assert body["ok"] is False
    assert body["error"]
    assert "Traceback" not in json.dumps(body)


def test_search_with_missing_profile_fields_says_which(client):
    response = client.post("/search", json={"field_of_study": "Computer Science"})
    assert response.status_code == 400
    body = response.get_json()
    assert body["ok"] is False
    assert body["needs_profile"], body
    assert any("degree" in f for f in body["needs_profile"])


def test_search_failure_is_json_not_a_stack_trace(client, monkeypatch):
    def explode(profile, **kw):
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(agent, "run_shortlist", explode)
    response = client.post("/search", json=VALID_PROFILE)
    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert "Traceback" not in json.dumps(body)


def test_search_missing_api_key_is_503(client, monkeypatch):
    def no_key(profile, **kw):
        raise agent.MissingKeyError("ANTHROPIC_API_KEY is not set. Add it to .env.")

    monkeypatch.setattr(agent, "run_shortlist", no_key)
    response = client.post("/search", json=VALID_PROFILE)
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.get_json()["error"]


def test_empty_shortlist_says_so_rather_than_inventing(client, monkeypatch):
    monkeypatch.setattr(
        agent, "run_shortlist",
        lambda profile, **kw: {"ok": True, "results": [], "message": "No opportunities were found."},
    )
    response = client.post("/search", json=VALID_PROFILE)
    assert response.status_code == 200
    body = response.get_json()
    assert body["results"] == []
    assert body["message"]


# --- file uploads ----------------------------------------------------------


def test_txt_upload_becomes_courses(client, monkeypatch):
    captured = {}

    def capture(profile, **kw):
        captured["profile"] = profile
        return {"ok": True, "results": []}

    monkeypatch.setattr(agent, "run_shortlist", capture)

    data = dict(VALID_PROFILE)
    data["courses_file"] = (io.BytesIO(b"Linear Algebra\nIntro to AI\n"), "courses.txt")
    response = client.post("/search", data=data, content_type="multipart/form-data")

    assert response.status_code == 200
    assert captured["profile"]["courses"] == ["Linear Algebra", "Intro to AI"]


def test_csv_upload_joins_each_row(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        agent, "run_shortlist",
        lambda profile, **kw: (captured.setdefault("profile", profile), {"ok": True, "results": []})[1],
    )

    data = dict(VALID_PROFILE)
    csv_bytes = b"Course,Description\nIntro to AI,search and machine learning\n"
    data["courses_file"] = (io.BytesIO(csv_bytes), "transcript.csv")
    client.post("/search", data=data, content_type="multipart/form-data")

    courses = captured["profile"]["courses"]
    assert "Intro to AI search and machine learning" in courses


def test_pdf_upload_is_parsed():
    """Round-trip a real generated PDF rather than trusting the code path blind."""
    pypdf = pytest.importorskip("pypdf")
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    parsed = flask_app.parse_courses_file("transcript.pdf", buffer.getvalue())
    # A blank page yields no text — the tool must say so, not return nonsense.
    assert parsed["ok"] is False
    assert "scanned image" in parsed["error"] or "No text" in parsed["error"]


def test_unsupported_upload_type_is_rejected():
    parsed = flask_app.parse_courses_file("transcript.docx", b"data")
    assert parsed["ok"] is False
    assert ".txt" in parsed["error"]


def test_typed_and_uploaded_courses_are_combined(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        agent, "run_shortlist",
        lambda profile, **kw: (captured.setdefault("profile", profile), {"ok": True, "results": []})[1],
    )

    data = dict(VALID_PROFILE)
    data["courses"] = "Typed Course One"
    data["courses_file"] = (io.BytesIO(b"Uploaded Course Two"), "c.txt")
    client.post("/search", data=data, content_type="multipart/form-data")

    assert captured["profile"]["courses"] == ["Typed Course One", "Uploaded Course Two"]


# --- POST /draft_email -----------------------------------------------------


def test_draft_email_without_gmail_is_a_clear_503(client, monkeypatch):
    monkeypatch.setattr(agent, "gmail_available", lambda: False)
    response = client.post(
        "/draft_email",
        json={"to": "office@uni.de", "opportunity": {"name": "DAAD Scholarship"}},
    )
    assert response.status_code == 503
    body = response.get_json()
    assert body["ok"] is False
    assert body["configured"] is False
    assert "credentials.json" in body["error"]


def test_draft_email_needs_a_recipient(client):
    response = client.post("/draft_email", json={"opportunity": {"name": "X"}})
    assert response.status_code == 400
    assert "recipient" in response.get_json()["error"].lower()


def test_draft_email_composes_from_the_opportunity(client, monkeypatch):
    captured = {}

    def fake_draft(to, subject, body):
        captured.update(to=to, subject=subject, body=body)
        return {"ok": True, "drafted": True, "sent": False, "to": to,
                "subject": subject, "draft_id": "r-1", "message": "Draft saved."}

    monkeypatch.setattr(agent, "create_gmail_draft", fake_draft)
    response = client.post(
        "/draft_email",
        json={"to": "office@uni.de",
              "opportunity": {"name": "DAAD Scholarship", "institution": "DAAD",
                              "url": "https://daad.de/x"}},
    )
    assert response.status_code == 200
    assert response.get_json()["sent"] is False
    assert "DAAD Scholarship" in captured["subject"]
    assert "https://daad.de/x" in captured["body"]


# --- POST /save_deadline ---------------------------------------------------


def test_save_deadline_appends(client, monkeypatch, tmp_path):
    store = tmp_path / "deadlines.json"
    monkeypatch.setattr(agent, "DEADLINES_FILE", store)

    response = client.post(
        "/save_deadline",
        json={"opportunity": {"name": "DAAD Scholarship",
                              "deadline": "15 January 2027",
                              "url": "https://daad.de/x"}},
    )
    assert response.status_code == 200
    assert response.get_json()["saved"] is True
    assert json.loads(store.read_text())[0]["scholarship_name"] == "DAAD Scholarship"


def test_save_deadline_refuses_when_none_is_stated(client):
    response = client.post(
        "/save_deadline",
        json={"opportunity": {"name": "Some Programme", "deadline": "not stated"}},
    )
    assert response.status_code == 400
    assert "does not state a deadline" in response.get_json()["error"]


def test_save_deadline_needs_a_name(client):
    response = client.post("/save_deadline", json={"opportunity": {"deadline": "1 Jan 2027"}})
    assert response.status_code == 400


# --- the shortlist pipeline behind /search ---------------------------------


def _stub_pipeline(monkeypatch, results, extraction):
    """Wire the pipeline to fixed search results and a fixed extraction."""
    monkeypatch.setattr(
        agent, "search_opportunities",
        lambda q, **kw: {"ok": True, "query": q, "results": results, "count": len(results)},
    )
    monkeypatch.setattr(agent, "extract_requirements_from", lambda url, **kw: extraction)
    monkeypatch.setattr(
        agent, "check_eligibility_for",
        lambda reqs, prof: {"ok": True, "verdict": "eligible", "reason": "ok", "fit": "good",
                            "deadline_status": "open", "breakdown": []},
    )
    monkeypatch.setattr(agent, "match_courses_for", lambda s, r: dict(agent.NOT_ASSESSED))


LISTING = {"ok": True, "url": "https://x.com/list", "name": "93 Scholarships in Germany",
           "type": "scholarship", "institution": "not stated",
           "is_single_opportunity": False,
           "requirements": {f: agent.NOT_STATED for f in agent.REQUIREMENT_FIELDS}}

REAL = {"ok": True, "url": "https://daad.de/real", "name": "DAAD Study Scholarship",
        "type": "scholarship", "institution": "DAAD", "is_single_opportunity": True,
        "requirements": {**{f: agent.NOT_STATED for f in agent.REQUIREMENT_FIELDS},
                         "minimum_gpa": "3.0", "required_courses": []}}


def test_directory_pages_are_skipped_not_shown(monkeypatch):
    """Regression: 'Top 93 Scholarships' listing pages were being shown as ELIGIBLE.
    A student cannot apply to a list, and its blanket criteria match almost anyone."""
    _stub_pipeline(monkeypatch, [{"title": "93 Scholarships", "url": "https://x/list",
                                  "snippet": "", "trusted": True}], LISTING)
    outcome = agent.run_shortlist(VALID_PROFILE)

    assert outcome["ok"] is True
    assert outcome["results"] == []
    assert outcome["skipped_listings"] == 1
    assert "directory" in outcome["message"] or "round-up" in outcome["message"]


def test_a_real_opportunity_page_is_kept(monkeypatch):
    _stub_pipeline(monkeypatch, [{"title": "DAAD Study Scholarship",
                                  "url": "https://daad.de/real", "snippet": "",
                                  "trusted": True}], REAL)
    outcome = agent.run_shortlist(VALID_PROFILE)

    assert len(outcome["results"]) == 1
    assert outcome["skipped_listings"] == 0
    item = outcome["results"][0]
    assert item["name"] == "DAAD Study Scholarship"
    assert item["url"] == "https://daad.de/real"


def test_social_media_results_are_dropped_before_extraction():
    """A Facebook post about a scholarship is not the scholarship."""
    cleaned = agent._clean_results([
        {"title": "Study in Germany!", "url": "https://www.facebook.com/x/posts/1",
         "content": "apply now"},
        {"title": "Real one", "url": "https://www2.daad.de/real", "content": "details"},
    ])
    assert [r["url"] for r in cleaned] == ["https://www2.daad.de/real"]


def test_extraction_defaults_to_listing_when_unconfirmed(monkeypatch):
    """If the model does not confirm a single opportunity, assume it is a listing
    rather than assume it is applicable."""
    monkeypatch.setattr(agent, "_ask_json", lambda prompt, fallback: {"opportunity_name": "X"})
    outcome = agent.extract_requirements_from("Some text about scholarships")
    assert outcome["is_single_opportunity"] is False


def test_shortlist_asks_for_missing_profile_fields():
    outcome = agent.run_shortlist({"field_of_study": "Computer Science"})
    assert outcome["ok"] is False
    assert outcome["needs_profile"]


def test_one_broken_page_does_not_sink_the_shortlist(monkeypatch):
    calls = {"n": 0}

    def flaky(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("that page 500'd")
        return REAL

    monkeypatch.setattr(
        agent, "search_opportunities",
        lambda q, **kw: {"ok": True, "query": q, "count": 2, "results": [
            {"title": "broken", "url": "https://a/1", "snippet": "", "trusted": False},
            {"title": "fine", "url": "https://daad.de/real", "snippet": "", "trusted": True},
        ]},
    )
    monkeypatch.setattr(agent, "extract_requirements_from", flaky)
    monkeypatch.setattr(
        agent, "check_eligibility_for",
        lambda reqs, prof: {"ok": True, "verdict": "eligible", "reason": "", "fit": "",
                            "deadline_status": "open", "breakdown": []},
    )
    monkeypatch.setattr(agent, "match_courses_for", lambda s, r: dict(agent.NOT_ASSESSED))

    outcome = agent.run_shortlist(VALID_PROFILE)
    assert len(outcome["results"]) == 1  # the good one still came through


def test_eligible_ranks_above_unclear(monkeypatch):
    verdicts = iter(["unclear", "eligible"])
    monkeypatch.setattr(
        agent, "search_opportunities",
        lambda q, **kw: {"ok": True, "query": q, "count": 2, "results": [
            {"title": "a", "url": "https://a/1", "snippet": "", "trusted": True},
            {"title": "b", "url": "https://b/2", "snippet": "", "trusted": True},
        ]},
    )
    monkeypatch.setattr(agent, "extract_requirements_from", lambda url, **kw: REAL)
    monkeypatch.setattr(
        agent, "check_eligibility_for",
        lambda reqs, prof: {"ok": True, "verdict": next(verdicts), "reason": "", "fit": "",
                            "deadline_status": "open", "breakdown": []},
    )
    monkeypatch.setattr(agent, "match_courses_for", lambda s, r: dict(agent.NOT_ASSESSED))

    outcome = agent.run_shortlist(VALID_PROFILE)
    assert [i["verdict"] for i in outcome["results"]] == ["eligible", "unclear"]


def test_query_targets_the_next_degree_level():
    """A Bachelor's holder wants Master's funding, not more Bachelor's funding."""
    query = agent.build_query({"degree_level": "Bachelor's", "field_of_study": "Physics",
                               "nationality": "Egypt"})
    assert "Master's" in query
    assert "Physics" in query
    assert "Egypt" in query


# --- error handling --------------------------------------------------------


def test_unknown_route_returns_json_not_html(client):
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.get_json()["ok"] is False
