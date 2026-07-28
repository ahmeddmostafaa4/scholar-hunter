"""Scholar Hunter — the LangChain agent: LLM, tools, and system prompt.

Importable, so app.py reuses this exact agent instead of duplicating logic.
Also runnable on its own for a quick check without the browser:

    python agent.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic

# Load .env from the project root so both `python agent.py` and `python app.py`
# see the same keys. Existing environment variables win over the file.
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The Claude model, kept as a constant so it is a one-line change.
MODEL_NAME = "claude-sonnet-4-6"

# Keep responses roomy enough for a full shortlist without risking a timeout.
MAX_TOKENS = 8000


class MissingKeyError(RuntimeError):
    """Raised when a required API key is absent.

    Carries a human-readable message so the Flask layer can return clean JSON
    instead of leaking a stack trace at the user.
    """


def _require_key(name: str, where: str) -> str:
    """Fetch an API key or fail with an instruction, not a traceback."""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise MissingKeyError(
            f"{name} is not set. Add it to your .env file in the project root "
            f"(copy .env.example to .env). Get a key at {where}."
        )
    return value


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


def build_llm(temperature: float = 0.0, **kwargs) -> ChatAnthropic:
    """Build the Claude chat model used by every tool and by the agent itself.

    temperature defaults to 0 because eligibility reasoning should be stable and
    repeatable; this is a matching task, not a creative one.
    """
    api_key = _require_key("ANTHROPIC_API_KEY", "https://console.anthropic.com")
    return ChatAnthropic(
        model=MODEL_NAME,
        api_key=api_key,
        temperature=temperature,
        max_tokens=kwargs.pop("max_tokens", MAX_TOKENS),
        timeout=kwargs.pop("timeout", 120),
        **kwargs,
    )


def check_llm() -> dict:
    """Smoke-test the Claude connection. Returns a result dict, never raises.

    Used by the Phase 1 test and by the startup banner so a bad key shows up as
    a clear message rather than an exception mid-search.
    """
    try:
        llm = build_llm()
        reply = llm.invoke("Reply with exactly: OK")
        return {"ok": True, "model": MODEL_NAME, "reply": reply.content}
    except MissingKeyError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # network, auth, rate limit — report, don't crash
        return {"ok": False, "error": f"Could not reach Claude ({MODEL_NAME}): {exc}"}


if __name__ == "__main__":
    print(f"Scholar Hunter — checking Claude connection ({MODEL_NAME})...")
    result = check_llm()
    print("  OK:", result["reply"]) if result["ok"] else print("  FAILED:", result["error"])
