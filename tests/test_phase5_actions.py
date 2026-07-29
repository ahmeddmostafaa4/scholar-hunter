"""Phase 5 tests: the two tools with side effects.

The rules under test are the safety-critical ones: the email tool drafts and
never sends, the deadline store stays valid JSON across many writes, and a
missing credentials.json degrades to a clear message instead of breaking the
agent.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402


# --- save_deadline ---------------------------------------------------------


def test_saves_an_entry(tmp_path):
    store = tmp_path / "deadlines.json"
    result = agent.save_deadline_entry("DAAD Study Scholarship", "15 January 2027",
                                       "https://daad.de/x", path=store)
    assert result["ok"] and result["saved"]

    saved = json.loads(store.read_text())
    assert len(saved) == 1
    assert saved[0]["scholarship_name"] == "DAAD Study Scholarship"
    assert saved[0]["deadline_date"] == "15 January 2027"
    assert saved[0]["url"] == "https://daad.de/x"
    assert saved[0]["saved_at"]


def test_json_stays_valid_across_many_appends(tmp_path):
    """The spec's requirement: appends must not corrupt the file."""
    store = tmp_path / "deadlines.json"
    for i in range(12):
        result = agent.save_deadline_entry(f"Scholarship {i}", f"1 March 202{i % 10}",
                                           f"https://example.org/{i}", path=store)
        assert result["ok"]
        # Re-parse after *every* write, not just at the end.
        entries = json.loads(store.read_text())
        assert isinstance(entries, list)
        assert len(entries) == i + 1

    entries = json.loads(store.read_text())
    assert [e["scholarship_name"] for e in entries] == [f"Scholarship {i}" for i in range(12)]


def test_a_corrupted_store_does_not_block_saving(tmp_path):
    """A half-written file must not lock the student out of saving."""
    store = tmp_path / "deadlines.json"
    store.write_text('[{"scholarship_name": "truncated"')  # invalid JSON

    result = agent.save_deadline_entry("Recovered", "1 May 2027", path=store)
    assert result["ok"]
    assert json.loads(store.read_text())[0]["scholarship_name"] == "Recovered"


def test_missing_fields_are_rejected(tmp_path):
    store = tmp_path / "deadlines.json"
    assert agent.save_deadline_entry("", "1 Jan 2027", path=store)["ok"] is False
    assert agent.save_deadline_entry("Name", "", path=store)["ok"] is False
    assert not store.exists(), "a rejected save must not create the file"


def test_no_temp_file_is_left_behind(tmp_path):
    store = tmp_path / "deadlines.json"
    agent.save_deadline_entry("A", "1 Jan 2027", path=store)
    assert list(tmp_path.glob("*.tmp")) == []


# --- draft_email -----------------------------------------------------------


def test_without_credentials_the_message_is_clear_not_a_crash(monkeypatch):
    monkeypatch.setattr(agent, "gmail_available", lambda: False)

    result = agent.create_gmail_draft("office@uni.de", "Inquiry", "Hello")
    assert result["ok"] is False
    assert result["available"] is False
    assert "credentials.json" in result["error"]
    assert "works without it" in result["error"]

    text = agent.draft_email.invoke(
        {"to": "office@uni.de", "subject": "Inquiry", "body": "Hello"}
    )
    assert "DRAFT NOT CREATED" in text
    assert "credentials.json" in text


def test_the_agent_still_runs_when_gmail_is_absent(monkeypatch):
    """Gmail must never be a hard dependency of the whole agent."""
    monkeypatch.setattr(agent, "gmail_available", lambda: False)
    names = agent.describe_tools()
    assert "search_scholarships" in names
    assert "check_eligibility" in names
    # The tool is still registered — it just reports itself unavailable when used.
    assert "draft_email" in names


def test_empty_recipient_is_rejected_before_touching_gmail(monkeypatch):
    def explode():
        raise AssertionError("must not reach the Gmail layer for invalid input")

    monkeypatch.setattr(agent, "gmail_available", explode)
    assert agent.create_gmail_draft("", "Subject", "Body")["ok"] is False


