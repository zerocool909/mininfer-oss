"""Ingestion shape tests.

The interesting case is a key-gated provider that publishes *no* pricing: the
API key buys rate-limited access at $0, so the deployment must carry a free
marker or the router rejects it as "price unknown" and it can never be used.

The catalogue and smoke tests below exist because the adapters were split into
`mi/ingest/adapters/` with almost no coverage: 814 lines and 14 adapters behind
two assertions. They pin what the split must not change — the registry's exact
contents, and that every adapter is a pure function of a snapshot that tolerates a
payload it does not recognise. A live API is not something a unit test can call,
but "returns an empty Run instead of raising" is a property worth holding.
"""
from __future__ import annotations

import pytest

from mininfer.fetch import Snapshot
from mininfer.ingest import ADAPTERS, SOURCES, ingest_openai_compat


def _snap(source: str, models: list[dict] | None = None, *, payload=None) -> Snapshot:
    return Snapshot(
        source=source,
        url=f"https://example.test/{source}/models",
        fetched_at="2026-01-01T00:00:00+00:00",
        sha256="0" * 64,
        nbytes=1,
        storage_uri="s3://test",
        payload=payload if payload is not None else {"data": models or []},
    )


def test_keyed_free_tier_is_marked_free_not_unknown_price():
    run = ingest_openai_compat(
        _snap("groq", [{"id": "llama-3.3-70b", "context_window": 131072}])
    )
    d = run.bundles[0].deployment
    assert d.price_in is None and d.price_out is None   # the API publishes none
    assert d.trial_credits is True                      # but the tier is free
    assert d.limits.get("rpd")                          # and the allowance is recorded


def test_a_provider_without_a_declared_free_tier_stays_unknown():
    # `together` is pay-per-token with signup credits, not a free tier: no marker.
    run = ingest_openai_compat(_snap("together", [{"id": "meta-llama/x"}]))
    d = run.bundles[0].deployment
    assert d.trial_credits is False
    assert d.limits == {}


# --------------------------------------------------------------------------- #
# the catalogue
# --------------------------------------------------------------------------- #

#: Every source `mi ingest` knows about. Adding one is a deliberate act, so this
#: list is spelled out rather than derived — a source that vanishes silently takes
#: its deployments of coverage with it, and the count is not obvious from any
#: single place (`cmd_ingest` only runs tier 0 by default).
EXPECTED_SOURCES = {
    "cerebras", "chutes", "cloudflare", "cohere", "dashscope", "deepinfra",
    "deepseek", "fireworks", "github", "google", "groq",
    "huggingface", "hyperbolic", "kluster", "lmstudio", "mistral", "moonshot",
    "nebius", "novita", "nvidia", "ollama", "openrouter", "sambanova",
    "together", "vercel", "vllm", "zhipu",
}


def test_the_catalogue_is_intact():
    assert set(SOURCES) == EXPECTED_SOURCES


def test_every_source_can_be_dispatched():
    """A source naming an `kind` with no adapter is a runtime failure waiting."""
    missing = sorted({s.kind for s in SOURCES.values()} - set(ADAPTERS))
    assert not missing, f"sources point at adapters that do not exist: {missing}"


def test_every_source_describes_itself():
    for name, spec in SOURCES.items():
        assert spec.name == name, name
        assert spec.note, f"{name} has no note explaining what it is for"
        assert spec.tier in (0, 1, 2), name
        assert spec.confidence in ("aggregator_api", "provider_api", "scraped")


def test_a_source_without_an_endpoint_says_so_clearly():
    """`cloudflare` ships no default URL — the account id is part of it.

    `cmd_ingest` skips URL-less sources in its default sweep, so this is only
    reached when one is named explicitly, and it must fail legibly rather than
    fetching an empty string.
    """
    url_less = [n for n, s in SOURCES.items() if not s.url]
    assert url_less, "the fixture this test assumes no longer holds"
    from mininfer.ingest import run_source

    with pytest.raises(ValueError, match="needs configuration"):
        run_source(url_less[0])


def test_tier_zero_sources_need_no_credential():
    """The keyless tier is what makes a cold start possible at all."""
    for name, spec in SOURCES.items():
        if spec.tier == 0:
            assert spec.available, f"{name} is tier 0 but reports unavailable"


