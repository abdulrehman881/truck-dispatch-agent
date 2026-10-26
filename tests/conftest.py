"""Shared fixtures: an admin token must be configured for all tests."""

import sys
from pathlib import Path

import pytest

import main

# Make the src/ phase modules importable (main.py does this too).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
src_dir = str(PROJECT_ROOT / "src")
if src_dir not in sys.path:
    sys.path.append(src_dir)

TEST_ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_API_TOKEN", TEST_ADMIN_TOKEN)
    # Never start the background task worker inside tests.
    monkeypatch.setattr(main, "TASK_SCHEDULER_ENABLED", False)
    return TEST_ADMIN_TOKEN
