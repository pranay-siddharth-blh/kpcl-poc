"""Shared fixtures. Every test runs without the LLM gateway (no VPN needed); the LLM is mocked where used."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# The whole suite comes from one client address; the per-minute limit is tested on its own (test_stress_fixes), so it
# is lifted here to keep the API tests from tripping it.
os.environ.setdefault("KPCL_RATE_LIMIT_PER_MINUTE", "100000")

from app.config import settings  # noqa: E402
from app.db import ReadOnlyDB  # noqa: E402


@pytest.fixture(scope="session")
def db():
    d = ReadOnlyDB(settings.db_path)
    yield d
    d.close()


def run(coro):
    return asyncio.run(coro)
