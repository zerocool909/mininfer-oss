"""The API contract, kept honest.

`docs/api-contract.md` lists every endpoint and its stability. A document like that
rots the moment it is written by hand and never checked — so the inventory block
is parsed here and compared with the application's real routes. Adding an endpoint
without documenting it fails the build.

It also asserts the OpenAPI schema covers every route, because that is the schema
consumers and SDK generators actually read.
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "docs" / "api-contract.md"

#: `GET    /v1/models    stable`
_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(\S+)\s+(stable|experimental|internal)$")


def _documented() -> dict[str, str]:
    """`{"GET /v1/models": "stable"}` from the fenced inventory block."""
    text = CONTRACT.read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(.*?)```", text, re.DOTALL)
    assert blocks, "the contract has no ```text inventory block"
    documented: dict[str, str] = {}
    for block in blocks:
        for raw in block.splitlines():
            line = raw.strip()
            if not line:
                continue
            match = _LINE.match(line)
            assert match, f"unparseable inventory line: {line!r}"
            documented[f"{match.group(1)} {match.group(2)}"] = match.group(3)
    return documented


def _actual() -> set[str]:
    """Every routable `METHOD /path` the app exposes, excluding static mounts."""
    from mininfer.proxy import app

    routes: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or route.__class__.__name__ == "Mount":
            continue
        for method in getattr(route, "methods", None) or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            routes.add(f"{method} {path}")
    return routes


def test_the_contract_file_exists():
    assert CONTRACT.exists(), "docs/api-contract.md is part of the public contract"


def test_every_route_is_documented():
    missing = sorted(_actual() - set(_documented()))
    assert not missing, (
        f"these routes are not in docs/api-contract.md: {missing}. "
        "Add them in the same commit as the route.")


def test_the_contract_documents_nothing_that_no_longer_exists():
    stale = sorted(set(_documented()) - _actual())
    assert not stale, (
        f"docs/api-contract.md lists routes that do not exist: {stale}. "
        "A stale contract is worse than none.")


def test_every_documented_route_carries_a_stability_label():
    assert set(_documented().values()) <= {"stable", "experimental", "internal"}


def test_the_openapi_schema_covers_every_route():
    """SDK generators read the OpenAPI, so a route missing from it is invisible."""
    from mininfer.proxy import app

    # FastAPI drops the converter suffix in the schema: the route is
    # `/v1/economics/deployments/{deploy_id:path}` and the schema says
    # `{deploy_id}`. Normalise both sides rather than exempt the route, so a route
    # that really is missing from the schema still fails.
    def _plain(path: str) -> str:
        return re.sub(r"\{(\w+):[a-z]+\}", r"{\1}", path)

    documented_paths = {_plain(path) for _, path in
                        (entry.split(" ", 1) for entry in _actual())}
    schema_paths = {_plain(p) for p in app.openapi()["paths"]}

    # FastAPI's own built-ins and the plain Starlette routes are not operations.
    exempt = {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
    missing = sorted(documented_paths - schema_paths - exempt)
    assert not missing, f"routes absent from the OpenAPI schema: {missing}"


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/models", "/healthz"])
def test_the_core_surface_is_marked_stable(path):
    """The three things an OpenAI SDK needs must never be experimental."""
    documented = _documented()
    entries = [status for key, status in documented.items() if key.endswith(f" {path}")]
    assert entries == ["stable"], entries
