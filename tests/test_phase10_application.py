"""Phase 10 tests: the application pack and the portal link.

The feature is deliberately narrow, and the tests pin that narrowness down:
the tool merges the student's OWN files and never writes or submits anything.
"""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402
import app as flask_app  # noqa: E402
import application_pack as pack  # noqa: E402


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as test_client:
        yield test_client


def a_pdf(pages: int = 1) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def an_image(fmt: str = "JPEG") -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (900, 1200), "white").save(buffer, format=fmt)
    return buffer.getvalue()


# --- the boundary: what this feature must never do -------------------------


def test_the_pack_module_never_generates_a_document():
    """A motivation letter must be the applicant's own words, and a transcript or
    reference is issued by a third party. Generating either is forgery."""
    source = Path(pack.__file__).read_text().lower()
    assert "does not write any document" in source
    # No text-generation model anywhere near this module.
    assert "chatopenai" not in source
    assert "chatanthropic" not in source
    assert "build_llm" not in source


def test_nothing_is_submitted_anywhere():
    """The declaration that an application is truthful belongs to the student."""
    source = Path(pack.__file__).read_text()
    assert "does not submit anything" in source
    for outbound in ("requests.post", "urlopen", "httpx"):
        assert outbound not in source


def test_the_endpoint_returns_the_pack_rather_than_sending_it(client):
    """The pack must come back to the student as a download."""
    data = {"documents": json.dumps(["CV"]), "doc_0": (io.BytesIO(a_pdf()), "cv.pdf")}
    response = client.post("/application_pack", data=data,
                           content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert "attachment" in response.headers["Content-Disposition"]


# --- merging ---------------------------------------------------------------


def test_files_merge_in_the_order_the_programme_lists_them():
    from pypdf import PdfReader

    result = pack.build_pack(
        [
            {"label": "CV", "filename": "cv.pdf", "data": a_pdf(2)},
            {"label": "Transcript", "filename": "t.pdf", "data": a_pdf(3)},
        ],
        opportunity="DAAD Study Scholarship", institution="DAAD",
    )
    assert result["ok"]
    assert [e["label"] for e in result["included"]] == ["CV", "Transcript"]
    assert [e["pages"] for e in result["included"]] == [2, 3]
    # 2 + 3 plus a cover sheet.
    assert result["pages"] == 6
    assert len(PdfReader(io.BytesIO(result["pdf"])).pages) == 6


def test_a_scan_is_converted_rather_than_rejected():
    """Transcripts arrive as phone photos more often than as PDFs."""
    result = pack.build_pack([
        {"label": "Transcript", "filename": "scan.jpg", "data": an_image()}])
    assert result["ok"]
    assert result["included"][0]["pages"] == 1


def test_png_with_transparency_does_not_blacken():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (600, 800), (255, 255, 255, 0)).save(buffer, format="PNG")
    result = pack.build_pack([
        {"label": "Certificate", "filename": "c.png", "data": buffer.getvalue()}])
    assert result["ok"], result


def test_unmergeable_files_are_reported_never_dropped_silently():
    """Discovering a missing page after submitting is the failure this prevents."""
    result = pack.build_pack([
        {"label": "CV", "filename": "cv.pdf", "data": a_pdf()},
        {"label": "Reference", "filename": "ref.docx", "data": b"docx bytes"},
        {"label": "Empty", "filename": "e.pdf", "data": b""},
        {"label": "Broken", "filename": "b.pdf", "data": b"not a pdf at all"},
    ])
    assert result["ok"]
    assert len(result["included"]) == 1

    reasons = {s["filename"]: s["why"] for s in result["skipped"]}
    assert "export it as PDF first" in reasons["ref.docx"]
    assert "empty" in reasons["e.pdf"]
    assert "could not be read" in reasons["b.pdf"]


def test_an_oversized_file_is_refused_with_a_reason():
    result = pack.build_pack([
        {"label": "Huge", "filename": "h.pdf", "data": b"x" * (pack.MAX_FILE_BYTES + 1)}])
    assert result["ok"] is False
    assert "MB" in result["skipped"][0]["why"]


