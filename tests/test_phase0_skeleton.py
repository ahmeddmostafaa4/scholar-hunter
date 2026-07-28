"""Phase 0 tests: the skeleton is sound and no secrets are tracked by git."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that must never be committed, even if they exist on disk.
SECRET_FILES = [".env", "credentials.json", "token.json"]


def test_expected_files_exist():
    for name in [
        ".gitignore",
        ".env.example",
        "requirements.txt",
        "README.md",
        "PROJECT_MEMORY.md",
        "agent.py",
        "app.py",
    ]:
        assert (ROOT / name).is_file(), f"missing {name}"
    for name in ["templates", "static", "tests"]:
        assert (ROOT / name).is_dir(), f"missing directory {name}"


def test_gitignore_covers_every_secret():
    ignored = (ROOT / ".gitignore").read_text()
    for pattern in SECRET_FILES + ["__pycache__/", "*.pyc", "deadlines.json", ".venv/"]:
        assert pattern in ignored, f".gitignore is missing {pattern}"


def test_no_secrets_are_tracked_by_git():
    """The real check: ask git what it tracks, not what .gitignore claims."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for path in tracked:
        assert Path(path).name not in SECRET_FILES, f"secret file is tracked: {path}"
        assert ".venv/" not in path, f"virtualenv is tracked: {path}"


def test_env_example_is_tracked_and_has_no_values():
    """The template is committed, but only as empty keys."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert ".env.example" in tracked

    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            assert value == "", f"{key} has a value filled in — that is a leak risk"


def test_stub_modules_import():
    """agent.py must stay importable — app.py reuses it rather than duplicating logic."""
    result = subprocess.run(
        [sys.executable, "-c", "import agent, app"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_reused_packages_are_importable():
    """cms-app/ and portal-app/ are reused in place, not reimplemented."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path[:0] = ['portal-app', 'cms-app']; "
            "from guc_portal import GucPortal; from guc_cms import GucCms",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
