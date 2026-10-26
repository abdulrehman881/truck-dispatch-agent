"""Shared fixtures: an admin token must be configured for all tests."""

import pytest

import main

TEST_ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_API_TOKEN", TEST_ADMIN_TOKEN)
    return TEST_ADMIN_TOKEN
