"""Every provider's price unit, pinned against a golden payload.

These are stronger than testing `to_usd_per_mtok` in isolation: they exercise the
whole path a real ingest takes —

    provider payload -> contract field path -> declared unit -> USD/Mtok

so a wrong field path and a wrong unit both fail here, not in production. The
set of adapters that price is asserted explicitly, so adding one without a
contract is a deliberate act rather than an omission.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import mininfer.ingest  # noqa: F401 - importing registers every adapter
from mininfer.fetch import Snapshot
from mininfer.ingest.shared import ADAPTERS
from mininfer.pricing.providers import (
    PROVIDER_PRICING,
    contract_is_well_formed,
    read_prices,
)
from mininfer.pricing.units import UNITS

# Adapters that read a price field. Kept explicit: a provider that starts
# publishing prices must be added here *and* given a contract, in one reviewable
# change. (Adapters that carry no price — google, cloudflare, cohere, github,
# nvidia, openai_compat — are deliberately absent.)
PRICING_ADAPTERS = frozenset({
    "novita", "openrouter", "deepinfra", "vercel", "sambanova", "chutes", "hf_router",
})


# --------------------------------------------------------------- contract shape


def test_every_contract_is_well_formed():
    for name, spec in PROVIDER_PRICING.items():
        problems = contract_is_well_formed(spec)
        assert problems == [], f"{name}: {problems}"


def test_contracts_cover_exactly_the_pricing_adapters():
    assert set(PROVIDER_PRICING) == PRICING_ADAPTERS


def test_every_contract_belongs_to_a_registered_adapter():
    assert set(PROVIDER_PRICING) <= set(ADAPTERS)


def test_every_contract_unit_is_declared():
    for spec in PROVIDER_PRICING.values():
        for field in spec.fields:
            assert field.unit in UNITS, field.unit


def test_a_contract_that_claims_validation_says_what_validated_it():
    """`validated=True` is a claim. It must be backed by a note, not asserted."""
    for spec in PROVIDER_PRICING.values():
        if spec.validated:
            note = spec.note.lower()
            assert "cross-check" in note or "verified" in note or "validated" in note, spec.provider


def test_reading_a_provider_without_a_contract_raises():
    from mininfer.pricing.units import PriceUnitError

    with pytest.raises(PriceUnitError):
        read_prices("definitely-not-a-provider", {"pricing": {"prompt": 1}})


# --------------------------------------------------------------- golden payloads


@pytest.mark.parametrize(
    "provider,fixture,path",
    [
        ("novita", "novita.json", ()),
        ("openrouter", "openrouter.json", ()),
        ("deepinfra", "deepinfra.json", ()),
        ("vercel", "vercel.json", ()),
        ("sambanova", "sambanova.json", ()),
        ("chutes", "chutes.json", ()),
        # hf_router nests each provider's price one level down, under `providers[]`.
        ("hf_router", "hf_router.json", ("providers", 0)),
    ],
)
def test_contract_paths_resolve_a_price_in_a_real_payload(golden, provider, fixture, path):
    """A typo'd field path is the other silent price bug: the contract looks
    fine but resolves nothing, and every arm becomes 'unknown'."""
    row = golden(fixture)["data"][0]
    for key in path:
        row = row[key]
    price = read_prices(provider, row)
    assert price.decimal("input") is not None
    assert price.decimal("output") is not None


def test_novita_golden_prices(golden):
    """The regression the whole layer exists for: raw 750 == $0.075/Mtok."""
    row = golden("novita.json")["data"][0]
    price = read_prices("novita", row)
    assert price.decimal("input") == Decimal("0.075")
    assert price.decimal("output") == Decimal("0.22")

    obs = price.observation("input")
    assert obs.raw_value == Decimal("750")          # raw value preserved
    assert obs.raw_unit == "usd_per_myriad_mtok"    # and its unit named


@pytest.mark.parametrize(
    "raw_in,raw_out,expected_in,expected_out",
    [
        (8340, 25010, "0.834", "2.501"),
        (13200, 39600, "1.32", "3.96"),
        (1500, 1500, "0.15", "0.15"),
        (4200, 4200, "0.42", "0.42"),
        (20000, 20000, "2.0", "2.0"),
    ],
)
def test_novita_myriad_scale_is_exact(raw_in, raw_out, expected_in, expected_out):
    price = read_prices("novita", {
        "input_token_price_per_m": raw_in,
        "output_token_price_per_m": raw_out,
    })
    assert price.decimal("input") == Decimal(expected_in)
    assert price.decimal("output") == Decimal(expected_out)


def test_openrouter_golden_prices(golden):
    rows = {r["id"]: r for r in golden("openrouter.json")["data"]}
    price = read_prices("openrouter", rows["inclusionai/ling-3.0-flash-fin"])
    assert price.decimal("input") == Decimal("0.075")
    assert price.decimal("output") == Decimal("0.22")
    assert price.decimal("cached_input") == Decimal("0.0375")


def test_openrouter_free_variant_is_free_not_unknown(golden):
    rows = {r["id"]: r for r in golden("openrouter.json")["data"]}
    price = read_prices("openrouter", rows["inclusionai/ling-3.0-flash-fin:free"])
    assert price.decimal("input") == Decimal("0")
    assert price.observation("input").pricing_type == "free"
    assert price.zero_price is True


def test_openrouter_sentinel_is_unknown_not_free(golden):
    """`-1` means "depends on the inner router", not $0. Reading it as a number
    would make a competitor's meta-router look free."""
    rows = {r["id"]: r for r in golden("openrouter.json")["data"]}
    price = read_prices("openrouter", rows["openrouter/auto"])
    assert price.decimal("input") is None
    assert price.decimal("output") is None
    assert {o.kind for o in price.sentinels} == {"input", "output"}
    assert price.zero_price is False