def test_a_keyed_source_is_unavailable_without_its_key(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert SOURCES["mistral"].available is False
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    assert SOURCES["mistral"].available is True


# --------------------------------------------------------------------------- #
# every adapter, on a payload it does not recognise
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", sorted(ADAPTERS))
def test_an_adapter_survives_an_unrecognised_payload(kind):
    """The property the split must preserve for all fourteen.

    A provider changing its response shape must produce an empty run that `mi
    ingest` can report, not a traceback halfway through a 23,000-row ingest.
    """
    run = ADAPTERS[kind](_snap(kind, payload={}))
    assert run.bundles == []
    assert run.quarantine == []


@pytest.mark.parametrize("kind", sorted(ADAPTERS))
def test_a_wrong_typed_payload_never_invents_rows(kind):
    """A list where an object was expected is a shape change, not data.

    Either outcome is acceptable — raise, or return nothing — because `cmd_ingest`
    contains a per-source failure (see below). What is not acceptable is producing
    bundles from it: a fabricated row goes into the registry and is believed.
    """
    try:
        run = ADAPTERS[kind](_snap(kind, payload=[]))
    except (AttributeError, TypeError, KeyError):
        return
    assert run.bundles == [], f"{kind} invented {len(run.bundles)} rows from a list"


def test_one_broken_source_does_not_abort_the_ingest(tmp_path, monkeypatch, capsys):
    """The containment that makes the raise above acceptable."""
    import mininfer.ingest as ing
    from mininfer import cli

    monkeypatch.setenv("MI_DB", str(tmp_path / "p.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")

    def fake_run_source(name, *, force=False, **kw):
        if name == "vercel":
            raise AttributeError("'list' object has no attribute 'get'")
        return ing.Run()

    monkeypatch.setattr(ing, "run_source", fake_run_source)
    # `sources` is positional
    rc = cli.main(["--db", str(tmp_path / "p.db"), "ingest", "vercel", "groq"])
    err = capsys.readouterr().err
    assert rc == 0, "a failing source must not fail the command"
    assert "vercel" in err and "FAILED" in err
    assert "AttributeError" in err


def test_adapter_names_are_unique_per_kind():
    """Two adapters under one kind would silently shadow each other."""
    from mininfer.ingest import ADAPTERS as A

    assert len(A) == len(set(A))


# --------------------------------------------------------------------------- #
# the contributor template
# --------------------------------------------------------------------------- #


def test_the_template_registers_nothing():
    """It is documentation that happens to be Python.

    If someone uncomments it to start a provider and forgets to rename the kind,
    a source called `example` would appear in the registry and be pulled by
    `mi ingest` against an endpoint that does not exist.
    """
    import importlib

    from mininfer.ingest import ADAPTERS as before

    importlib.import_module("mininfer.ingest.adapters._template")
    assert set(ADAPTERS) == set(before)
    assert "example" not in ADAPTERS


def test_the_template_is_not_in_the_catalogue():
    assert "example" not in SOURCES


def test_every_adapter_module_is_registered():
    """A file in `adapters/` that registers nothing is dead code.

    The template is the one deliberate exception. The directory comes from the
    imported package rather than a hardcoded path: a stale path made this test
    pass by iterating nothing at all, which is worse than failing.
    """
    import importlib
    import pathlib

    import mininfer.ingest.adapters as adapters_pkg
    from mininfer.ingest import ADAPTERS

    registered = set(ADAPTERS.values())
    adapters_dir = pathlib.Path(adapters_pkg.__file__).parent
    files = [p for p in sorted(adapters_dir.glob("*.py"))
             if not p.stem.startswith("_")]
    assert files, f"no adapter modules found in {adapters_dir}"
    for path in files:
        mod = importlib.import_module(f"{adapters_pkg.__name__}.{path.stem}")
        fns = [v for k, v in vars(mod).items()
               if k.startswith("ingest_") and callable(v)]
        assert fns, f"{path.name} defines no adapter"
        assert any(fn in registered for fn in fns), f"{path.name} is never registered"


# --------------------------------------------------------------------------- #
# two bugs found by actually running every upstream source (P8 verification)
# --------------------------------------------------------------------------- #


def test_a_local_source_does_not_ingest_mininfer_itself():
    """`:8000` is both our published port and `vllm`'s default URL.

    With a container running, `mi ingest` read MinInfer's *own* `/v1/models` and
    turned task names — `auto`, `general_chat`, `sql_generation` — into 36 invented
    "models" in the registry. A model list that says it belongs to us is ours.
    """
    run = ingest_openai_compat(_snap("vllm", [
        {"id": "auto", "owned_by": "mininfer"},
        {"id": "general_chat", "owned_by": "mininfer"},
        {"id": "sql_generation", "owned_by": "mininfer"},
        {"id": "real-model", "owned_by": "vllm"},
    ]))
    assert [b.deployment.provider_model_id for b in run.bundles] == ["real-model"]


def test_a_source_that_declares_a_key_is_not_listed_as_keyless():
    """Tier 0 means keyless, and `available` now takes `key_env` as the fact.

    `deepseek` was tier 0 *and* declared `DEEPSEEK_API_KEY`, so `available` said yes,
    `mi ingest` tried it on every run, and the API answered 401 on every run — a
    permanent failure line nothing checked.
    """
    from mininfer.ingest import SOURCES

    keyless = {n for n, s in SOURCES.items() if s.key_env is None}
    assert keyless <= {n for n, s in SOURCES.items() if s.tier in (0, 2)}
    for name, spec in SOURCES.items():
        if spec.key_env:
            assert spec.tier != 0, f"{name} declares {spec.key_env} but is tier 0"


def test_a_declared_key_gates_availability_whatever_the_tier(monkeypatch):
    from mininfer.ingest import SOURCES

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert SOURCES["deepseek"].available is False, (
        "a source with a key_env must be unavailable without it, tier notwithstanding")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    assert SOURCES["deepseek"].available is True


# --------------------------------------------------------------------------- #
# the default sweep
# --------------------------------------------------------------------------- #


def test_the_default_sweep_widens_when_a_provider_key_is_configured(monkeypatch):
    """Bringing a key must widen the *catalogue*, not only enable the *call*.

    `_ingest_all` filtered its default on `tier == 0`, so a user who exported
    GROQ_API_KEY still got none of Groq's models after a refresh: the key made a
    call possible, but the model was never ingested, so the router could never
    rank it. The default now follows `SourceSpec.available`, which is already
    key-aware.
    """
    from mininfer import cli

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert "groq" not in cli._default_source_names()

    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert "groq" in cli._default_source_names()


def test_the_default_sweep_leaves_out_local_runtimes():
    """A scheduled refresh must not ping `localhost` for a runtime that is down.

    Naming one explicitly (`mi ingest ollama`) is the operator saying it is up.
    """
    from mininfer import cli

    assert {"ollama", "lmstudio", "vllm"}.isdisjoint(cli._default_source_names())


# --------------------------------------------------------------------------- #
# a command that ingested nothing must not report success
# --------------------------------------------------------------------------- #


class _Store:
    def counts(self):
        return {"weights": 0, "deployments": 0, "evidence": 0, "snapshots": 0}

    def close(self):
        pass


def _ingest_args():
    import argparse

    return argparse.Namespace(sources=[], force=False, no_endpoints=True,
                              endpoints_top=0, limit=0)


def test_ingest_fails_when_every_source_failed(monkeypatch):
    """The bug: `mi ingest` exited 0 after all sources failed.

    The container's first-run bootstrap reads that exit code, so it announced
    "Catalogue populated" over an empty registry — a blank dashboard and a log
    line saying it worked, which is worse than an error.
    """
    from mininfer import cli

    monkeypatch.setattr(cli, "_open", lambda _a: _Store())
    monkeypatch.setattr(cli, "_ingest_all",
                        lambda _s, _a: {"sources": 8, "ok": 0, "failed": 8, "skipped": 0})

    assert cli.cmd_ingest(_ingest_args()) == 1


def test_ingest_succeeds_when_at_least_one_source_worked(monkeypatch):
    """Partial failure is normal — one provider down is not a broken run."""
    from mininfer import cli

    monkeypatch.setattr(cli, "_open", lambda _a: _Store())
    monkeypatch.setattr(cli, "_ingest_all",
                        lambda _s, _a: {"sources": 8, "ok": 1, "failed": 7, "skipped": 0})

    assert cli.cmd_ingest(_ingest_args()) == 0


def test_ingest_succeeds_when_every_source_was_skipped(monkeypatch):
    """Nothing available is not a failure: tier-0 is keyless, but a run can
    legitimately have no work to do."""
    from mininfer import cli

    monkeypatch.setattr(cli, "_open", lambda _a: _Store())
    monkeypatch.setattr(cli, "_ingest_all",
                        lambda _s, _a: {"sources": 3, "ok": 0, "failed": 0, "skipped": 3})

    assert cli.cmd_ingest(_ingest_args()) == 0
