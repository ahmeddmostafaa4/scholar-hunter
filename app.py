"""Scholar Hunter — local Flask server.

Serves the single-page frontend and exposes the agent over JSON. Run it from the
VS Code integrated terminal:

    python app.py

then open http://localhost:5000 in a browser. This is a local dev loop; there is
no deployment step.

All agent logic lives in agent.py and is imported, never duplicated here.
"""

from __future__ import annotations

import csv
import io
import os

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

import agent
import application_pack as pack_builder
import autofill

app = Flask(__name__)
CORS(app)  # so the page can call the API from localhost without friction

# Reject oversized uploads before reading them into memory. An application pack
# is several scanned documents at once, so this has to be roomier than the
# single transcript the course upload takes.
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024  # 40 MB

ALLOWED_UPLOADS = {".txt", ".csv", ".pdf"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def fail(message: str, status: int = 400, **extra):
    """A clean JSON error. The frontend shows `error`; it never sees a stack trace."""
    return jsonify({"ok": False, "error": message, **extra}), status


def parse_courses_file(filename: str, data: bytes) -> dict:
    """Extract course text from an uploaded .txt, .csv or .pdf.

    Deliberately simple: pull the text out and let the LLM identify the course
    names and descriptions, rather than trying to guess a transcript's layout.
    """
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in ALLOWED_UPLOADS:
        return {
            "ok": False,
            "error": f"Unsupported file type '{suffix or filename}'. "
            "Upload a .txt, .csv or .pdf.",
        }
    if not data:
        return {"ok": False, "error": "The uploaded file was empty."}

    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            if not text.strip():
                return {
                    "ok": False,
                    "error": "No text could be read from that PDF — it may be a "
                    "scanned image. Paste your courses instead.",
                }
        elif suffix == ".csv":
            decoded = data.decode("utf-8", errors="replace")
            rows = list(csv.reader(io.StringIO(decoded)))
            # Join each row so "Course, description" survives as one line.
            text = "\n".join(" ".join(cell.strip() for cell in row if cell.strip())
                             for row in rows)
        else:
            text = data.decode("utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "error": f"Could not read that file: {exc}"}

    courses = [line.strip() for line in text.splitlines() if line.strip()]
    return {"ok": True, "courses": courses, "source": filename}


def read_profile(req) -> tuple[dict, str]:
    """Build the student profile from JSON or multipart form data.

    Returns (profile, error). The upload path is multipart; the plain path is JSON.
    """
    if req.content_type and req.content_type.startswith("multipart/form-data"):
        profile = {
            key: (req.form.get(key) or "").strip()
            for key in (
                "field_of_study",
                "degree_level",
                "gpa",
                "nationality",
                "interests",
                # Optional, and only used to fill a portal's form fields.
                "full_name",
                "email",
                "university",
            )
        }
        courses = [
            line.strip()
            for line in (req.form.get("courses") or "").splitlines()
            if line.strip()
        ]

        upload = req.files.get("courses_file")
        if upload and upload.filename:
            parsed = parse_courses_file(upload.filename, upload.read())
            if not parsed["ok"]:
                return {}, parsed["error"]
            courses.extend(parsed["courses"])

        profile["courses"] = courses
        return profile, ""

    body = req.get_json(silent=True)
    if body is None:
        return {}, "Expected a JSON body (or a multipart form when uploading a file)."
    if not isinstance(body, dict):
        return {}, "Expected a JSON object describing the student profile."

    profile = {k: v for k, v in body.items()}
    raw_courses = profile.get("courses", [])
    if isinstance(raw_courses, str):
        profile["courses"] = [c.strip() for c in raw_courses.splitlines() if c.strip()]
    return profile, ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def index():
    """Serve the single-page UI."""
    return render_template("index.html")


@app.get("/health")
def health():
    """What is configured — the page uses this to explain a disabled email button."""
    return jsonify({"ok": True, **agent.status()})


@app.post("/search")
def search():
    """Profile in, ranked uniform shortlist out (steps 1-4; no side effects)."""
    profile, error = read_profile(request)
    if error:
        return fail(error)
    if not profile:
        return fail("No student profile supplied.")

    try:
        outcome = agent.run_shortlist(profile)
    except agent.MissingKeyError as exc:
        return fail(str(exc), 503)
    except Exception as exc:
        app.logger.exception("shortlist failed")
        return fail(f"The search could not be completed: {exc}", 500)

    if not outcome["ok"]:
        # A profile gap is the user's to fix (400); anything else is ours (502).
        status = 400 if outcome.get("needs_profile") else 502
        return fail(outcome["error"], status, needs_profile=outcome.get("needs_profile", []))

    return jsonify(outcome)


@app.post("/document_help")
def document_help():
    """Explain how to prepare a chosen opportunity's required documents.

    Called when a card is expanded rather than during /search, so a shortlist the
    student only skims costs nothing extra.
    """
    body = request.get_json(silent=True) or {}
    opportunity = body.get("opportunity") or {}

    documents = body.get("documents") or opportunity.get("documents") or []
    if not documents:
        return jsonify(
            {
                "ok": True,
                "assessed": False,
                "guidance": [],
                "note": "This page does not list the documents required.",
            }
        )

    try:
        outcome = agent.document_guidance(
            documents,
            opportunity_name=opportunity.get("name", ""),
            institution=opportunity.get("institution", ""),
            student_profile=body.get("profile") or {},
        )
    except agent.MissingKeyError as exc:
        return fail(str(exc), 503)
    except Exception as exc:
        app.logger.exception("document help failed")
        return fail(f"Could not prepare document guidance: {exc}", 500)

    if not outcome.get("ok"):
        return fail(outcome.get("note") or "Could not prepare document guidance.", 502)
    return jsonify(outcome)


@app.post("/application_pack")
def application_pack():
    """Merge the student's own documents into one ordered PDF for a portal.

    Deliberately does not generate any document and does not submit anything:
    the pack comes back to the student, who reviews it and submits it themselves.
    """
    import json as _json

    if not (request.content_type or "").startswith("multipart/form-data"):
        return fail("Attach your documents as a multipart form.")

    try:
        documents = _json.loads(request.form.get("documents") or "[]")
    except ValueError:
        return fail("Could not read the document list.")
    if not isinstance(documents, list) or not documents:
        return fail("No required documents were supplied to order the pack by.")

    # One file input per required document, in the programme's own order, so the
    # pack is assembled correctly without anyone having to guess which is which.
    entries, attached_labels = [], []
    for index, label in enumerate(documents):
        upload = request.files.get(f"doc_{index}")
        if not upload or not upload.filename:
            continue
        entries.append(
            {"label": str(label), "filename": upload.filename, "data": upload.read()}
        )
        attached_labels.append(str(label))

    if not entries:
        return fail("Attach at least one document before building the pack.")

    try:
        result = pack_builder.build_pack(
            entries,
            opportunity=(request.form.get("opportunity") or "").strip(),
            institution=(request.form.get("institution") or "").strip(),
        )
    except Exception as exc:
        app.logger.exception("pack build failed")
        return fail(f"Could not build the pack: {exc}", 500)

    if not result["ok"]:
        return fail(result["error"], 400, skipped=result.get("skipped", []))

    ready = pack_builder.readiness(documents, attached_labels)
    filename = "application-pack.pdf"

    response = app.response_class(result["pdf"], mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    # Summary rides in headers so the browser can report it beside the download.
    response.headers["X-Pack-Pages"] = str(result["pages"])
    response.headers["X-Pack-Included"] = str(len(result["included"]))
    response.headers["X-Pack-Skipped"] = _json.dumps(result["skipped"])[:1800]
    response.headers["X-Pack-Missing"] = _json.dumps(ready["missing"])[:1800]
    response.headers["Access-Control-Expose-Headers"] = (
        "X-Pack-Pages, X-Pack-Included, X-Pack-Skipped, X-Pack-Missing"
    )
    return response


@app.post("/autofill/open")
def autofill_open():
    """Open the application portal in a visible browser the student controls."""
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or (body.get("opportunity") or {}).get("application_url") or "").strip()
    if not url or url.lower() == agent.NOT_STATED:
        return fail("This opportunity has no application link to open.")

    outcome = autofill.open_portal(url)
    if not outcome["ok"]:
        status = 503 if outcome.get("available") is False else 502
        return fail(outcome["error"], status, configured=outcome.get("available", True))
    return jsonify(outcome)


@app.post("/autofill/fill")
def autofill_fill():
    """Fill the open form from the profile. Never submits — see autofill.py."""
    body = request.get_json(silent=True) or {}
    outcome = autofill.fill_form(body.get("profile") or {})
    if not outcome["ok"]:
        status = 503 if outcome.get("available") is False else 502
        return fail(outcome["error"], status, configured=outcome.get("available", True))
    return jsonify(outcome)


@app.post("/autofill/close")
def autofill_close():
    return jsonify(autofill.close_portal())


@app.post("/draft_email")
def draft_email():
    """Create a Gmail DRAFT for a chosen opportunity. Never sends."""
    body = request.get_json(silent=True) or {}
    to = (body.get("to") or "").strip()
    subject = (body.get("subject") or "").strip()
    message = (body.get("body") or "").strip()

    # The frontend may send just the chosen opportunity and let us compose.
    opportunity = body.get("opportunity") or {}
    if not subject and opportunity.get("name"):
        subject = f"Inquiry about {opportunity['name']}"
    if not message and opportunity.get("name"):
        message = (
            f"Dear Admissions Office,\n\n"
            f"I am writing to enquire about {opportunity['name']}"
            f"{' at ' + opportunity['institution'] if opportunity.get('institution') not in (None, '', 'not stated') else ''}"
            f", which I found at {opportunity.get('url', '')}.\n\n"
            "I would be grateful if you could confirm the eligibility requirements "
            "and the application process for the coming intake.\n\n"
            "Thank you for your time.\n\nKind regards,\n"
        )

    if not to:
        return fail("No recipient address supplied.")

    outcome = agent.create_gmail_draft(to, subject, message)
    if not outcome["ok"]:
        # 503 when the integration is simply not configured — that is not a bug.
        status = 503 if outcome.get("available") is False else 502
        return fail(outcome["error"], status, configured=outcome.get("available", True))

    return jsonify(outcome)


@app.post("/save_deadline")
def save_deadline():
    """Append a chosen opportunity's deadline to deadlines.json."""
    body = request.get_json(silent=True) or {}
    opportunity = body.get("opportunity") or {}

    name = (body.get("scholarship_name") or opportunity.get("name") or "").strip()
    when = (body.get("deadline_date") or opportunity.get("deadline") or "").strip()
    url = (body.get("url") or opportunity.get("url") or "").strip()

    if not name:
        return fail("No scholarship name supplied.")
    if not when or when.lower() == agent.NOT_STATED:
        return fail(
            "This opportunity does not state a deadline, so there is nothing to save."
        )

    outcome = agent.save_deadline_entry(name, when, url)
    if not outcome["ok"]:
        return fail(outcome["error"], 500)
    return jsonify(outcome)


@app.errorhandler(404)
def not_found(_error):
    return fail("Not found.", 404)


@app.errorhandler(413)
def too_large(_error):
    return fail("Those files are too large. The limit is 40 MB in total.", 413)


@app.errorhandler(500)
def server_error(_error):
    return fail("Something went wrong on the server.", 500)


DEFAULT_PORT = 5000
FALLBACK_PORT = 5050


def choose_port() -> tuple[int, str]:
    """Pick a port that the browser will actually reach.

    On macOS, AirPlay Receiver (ControlCenter) listens on *:5000. Flask still
    binds 127.0.0.1:5000 happily, so the server looks fine in the terminal — but
    http://localhost:5000 resolves to AirPlay first and answers 403 Forbidden.
    Rather than let that look like a broken app, detect the clash and move.

    Set PORT to override.
    """
    import socket

    override = (os.environ.get("PORT") or "").strip()
    if override.isdigit():
        return int(override), ""

    with socket.socket() as probe:
        probe.settimeout(0.4)
        occupied = probe.connect_ex(("localhost", DEFAULT_PORT)) == 0

    if occupied:
        return FALLBACK_PORT, (
            f"  Note: something else already answers on port {DEFAULT_PORT}\n"
            "        (on macOS this is usually AirPlay Receiver — System Settings\n"
            "        > General > AirDrop & Handoff > AirPlay Receiver).\n"
            f"        Using port {FALLBACK_PORT} instead."
        )
    return DEFAULT_PORT, ""


if __name__ == "__main__":
    port, note = choose_port()

    print("\n" + "=" * 62)
    print("  Scholar Hunter — find scholarships you can actually apply to")
    print("=" * 62)
    agent.print_banner()
    print("=" * 62)
    if note:
        print(note)
    url = f"http://localhost:{port}"
    print(f"  Open {url} in your browser")
    print("  (local dev server — nothing is deployed anywhere)")
    print("=" * 62 + "\n")

    # Open the right URL automatically. Typing localhost:5000 out of habit lands
    # on AirPlay's blank 403, which reads as "the app is broken" — so don't make
    # anyone type it. Set NO_BROWSER=1 to skip.
    if not (os.environ.get("NO_BROWSER") or "").strip():
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    app.run(host="127.0.0.1", port=port, debug=True, use_reloader=False)