def test_deepinfra_golden_prices(golden):
    row = golden("deepinfra.json")["data"][0]
    price = read_prices("deepinfra", row)
    assert price.decimal("input") == Decimal("0.03")
    assert price.decimal("output") == Decimal("0.05")
    assert price.decimal("cached_input") == Decimal("0.015")


def test_vercel_golden_prices(golden):
    rows = {r["id"]: r for r in golden("vercel.json")["data"]}
    paid = read_prices("vercel", rows["alibaba/qwen3-coder"])
    assert paid.decimal("input") == Decimal("0.4")
    assert paid.decimal("output") == Decimal("1.6")

    free = read_prices("vercel", rows["poolside/laguna-s-2.1-free"])
    assert free.zero_price is True
    assert free.observation("input").pricing_type == "free"


def test_sambanova_golden_prices(golden):
    row = golden("sambanova.json")["data"][0]
    price = read_prices("sambanova", row)
    assert price.decimal("input") == Decimal("0.6")
    assert price.decimal("output") == Decimal("1.2")


def test_chutes_golden_prices(golden):
    row = golden("chutes.json")["data"][0]
    price = read_prices("chutes", row)
    assert price.decimal("input") == Decimal("0.3")
    assert price.decimal("output") == Decimal("1.2")
    assert price.decimal("cached_input") == Decimal("0.15")


def test_hf_router_reads_each_provider_block(golden):
    """The unit is a property of the payload shape, not of whichever provider the
    block names — so the same contract parses every block."""
    providers = golden("hf_router.json")["data"][0]["providers"]
    paid, free = providers

    paid_price = read_prices("hf_router", paid)
    assert paid_price.decimal("input") == Decimal("0.2")
    assert paid_price.decimal("output") == Decimal("0.8")
    assert paid_price.zero_price is False

    free_price = read_prices("hf_router", free)
    assert free_price.decimal("input") == Decimal("0")
    assert free_price.zero_price is True


def test_missing_price_fields_are_unknown(golden):
    """A row that omits a priced field yields unknown, never zero."""
    price = read_prices("novita", {"id": "x"})
    assert price.decimal("input") is None
    assert price.decimal("output") is None
    assert price.observation("input").pricing_type == "unknown"


