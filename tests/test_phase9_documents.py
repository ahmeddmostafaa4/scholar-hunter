"""Phase 9 tests: required documents and how to prepare them.

The extraction and guidance are model work, so the live tests are marked
`live_llm` and skip when the API is unreachable. Everything structural is
tested offline.
"""

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


PAGE_WITH_DOCUMENTS = """
Global Excellence Master's Scholarship in Computer Science

Applicants must submit the following with their application:
 - A motivation letter, maximum 2 pages
 - A curriculum vitae in tabular form
 - Two academic letters of recommendation
 - A certified transcript of records
 - Proof of English proficiency (IELTS 6.5 or TOEFL iBT 90)

Deadline: 15 January 2099.
"""

PROFILE = {
    "field_of_study": "Computer Science",
    "degree_level": "Bachelor's",
    "gpa": "3.6/4.0",
    "nationality": "Egypt",
}


# --- extraction ------------------------------------------------------------


def test_required_documents_is_an_extracted_field():
    assert "required_documents" in agent.REQUIREMENT_FIELDS
    # It is a document checklist, not a criterion the student is judged against.
    assert "required_documents" not in agent.ELIGIBILITY_FIELDS


@pytest.mark.live_llm
def test_documents_are_pulled_from_the_page():
    extracted = agent.extract_requirements_from(PAGE_WITH_DOCUMENTS)
    docs = extracted["requirements"]["required_documents"]

    assert isinstance(docs, list) and docs, extracted["requirements"]
    joined = " ".join(docs).lower()
    assert "motivation" in joined
    assert "recommendation" in joined or "reference" in joined
    # The stated limit is part of what the applicant needs, so keep it.
    assert "2 page" in joined or "two page" in joined


def test_a_stringified_document_list_is_normalised(monkeypatch):
    """The model sometimes returns a bare string where a list was asked for."""
    monkeypatch.setattr(
        agent, "_ask_json",
        lambda prompt, fallback: {"required_documents": "Motivation letter",
                                  "required_courses": agent.NOT_STATED},
    )
    reqs = agent.extract_requirements_from("some text")["requirements"]
    assert reqs["required_documents"] == ["Motivation letter"]
    assert reqs["required_courses"] == []


def test_documents_reach_the_card(monkeypatch):
    monkeypatch.setattr(
        agent, "search_opportunities",
        lambda q, **kw: {"ok": True, "query": q, "count": 1, "results": [
            {"title": "x", "url": "https://daad.de/a", "snippet": "", "trusted": True}]},
    )
    monkeypatch.setattr(
        agent, "extract_requirements_from",
        lambda url, **kw: {
            "ok": True, "url": url, "name": "N", "type": "scholarship",
            "institution": "DAAD", "is_single_opportunity": True,
            "requirements": {**{f: agent.NOT_STATED for f in agent.REQUIREMENT_FIELDS},
                             "required_courses": [],
                             "required_documents": ["Motivation letter", "CV"]},
        },
    )
    monkeypatch.setattr(
        agent, "check_eligibility_for",
        lambda reqs, prof: {"ok": True, "verdict": "eligible", "reason": "", "fit": "",
                            "deadline_status": "open",
                            "breakdown": [{"requirement": "GPA", "required": "3.0",
                                           "student": "3.6", "status": "met", "note": ""}]},
    )
    monkeypatch.setattr(agent, "match_courses_for", lambda s, r: dict(agent.NOT_ASSESSED))

    item = agent.run_shortlist(PROFILE)["results"][0]
    assert item["documents"] == ["Motivation letter", "CV"]


# --- guidance --------------------------------------------------------------


def test_no_documents_means_no_model_call(monkeypatch):
    """A page listing no documents must not cost an API call."""
    monkeypatch.setattr(
        agent, "_ask_json",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call the model")),
    )
    outcome = agent.document_guidance([], "Some Scholarship")
    assert outcome["ok"] is True
    assert outcome["assessed"] is False
    assert outcome["guidance"] == []
    assert "does not list" in outcome["note"]


