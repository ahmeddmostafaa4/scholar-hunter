"""Phase 3 tests: requirement extraction, eligibility reasoning, course matching.

These use real Claude calls — the reasoning IS the thing under test, so mocking
it would test nothing. Skipped without ANTHROPIC_API_KEY.
"""

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402

HAS_KEY = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
needs_key = pytest.mark.skipif(not HAS_KEY, reason="ANTHROPIC_API_KEY not set")

# --- fixtures: a student and three contrasting opportunity pages ------------

# Deadlines in fixtures are kept comfortably in the future so the suite does not
# start failing once a hard-coded date passes.
NEXT_YEAR = date.today().year + 1

STUDENT = {
    "field_of_study": "Computer Science",
    "degree_level": "Bachelor's",
    "gpa": "3.6 / 4.0",
    "nationality": "Egypt",
    "interests": "Master's funding in Germany, machine learning",
    "language": "IELTS 7.5 overall",
}

FULLY_STATED_PAGE = f"""
Global Excellence Master's Scholarship in Computer Science
Technical University of Munich

Eligibility:
- Applicants must hold a Bachelor's degree in Computer Science or a closely
  related field.
- A minimum GPA of 3.0 on a 4.0 scale is required.
- Open to applicants of all nationalities, including non-EU citizens.
- English proficiency: IELTS 6.5 or TOEFL iBT 90.

Funding: Full tuition waiver plus a monthly stipend of 992 EUR for 24 months.
Application deadline: 15 January {NEXT_YEAR}.
"""

REQUIRES_MASTERS_PAGE = f"""
Doctoral Research Fellowship in Artificial Intelligence
Eligibility: Applicants MUST already hold a completed Master's degree in
Computer Science or Mathematics. Minimum GPA 3.0/4.0. Open to all nationalities.
Funding: 3-year doctoral salary. Deadline: 1 March {NEXT_YEAR}.
"""

# States nothing checkable: no GPA, no nationality, no deadline, no language.
SILENT_PAGE = """
Department of Computer Science — Funding Opportunities

We offer a number of funding opportunities for talented students. Applications
are reviewed by the faculty committee, which meets several times per year.
Contact the departmental office for further information about how to apply.
"""

EXPIRED_PAGE = """
Summer Research Workshop in Machine Learning
Open to Bachelor's students of all nationalities in Computer Science.
No minimum GPA. Funding: travel and accommodation.
Application deadline: 3 February 2019.
"""


# --- (a) clearly eligible ---------------------------------------------------


@needs_key
def test_all_requirements_met_gives_eligible():
    extracted = agent.extract_requirements_from(FULLY_STATED_PAGE)
    assert extracted["ok"]
    reqs = extracted["requirements"]
    assert "3.0" in str(reqs["minimum_gpa"])
    assert str(NEXT_YEAR) in str(reqs["deadline"])

    result = agent.check_eligibility_for(reqs, STUDENT)
    assert result["verdict"] == "eligible", result
    assert result["breakdown"], "a verdict without a breakdown is not auditable"
    assert all(row["status"] != "not_met" for row in result["breakdown"])
    assert result["deadline_status"] == "open", result


# --- (b) clearly ineligible, with the failing requirement named -------------


@needs_key
def test_wrong_degree_level_is_caught_and_named():
    extracted = agent.extract_requirements_from(REQUIRES_MASTERS_PAGE)
    result = agent.check_eligibility_for(extracted["requirements"], STUDENT)

    assert result["verdict"] == "not_eligible", result
    failed = [row for row in result["breakdown"] if row["status"] == "not_met"]
    assert failed, "the failing requirement must be identified, not just the verdict"
    assert any("degree" in json.dumps(row).lower() for row in failed), failed


# --- (c) missing info -> not_stated + unclear, never a guess ----------------


@needs_key
def test_silent_page_yields_not_stated_and_unclear():
    extracted = agent.extract_requirements_from(SILENT_PAGE)
    reqs = extracted["requirements"]

    # The page states no GPA, nationality or deadline — those must come back blank.
    assert reqs["minimum_gpa"] == agent.NOT_STATED, reqs
    assert reqs["deadline"] == agent.NOT_STATED, reqs

    result = agent.check_eligibility_for(reqs, STUDENT)
    assert result["verdict"] == "unclear", result


def test_silence_is_never_treated_as_permission(monkeypatch):
    """Regression: with no criteria stated, 'nothing was unmet' is vacuously true.
    The tool must not turn that into 'eligible'."""
    monkeypatch.setattr(
        agent,
        "_ask_json",
        lambda prompt, fallback: {
            "verdict": "eligible",
            "reason": "no criteria stated, so nothing is unmet",
            "breakdown": [],
            "deadline_status": "not stated",
        },
    )
    result = agent.check_eligibility_for(
        {f: agent.NOT_STATED for f in agent.REQUIREMENT_FIELDS}, STUDENT
    )
    assert result["verdict"] == "unclear", result


@needs_key
def test_expired_deadline_is_rejected():
    """The spec says expired options are discarded — so they must be detected."""
    extracted = agent.extract_requirements_from(EXPIRED_PAGE)
    result = agent.check_eligibility_for(extracted["requirements"], STUDENT)

    assert result["deadline_status"] == "expired", result
    assert result["verdict"] == "not_eligible", result