# ------------------------------------------------- adapter end-to-end
#
# `read_prices` being right is not the same as the *adapter* writing the right
# number: the contract could resolve correctly and the adapter could still read
# the wrong field, or pass the wrong value to `make_deploy`. These run the real
# adapter over the golden payload and assert the resulting `Deployment.price_*`.
# That is what makes the migration provably behaviour-preserving for every
# provider except Novita, whose value was already correct at `/10000`.


def _snap(source: str, payload) -> Snapshot:
    return Snapshot(
        source=source,
        url=f"https://example.test/{source}",
        fetched_at="2026-01-01T00:00:00+00:00",
        sha256="",
        nbytes=0,
        storage_uri="",
        payload=payload,
    )


ADAPTER_PRICE_CASES = [
    # provider, fixture, adapter kwargs, deploy_id, expected $/Mtok in, out
    ("novita", "novita.json", {},
     "novita:inclusionai/ling-3.0-flash-fin", "0.075", "0.22"),
    ("openrouter", "openrouter.json", {"with_endpoints": False},
     "openrouter:inclusionai/ling-3.0-flash-fin", "0.075", "0.22"),
    ("deepinfra", "deepinfra.json", {},
     "deepinfra:meta-llama/Meta-Llama-3.1-8B-Instruct", "0.03", "0.05"),
    ("vercel", "vercel.json", {},
     "vercel:alibaba/qwen3-coder", "0.4", "1.6"),
    ("sambanova", "sambanova.json", {},
     "sambanova:Meta-Llama-3.3-70B-Instruct", "0.6", "1.2"),
    ("chutes", "chutes.json", {},
     "chutes:deepseek-ai/DeepSeek-V3", "0.3", "1.2"),
    ("hf_router", "hf_router.json", {},
     "hf/novita:Qwen/Qwen3-235B-A22B", "0.2", "0.8"),
]


@pytest.mark.parametrize(
    "provider,fixture,kwargs,deploy_id,exp_in,exp_out", ADAPTER_PRICE_CASES
)
def test_adapter_emits_the_contract_price(
    golden, provider, fixture, kwargs, deploy_id, exp_in, exp_out
):
    """The adapter's output is an *observation*, not a deployment price.

    Asserting on `bundle.prices` is asserting the adapter's real contract now: it
    says what it read and in what unit, and never sets the deployment's price.
    """
    run = ADAPTERS[provider](_snap(provider, golden(fixture)), **kwargs)
    b = next(b for b in run.bundles if b.deployment.deploy_id == deploy_id)
    assert b.prices is not None, f"{provider} emitted no price observations"
    assert b.prices.decimal("input") == Decimal(exp_in)
    assert b.prices.decimal("output") == Decimal(exp_out)
    # ...and it deliberately did not put the price on the deployment.
    assert b.deployment.price_in is None
    assert b.deployment.price_out is None


def test_the_store_resolves_the_stored_price_from_the_observation(golden, tmp_path):
    """The other half of the seam: what the adapter emitted is what gets stored.

    Without this, `bundle.prices` could be right and the registry still wrong.
    """
    from mininfer.store import Store

    s = Store(tmp_path / "priced.db")
    run = ADAPTERS["novita"](_snap("novita", golden("novita.json")))
    for b in run.bundles:
        s.upsert_weights(b.weights, b.evidence)
        s.upsert_deployment(b.deployment, b.evidence, run.quarantine, prices=b.prices)
    s.commit()
    row = s.conn.execute(
        "SELECT price_in, price_out FROM deployments WHERE deploy_id=?",
        ("novita:inclusionai/ling-3.0-flash-fin",),
    ).fetchone()
    assert Decimal(str(row["price_in"])) == Decimal("0.075")
    assert Decimal(str(row["price_out"])) == Decimal("0.22")
    s.close()


def test_novita_adapter_no_longer_produces_the_ten_x_value(golden):
    """The regression in its P1 form: the emitted observation is the correct price
    and the wrong `/1000` value is nowhere in the bundle."""
    run = ADAPTERS["novita"](_snap("novita", golden("novita.json")))
    b = next(b for b in run.bundles
             if b.deployment.provider_model_id == "inclusionai/ling-3.0-flash-fin")
    assert b.prices.price_in == 0.075
    assert b.prices.price_in != 0.75
