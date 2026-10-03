"""Golden provider payloads, and the loader the contract tests share.

Each fixture is a small, real-shaped capture of one provider's price fields. The
values are the ones the units were validated against, so a contract change that
would have moved a price fails here rather than in the router's economics.
"""
from __future__ import annotations

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def golden():
    """Load a provider payload fixture by file name."""

    def _load(name: str) -> dict:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    return _load