def test_only_the_compose_scope_is_requested():
    """The agent should not hold the ability to send, not merely decline to use it."""
    source = Path(agent.__file__).read_text()
    assert "gmail.compose" in source
    assert "gmail.send" not in source
    assert "https://mail.google.com/" not in source, "that scope grants full mailbox access"


def _stub_gmail(monkeypatch):
    """Stand in for the whole Gmail path — credentials, service, and toolkit.

    `_gmail_credentials` must be stubbed: it runs the real OAuth flow, which
    blocks on a browser consent screen and hangs the suite forever.
    """
    import types

    monkeypatch.setattr(agent, "gmail_available", lambda: True)
    monkeypatch.setattr(agent, "_gmail_credentials", lambda: object())

    class FakeTool:
        name = "create_gmail_draft"

        def invoke(self, _payload):
            return "Draft created. Draft Id: r-12345"

    class FakeToolkit:
        def get_tools(self):
            return [FakeTool()]

    fake_community = types.ModuleType("langchain_google_community")
    fake_community.GmailToolkit = lambda **kw: FakeToolkit()
    monkeypatch.setitem(sys.modules, "langchain_google_community", fake_community)

    fake_discovery = types.ModuleType("googleapiclient.discovery")
    fake_discovery.build = lambda *a, **kw: object()
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", fake_discovery)


def test_draft_result_states_that_nothing_was_sent(monkeypatch):
    _stub_gmail(monkeypatch)

    result = agent.create_gmail_draft("office@uni.de", "Inquiry", "Hello")
    assert result["ok"] is True
    assert result["sent"] is False
    assert result["draft_id"] == "r-12345"  # Gmail draft ids carry an "r-" prefix
    assert "NOT been sent" in result["message"]


def test_the_broken_upstream_credential_helper_is_not_used():
    """Regression: langchain-google-community 2.0.10 ships a helper that
    misspells its own parameter (`client_sercret_file`) and unpacks a
    `ServiceCredentials` class that does not exist in google.oauth2 — it raises
    on every path. We run the documented OAuth flow ourselves instead."""
    # Check for real use, not mentions — the comment above _gmail_credentials
    # names the broken helper on purpose, to explain why we avoid it.
    code = [
        line
        for line in Path(agent.__file__).read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code)
    assert "langchain_google_community.gmail.utils" not in code
    assert "get_gmail_credentials(" not in code
    assert "build_resource_service(" not in code
    assert "InstalledAppFlow.from_client_secrets_file" in code


def test_a_stale_token_file_does_not_break_drafting(monkeypatch, tmp_path):
    """A hand-edited or corrupted token.json should fall back to re-authorising,
    not raise."""
    bad_token = tmp_path / "token.json"
    bad_token.write_text("{ not json")
    monkeypatch.setattr(agent, "TOKEN_FILE", bad_token)

    reauthorised = {"ran": False}

    class FakeFlow:
        @staticmethod
        def from_client_secrets_file(_secrets, _scopes):
            reauthorised["ran"] = True
            return FakeFlow()

        def run_local_server(self, port=0):
            class Creds:
                valid = True

                def to_json(self):
                    return '{"token": "fresh"}'

            return Creds()

    import types

    fake_flow_mod = types.ModuleType("google_auth_oauthlib.flow")
    fake_flow_mod.InstalledAppFlow = FakeFlow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", fake_flow_mod)

    agent._gmail_credentials()
    assert reauthorised["ran"], "a corrupt token must trigger re-authorisation"
    assert "fresh" in bad_token.read_text()


# --- wiring ----------------------------------------------------------------


def test_action_tools_are_registered():
    names = agent.describe_tools()
    assert "draft_email" in names
    assert "save_deadline" in names


def test_shortlist_toolset_excludes_side_effects():
    """The search path must not be able to draft or save, even by accident."""
    names = [t.name for t in agent.build_tools(include_actions=False)]
    assert "draft_email" not in names
    assert "save_deadline" not in names
    assert "search_scholarships" in names


def test_calendar_todo_is_marked_for_the_future():
    source = Path(agent.__file__).read_text()
    assert "TODO (Google Calendar)" in source
