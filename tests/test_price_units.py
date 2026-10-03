"""Provider price units — a wrong scale is the quietest bug in the registry.

Every adapter converts a provider's own units into the registry's one unit:
**dollars per Mtok** (`pricing.units.to_usd_per_mtok`, declared per provider in
`pricing.providers.PROVIDER_PRICING`). A mistake here is invisible — the number
looks plausible, and the router simply over- or under-charges by a constant
factor. Novita's went unnoticed until a hibernation reason read "$0.75/Mtok" for
a model four other providers priced at $0.075.

This file is the *adapter-level* regression: it runs a real adapter over a real
payload. The unit table itself, and every provider's field path, are covered in
`tests/pricing/`.
"""
from __future__ import annotations

import pytest

import mininfer.ingest  # noqa: F401 - importing registers every adapter
from mininfer.fetch import Snapshot
from mininfer.ingest.shared import ADAPTERS, per_mtok_from_per_token


def _snap(source: str, payload: dict) -> Snapshot:
    return Snapshot(source=source, url=f"https://example.test/{source}",
                    fetched_at="2026-01-01T00:00:00+00:00", sha256="", nbytes=0,
                    storage_uri="", payload=payload)


def test_the_registry_unit_is_dollars_per_mtok():
    """The one conversion everything else is measured against."""
    assert per_mtok_from_per_token(0.0000003) == pytest.approx(0.30)


def test_novita_prices_are_per_myriad_mtok():
    """Novita's `*_price_per_m` is 1e-4 USD/Mtok: raw 750 means $0.075/Mtok.

    Five models cross-checked against OpenRouter: `/10000` matches each to four
    decimals, the old `/1000` matched none. The adapter emits the observation
    here; the *stored* price is resolved from it by `Store.upsert_deployment`
    (see `tests/pricing/test_provider_contracts.py`).
    """
    run = ADAPTERS["novita"](_snap("novita", {"data": [{
        "id": "inclusionai/ling-3.0-flash-fin",
        "input_token_price_per_m": 750,
        "output_token_price_per_m": 2200,
        "context_size": 262144,
    }]}))
    price = run.bundles[0].prices
    assert price.price_in == pytest.approx(0.075)
    assert price.price_out == pytest.approx(0.22)


def test_novita_matches_a_second_provider_for_the_same_weights():
    """The independent check: another provider's published price agrees."""
    run = ADAPTERS["novita"](_snap("novita", {"data": [{
        "id": "qwen/qwen3.8-27b",
        "input_token_price_per_m": 4200,
        "output_token_price_per_m": 4200,
        "context_size": 262144,
    }]}))
    price = run.bundles[0].prices
    # OpenRouter publishes $0.42/$0.42 per Mtok for these weights.
    assert price.price_in == pytest.approx(0.42)
    assert price.price_out == pytest.approx(0.42)