def test_no_usable_file_fails_clearly():
    result = pack.build_pack([{"label": "X", "filename": "x.docx", "data": b"abc"}])
    assert result["ok"] is False
    assert result["skipped"]


def test_readiness_counts_what_is_still_missing():
    r = pack.readiness(["CV", "Transcript", "Reference"], ["CV"])
    assert r == {"required": 3, "attached": 1, "missing": ["Transcript", "Reference"],
                 "complete": False}
    assert pack.readiness(["CV"], ["CV"])["complete"] is True


# --- the endpoint ----------------------------------------------------------


def test_missing_documents_are_reported_in_the_headers(client):
    data = {
        "documents": json.dumps(["CV", "Transcript", "Reference"]),
        "doc_0": (io.BytesIO(a_pdf()), "cv.pdf"),
    }
    response = client.post("/application_pack", data=data,
                           content_type="multipart/form-data")
    assert response.status_code == 200
    missing = json.loads(response.headers["X-Pack-Missing"])
    assert missing == ["Transcript", "Reference"]
    assert response.headers["X-Pack-Included"] == "1"


def test_endpoint_needs_at_least_one_attachment(client):
    response = client.post("/application_pack",
                           data={"documents": json.dumps(["CV"])},
                           content_type="multipart/form-data")
    assert response.status_code == 400
    assert "at least one" in response.get_json()["error"]


def test_endpoint_rejects_a_json_body(client):
    response = client.post("/application_pack", json={"documents": ["CV"]})
    assert response.status_code == 400
    assert "multipart" in response.get_json()["error"]


def test_endpoint_needs_the_document_list(client):
    response = client.post("/application_pack", data={"documents": "[]"},
                           content_type="multipart/form-data")
    assert response.status_code == 400


# --- the application URL ---------------------------------------------------


def test_apply_links_are_scraped_from_the_page():
    from bs4 import BeautifulSoup

    html = """<html><body>
      <a href="/apply/online">Apply now</a>
      <a href="https://portal.daad.de/application">Start your application</a>
      <a href="/about">About us</a>
      <a href="mailto:x@y.de">Email us</a>
      <a href="#top">Back to top</a>
    </body></html>"""
    links = agent._apply_links(BeautifulSoup(html, "html.parser"),
                              "https://www.daad.de/en/scholarship")

    urls = [l["url"] for l in links]
    assert "https://www.daad.de/apply/online" in urls  # relative resolved
    assert "https://portal.daad.de/application" in urls
    assert not any("about" in u for u in urls)
    assert not any(u.startswith("mailto") for u in urls)
    assert not any("#top" in u for u in urls)


def test_an_invented_application_url_is_refused():
    """Sending a student to a hallucinated portal is worse than sending them
    nowhere, so the model's answer is checked against the page's real anchors."""
    real = [{"label": "Apply", "url": "https://portal.daad.de/apply"}]
    assert agent._verified_apply_url("https://portal.daad.de/apply", real) == \
        "https://portal.daad.de/apply"
    assert agent._verified_apply_url("https://totally-made-up.example/apply", real) == \
        agent.NOT_STATED
    assert agent._verified_apply_url("", real) == agent.NOT_STATED
    assert agent._verified_apply_url(agent.NOT_STATED, real) == agent.NOT_STATED


def test_application_url_reaches_the_card(monkeypatch):
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
            "application_url": "https://portal.daad.de/apply",
            "application_method": "online portal",
            "requirements": {**{f: agent.NOT_STATED for f in agent.REQUIREMENT_FIELDS},
                             "required_courses": [], "required_documents": ["CV"]},
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

    item = agent.run_shortlist({"field_of_study": "CS", "degree_level": "Bachelor's",
                                "nationality": "Egypt"})["results"][0]
    assert item["application_url"] == "https://portal.daad.de/apply"
    assert item["application_method"] == "online portal"
