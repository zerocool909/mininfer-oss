"""No adapter may scale a price with a local literal.

The rule the pricing layer exists to enforce: a conversion factor is *data*
(`pricing.providers.PROVIDER_PRICING` -> `pricing.units.UNITS`), never a number an
adapter divides by. Novita's `/1000` shipped because an adapter was *able* to do
that; this test makes the next one impossible to write by accident.

It is a source scan, not a behavioural test, deliberately: the property is "this
expression does not appear", which no runtime assertion can state.
"""
from __future__ import annotations

import ast
import pathlib
import re

from mininfer.pricing.providers import PROVIDER_PRICING

ADAPTERS_DIR = (
    pathlib.Path(__file__).resolve().parents[2] / "mininfer" / "ingest" / "adapters"
)

# Factors that only ever mean "convert a price". A bare `10000` is deliberately
# NOT here: Cloudflare's `neurons_per_day: 10000` is a legitimate quota limit.
FORBIDDEN = (
    "1_000_000",
    "10_000",
    "1e6",
    "1e-4",
    "0.0001",
    "per_mtok_from_per_token(",
    "price_sentinel(",
)


def _code_lines(path: pathlib.Path):
    """Yield (lineno, code) with whole-line and trailing `#` comments removed.

    Docstrings are not stripped. That is intentional for `_template.py`: the
    template teaches the pattern and must not reintroduce the literals either.
    """
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield i, line.split("#", 1)[0]


def test_no_adapter_scales_a_price_locally():
    offenders: list[str] = []
    for path in sorted(ADAPTERS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for lineno, code in _code_lines(path):
            for token in FORBIDDEN:
                if token in code:
                    offenders.append(f"{path.name}:{lineno}: {token}")
    assert offenders == [], (
        "an adapter is scaling a price locally; move the factor into a pricing "
        f"contract instead: {offenders}"
    )


def test_every_pricing_adapter_reads_through_the_contract():
    for provider in PROVIDER_PRICING:
        src = (ADAPTERS_DIR / f"{provider}.py").read_text(encoding="utf-8")
        assert "read_prices_for(" in src, (
            f"{provider}.py does not call read_prices_for(); it must emit a price "
            "observation rather than setting the deployment's price itself"
        )


def test_no_adapter_sets_a_price_on_the_deployment():
    """The P1 rule: an adapter emits observations and never decides the price.

    `make_deploy(..., pin=<value>, ...)` is how an adapter used to *be* the truth.
    The stored economics now come from `Store.upsert_deployment(prices=...)`, so a
    concrete price keyword in an adapter body is the old architecture creeping
    back. `pin=None` is allowed: it is how a source that publishes no price says
    so out loud rather than by omission.

    Parsed with `ast`, not a regex: `pin, pout = price.price_in, ...` is a read of
    the observation and must not be flagged, and only a parse can tell the two
    apart.
    """
    offenders = []
    for path in sorted(ADAPTERS_DIR.glob("*.py")):
        if path.name in ("__init__.py", "_template.py"):
            continue  # the template is entirely commented out
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name != "make_deploy":
                continue
            for kw in node.keywords:
                is_none = isinstance(kw.value, ast.Constant) and kw.value.value is None
                if kw.arg in ("pin", "pout", "pcache") and not is_none:
                    offenders.append(
                        f"{path.name}:{node.lineno}: make_deploy({kw.arg}=<concrete>)"
                    )
    assert offenders == [], (
        "an adapter is setting a deployment price instead of emitting an "
        f"observation: {offenders}"
    )


def test_no_adapter_re_derives_a_unit_from_magnitude():
    """A price's unit must be declared, never inferred from how big it looks.

    Inferring is what let the Novita error survive: `0.75` looks like a plausible
    per-Mtok price for *some* model, so nothing about the number itself is wrong.
    """
    for path in sorted(ADAPTERS_DIR.glob("*.py")):
        code = "\n".join(line for _, line in _code_lines(path))
        assert "raw_unit" not in code and "usd_per_" not in code, path.name