def test_guidance_is_shaped_and_capped(monkeypatch):
    monkeypatch.setattr(
        agent, "_ask_json",
        lambda prompt, fallback: {"guidance": [
            {"document": "Motivation letter", "summary": "Convince them.",
             "steps": [f"step {i}" for i in range(9)], "watch_out": "Being generic."},
            "not a dict",  # malformed entries are dropped, not crashed on
        ]},
    )
    outcome = agent.document_guidance(["Motivation letter"], "X", "Y", PROFILE)

    assert len(outcome["guidance"]) == 1
    row = outcome["guidance"][0]
    assert row["document"] == "Motivation letter"
    assert len(row["steps"]) <= 5, "steps must be capped so a card stays readable"
    assert row["watch_out"]


def test_guidance_prompt_carries_the_student_and_the_opportunity(monkeypatch):
    """Generic advice helps nobody — the prompt must include who and what."""
    captured = {}
    monkeypatch.setattr(
        agent, "_ask_json",
        lambda prompt, fallback: captured.setdefault("p", prompt) and {"guidance": []}
        or {"guidance": []},
    )
    agent.document_guidance(["CV"], "DAAD Study Scholarship", "DAAD", PROFILE)

    prompt = captured["p"]
    assert "DAAD Study Scholarship" in prompt
    assert "Computer Science" in prompt
    assert "3.6/4.0" in prompt
    assert "Do NOT invent requirements" in prompt


@pytest.mark.live_llm
def test_live_guidance_is_specific_to_this_student():
    outcome = agent.document_guidance(
        ["Motivation letter (max 2 pages)", "Certified transcript of records"],
        opportunity_name="DAAD Study Scholarship",
        institution="DAAD",
        student_profile={**PROFILE, "courses": ["Introduction to Artificial Intelligence"]},
    )
    assert outcome["assessed"], outcome
    assert len(outcome["guidance"]) == 2

    text = json.dumps(outcome).lower()
    # It should reference the student's own situation, not generic boilerplate.
    assert any(word in text for word in ("computer science", "3.6", "egypt", "german"))
    # And carry the stated limit through rather than dropping it.
    assert "2 page" in text or "two page" in text


def test_a_model_failure_is_reported_not_silently_empty(monkeypatch):
    monkeypatch.setattr(
        agent, "_ask_json",
        lambda prompt, fallback: {**fallback, "model_error": True,
                                  "error": "credit balance too low"},
    )
    outcome = agent.document_guidance(["CV"], "X")
    assert outcome["ok"] is False
    assert "credit balance" in outcome["note"]


def test_explain_documents_is_registered_as_a_tool():
    assert "explain_documents" in agent.describe_tools()


# --- the endpoint ----------------------------------------------------------


def test_endpoint_returns_guidance(client, monkeypatch):
    monkeypatch.setattr(
        agent, "document_guidance",
        lambda docs, **kw: {"ok": True, "assessed": True, "guidance": [
            {"document": "CV", "summary": "s", "steps": ["a"], "watch_out": ""}]},
    )
    response = client.post("/document_help", json={
        "opportunity": {"name": "X", "documents": ["CV"]}, "profile": PROFILE})

    assert response.status_code == 200
    assert response.get_json()["guidance"][0]["document"] == "CV"


def test_endpoint_with_no_documents_does_not_call_the_model(client, monkeypatch):
    monkeypatch.setattr(
        agent, "document_guidance",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    response = client.post("/document_help", json={"opportunity": {"name": "X"}})

    assert response.status_code == 200
    body = response.get_json()
    assert body["assessed"] is False
    assert body["guidance"] == []


def test_endpoint_reports_a_missing_key_cleanly(client, monkeypatch):
    def no_key(*a, **k):
        raise agent.MissingKeyError("No model key is set. Add LITELLM_API_KEY to .env.")

    monkeypatch.setattr(agent, "document_guidance", no_key)
    response = client.post("/document_help", json={
        "opportunity": {"name": "X", "documents": ["CV"]}})

    assert response.status_code == 503
    assert "LITELLM_API_KEY" in response.get_json()["error"]


def test_endpoint_failure_is_json_not_a_stack_trace(client, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "document_guidance", explode)
    response = client.post("/document_help", json={
        "opportunity": {"name": "X", "documents": ["CV"]}})

    assert response.status_code == 500
    assert "Traceback" not in json.dumps(response.get_json())
