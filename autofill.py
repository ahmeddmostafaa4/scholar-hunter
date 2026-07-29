"""Fill an application form in a real browser — and stop at the submit button.

The student clicks "Open portal", a visible browser window opens, they log in if
the portal needs an account, and then "Fill form" types what it can from their
profile into the fields it recognises. Every filled field is outlined and
reported back, so they can check the lot before submitting.

THE ONE RULE: this never submits.

That is enforced here, not merely intended:

  * `_SUBMIT_WORDS` names every control that could send the form, and
    `_is_submit_control` is checked before touching anything.
  * Only text-like `<input>`, `<textarea>` and `<select>` are ever written to.
    Buttons are never clicked, Enter is never pressed, and checkboxes and radios
    are never touched — on an application form those are declarations, and the
    applicant is the only one who may tick them.
  * `submit()` does not exist on this module. There is no code path to it.

The reason is not squeamishness. An application is one-shot and carries a
declaration that the information is true and the work is the applicant's own.
That declaration is the student's to make, and a wrong field discovered after
submitting cannot be undone.

The browser runs headed on purpose. The student watches it happen.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

# Controls that could send the form. Never clicked, never focused-and-entered.
_SUBMIT_WORDS = (
    "submit", "send", "apply now", "finish", "confirm", "complete application",
    "absenden", "bewerbung abschicken", "einreichen", "continue and submit",
    "sign and submit", "final submit",
    # Ticking a declaration is a legal statement by the applicant that the
    # information is true and the work is their own. Only they can make it.
    "declaration", "i declare", "declare that", "i agree", "i confirm",
    "i certify", "terms and conditions", "consent", "ich erkläre",
)

# Profile keys -> the words a form uses for them. Matched against a field's
# label, name, id, placeholder and aria-label.
_FIELD_WORDS: dict[str, tuple[str, ...]] = {
    "first_name": ("first name", "given name", "forename", "vorname", "firstname"),
    "last_name": ("last name", "family name", "surname", "nachname", "lastname"),
    "full_name": ("full name", "your name", "applicant name", "name of applicant"),
    "email": ("email", "e-mail", "mail address", "emailaddress"),
    "nationality": ("nationality", "citizenship", "country of origin",
                    "staatsangehörigkeit", "home country"),
    "field_of_study": ("field of study", "subject", "discipline", "major",
                       "course of study", "study field", "fachrichtung"),
    "degree_level": ("degree", "qualification", "level of study", "current degree",
                     "highest degree", "abschluss"),
    "gpa": ("gpa", "grade point", "final grade", "average grade", "note", "grades"),
    "university": ("university", "institution", "home university", "hochschule",
                   "current institution"),
}

# Fields we must never touch even if they look fillable.
_NEVER_FILL = (
    "password", "passwort", "captcha", "card number", "iban", "credit",
    "security code", "cvv", "signature",
)


def _text_of(field: dict) -> str:
    """Everything a form tells us about a field, lowercased into one haystack."""
    return " ".join(
        str(field.get(k) or "") for k in ("label", "name", "id", "placeholder", "aria")
    ).lower()


def _is_submit_control(text: str, element_type: str = "") -> bool:
    """True for anything that could send the form. Checked before every action."""
    haystack = f"{text} {element_type}".lower()
    if element_type.lower() in {"submit", "image"}:
        return True
    return any(word in haystack for word in _SUBMIT_WORDS)


def _must_not_fill(text: str) -> bool:
    return any(word in text for word in _NEVER_FILL)


def match_field(field: dict, profile: dict) -> tuple[str, str] | None:
    """Decide what, if anything, belongs in this field.

    Returns (profile_key, value) or None. Longer word matches win, so
    "first name" beats a bare "name".
    """
    text = _text_of(field)
    if not text.strip() or _must_not_fill(text):
        return None
    if _is_submit_control(text, field.get("type", "")):
        return None

    best: tuple[int, str] | None = None
    for key, words in _FIELD_WORDS.items():
        for word in words:
            if word in text and (best is None or len(word) > best[0]):
                best = (len(word), key)
    if best is None:
        return None

    key = best[1]
    value = _value_for(key, profile)
    return (key, value) if value else None


def _value_for(key: str, profile: dict) -> str:
    """Pull a value out of the profile, splitting the name when asked to."""
    full = str(profile.get("full_name") or "").strip()
    if key == "first_name":
        return full.split()[0] if full else ""
    if key == "last_name":
        parts = full.split()
        return " ".join(parts[1:]) if len(parts) > 1 else ""
    return str(profile.get(key) or "").strip()


# ---------------------------------------------------------------------------
# The browser session
# ---------------------------------------------------------------------------

# Playwright objects belong to one thread, and Flask serves each request on a
# different one, so the browser lives in a worker thread driven by a queue.
_commands: "queue.Queue[tuple[str, dict, queue.Queue]]" = queue.Queue()
_worker: threading.Thread | None = None
_lock = threading.Lock()


def available() -> bool:
    """Whether a browser is installed to drive."""
    try:
        import playwright  # noqa: F401

        return True
    except Exception:
        return False


UNAVAILABLE = (
    "Browser automation is not installed. Run `pip install playwright` and then "
    "`playwright install chromium` to enable auto-fill. Everything else — the "
    "portal link and the combined PDF — works without it."
)


def _run_worker() -> None:
    """Owns the browser for its whole life and answers one command at a time."""
    from playwright.sync_api import sync_playwright

    state: dict[str, Any] = {}
    with sync_playwright() as p:
        while True:
            action, payload, reply = _commands.get()
            try:
                if action == "open":
                    if not state.get("browser"):
                        # Headed on purpose: the student watches it happen.
                        state["browser"] = p.chromium.launch(headless=False)
                        state["page"] = state["browser"].new_page(
                            viewport={"width": 1280, "height": 900}
                        )
                    state["page"].goto(payload["url"], wait_until="domcontentloaded",
                                       timeout=60000)
                    reply.put({"ok": True, "url": state["page"].url})

                elif action == "fill":
                    page = state.get("page")
                    if not page:
                        reply.put({"ok": False, "error": "No portal is open yet."})
                    else:
                        reply.put(_fill_page(page, payload["profile"]))

                elif action == "status":
                    page = state.get("page")
                    reply.put({"ok": True, "open": bool(page),
                               "url": page.url if page else ""})

                elif action == "close":
                    if state.get("browser"):
                        state["browser"].close()
                    state.clear()
                    reply.put({"ok": True})

                elif action == "stop":
                    if state.get("browser"):
                        state["browser"].close()
                    reply.put({"ok": True})
                    return
            except Exception as exc:
                reply.put({"ok": False, "error": str(exc)[:300]})


def _send(action: str, **payload) -> dict:
    """Hand a command to the browser thread and wait for its answer."""
    global _worker
    if not available():
        return {"ok": False, "available": False, "error": UNAVAILABLE}

    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_worker, daemon=True)
            _worker.start()

    reply: "queue.Queue[dict]" = queue.Queue()
    _commands.put((action, payload, reply))
    try:
        return reply.get(timeout=payload.pop("timeout", 120))
    except queue.Empty:
        return {"ok": False, "error": "The browser did not respond in time."}


def _fill_page(page, profile: dict) -> dict:
    """Type the profile into the fields we recognise. Touches nothing else."""
    filled, skipped = [], []

    handles = page.query_selector_all("input, textarea, select")
    for handle in handles:
        try:
            tag = (handle.evaluate("e => e.tagName") or "").lower()
            input_type = (handle.get_attribute("type") or "").lower()

            # Never write to a control that could send the form, and never to a
            # password, payment or captcha box.
            # Checkboxes and radios are never touched at all: the only ones on an
            # application form are declarations and consents, and those are the
            # applicant's to tick. There is no profile value for them anyway.
            if tag == "input" and input_type in {
                "submit", "button", "image", "reset", "hidden", "file", "password",
                "checkbox", "radio",
            }:
                continue
            if not handle.is_visible() or not handle.is_enabled():
                continue

            field = {
                "label": _label_for(page, handle),
                "name": handle.get_attribute("name") or "",
                "id": handle.get_attribute("id") or "",
                "placeholder": handle.get_attribute("placeholder") or "",
                "aria": handle.get_attribute("aria-label") or "",
                "type": input_type,
            }
            if _is_submit_control(_text_of(field), input_type):
                continue

            match = match_field(field, profile)
            if not match:
                continue
            key, value = match

            existing = (handle.input_value() or "").strip() if tag != "select" else ""
            if existing:
                skipped.append({"field": field["label"] or field["name"],
                                "why": "already filled in"})
                continue

            if tag == "select":
                try:
                    handle.select_option(label=value)
                except Exception:
                    skipped.append({"field": field["label"] or field["name"],
                                    "why": f"no option matching '{value}'"})
                    continue
            else:
                handle.fill(value)

            # Outline it so the student can see exactly what was touched.
            handle.evaluate("e => e.style.outline = '2px solid #1b3fd3'")
            filled.append({"field": field["label"] or field["name"] or key,
                           "value": value, "matched": key})
        except Exception:
            continue  # one awkward field must not stop the rest

    return {
        "ok": True,
        "filled": filled,
        "skipped": skipped,
        "url": page.url,
        # Stated plainly so no caller can mistake filling for sending.
        "submitted": False,
        "message": "Fields filled. Nothing has been submitted — review the form and "
                   "submit it yourself.",
    }


def _label_for(page, handle) -> str:
    """The visible label for a field, which is usually the best clue to its purpose."""
    try:
        return (handle.evaluate(
            """e => {
                 if (e.labels && e.labels.length) return e.labels[0].innerText;
                 const wrap = e.closest('label');
                 if (wrap) return wrap.innerText;
                 const id = e.getAttribute('id');
                 if (id) {
                   const l = document.querySelector(`label[for="${id}"]`);
                   if (l) return l.innerText;
                 }
                 return '';
               }"""
        ) or "").strip()[:120]
    except Exception:
        return ""


# --- the public surface. Note there is no submit(). ------------------------


def open_portal(url: str) -> dict:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "That is not a valid application URL."}
    return _send("open", url=url)


def fill_form(profile: dict) -> dict:
    return _send("fill", profile=profile or {})


def status() -> dict:
    return _send("status")


def close_portal() -> dict:
    return _send("close")