@needs_key
def test_graduate_level_programme_does_not_exclude_a_bachelors_holder():
    """Regression: 'funding for graduate study' is the level being funded, not a
    degree the applicant must already hold. Conflating them wrongly excluded
    exactly the students this tool exists to help."""
    page = """
    Graduate Funding Programme in Computer Science
    Funding for graduate students pursuing a Master's degree.
    Open to applicants of all nationalities. Minimum GPA 3.0/4.0.
    Deadline: 1 December %d.
    """ % NEXT_YEAR
    extracted = agent.extract_requirements_from(page)
    result = agent.check_eligibility_for(extracted["requirements"], STUDENT)

    degree_rows = [r for r in result["breakdown"] if "degree" in str(r.get("requirement", "")).lower()]
    assert all(r["status"] != "not_met" for r in degree_rows), degree_rows
    assert result["verdict"] != "not_eligible", result


def test_deadline_and_funding_are_not_scored_as_student_attributes(monkeypatch):
    """Regression: a deadline is not something a student 'has'. Scoring it as a
    criterion produced meaningless not_stated rows that dragged every verdict
    down to unclear."""
    captured = {}

    def fake(prompt, fallback):
        captured["prompt"] = prompt
        return {"verdict": "eligible", "breakdown": [], "deadline_status": "open"}

    monkeypatch.setattr(agent, "_ask_json", fake)
    agent.check_eligibility_for(
        {f: "x" for f in agent.REQUIREMENT_FIELDS}, {"gpa": "3.6"}
    )

    criteria_block = captured["prompt"].split("ELIGIBILITY CRITERIA")[1].split("APPLICATION DEADLINE")[0]
    assert "funding_scope" not in criteria_block
    assert '"deadline"' not in criteria_block


def test_expired_deadline_overrides_an_eligible_claim(monkeypatch):
    monkeypatch.setattr(
        agent,
        "_ask_json",
        lambda prompt, fallback: {
            "verdict": "eligible",
            "breakdown": [{"requirement": "GPA", "status": "met"}],
            "deadline_status": "expired",
        },
    )
    result = agent.check_eligibility_for({"minimum_gpa": "3.0"}, STUDENT)
    assert result["verdict"] == "not_eligible"


def test_eligible_verdict_cannot_survive_a_failed_requirement(monkeypatch):
    """Defensive floor: an 'eligible' claim alongside a not_met row is downgraded."""
    monkeypatch.setattr(
        agent,
        "_ask_json",
        lambda prompt, fallback: {
            "verdict": "eligible",
            "reason": "looks good",
            "breakdown": [{"requirement": "GPA", "status": "not_met", "note": "3.6 < 3.8"}],
        },
    )
    result = agent.check_eligibility_for({"minimum_gpa": "3.8"}, STUDENT)
    assert result["verdict"] == "not_eligible"


# --- (d) different names, same description, must match ---------------------


@needs_key
def test_differently_named_equivalent_courses_match():
    student = [
        "Foundations of Machine Intelligence: search, knowledge representation, "
        "reasoning under uncertainty, and an introduction to machine learning",
        "Discrete Structures: logic, sets, relations, graphs, combinatorics",
    ]
    required = [
        "Introduction to Artificial Intelligence — covers search algorithms, "
        "knowledge representation, reasoning, and basic machine learning"
    ]
    result = agent.match_courses_for(student, required)

    assert result["assessed"] is True
    match = result["matches"][0]
    assert match["status"] in {"matched", "partially_matched"}, match
    assert "Machine Intelligence" in (match["student_course"] or ""), match


# --- (e) a genuinely absent course must be reported missing ----------------


@needs_key
def test_genuinely_missing_course_is_reported_missing():
    student = [
        "Introduction to Painting: oil technique, colour theory, still life composition"
    ]
    required = [
        "Advanced Organic Chemistry — reaction mechanisms, stereochemistry, synthesis"
    ]
    result = agent.match_courses_for(student, required)

    assert result["matches"][0]["status"] == "missing", result
    assert result["matched"] == 0
    assert "0 of 1" in result["summary"]


@needs_key
def test_names_without_descriptions_cap_confidence():
    """Bare names are a weaker signal, and the tool must say so."""
    result = agent.match_courses_for(["Linear Algebra"], ["Linear Algebra"])
    assert result["confidence_capped"] is True
    assert all(m["confidence"] != "high" for m in result["matches"]), result


# --- (f) no courses -> cleanly "not assessed", no LLM call, no error -------


def test_no_student_courses_is_not_assessed(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("must not call the model when there is nothing to match")

    monkeypatch.setattr(agent, "_ask_json", explode)

    result = agent.match_courses_for("", ["Linear Algebra"])
    assert result["ok"] is True
    assert result["assessed"] is False
    assert result["summary"] == "not assessed"
    assert result["matches"] == []


def test_no_required_courses_is_not_assessed(monkeypatch):
    monkeypatch.setattr(
        agent, "_ask_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call"))
    )
    result = agent.match_courses_for(["Linear Algebra"], [])
    assert result["assessed"] is False
    assert result["summary"] == "not assessed"


def test_course_lists_accept_json_lines_and_commas():
    assert agent._as_list('["A", "B"]') == ["A", "B"]
    assert agent._as_list("A\nB") == ["A", "B"]
    assert agent._as_list("A, B") == ["A", "B"]
    assert agent._as_list("") == []


def test_extraction_without_input_is_rejected():
    outcome = agent.extract_requirements_from("")
    assert outcome["ok"] is False


def test_unfetchable_url_reports_all_fields_unknown():
    """A dead link must not become invented requirements."""
    outcome = agent.extract_requirements_from("http://localhost:9/definitely-not-there")
    assert outcome["ok"] is True
    assert all(v == agent.NOT_STATED for v in outcome["requirements"].values())
