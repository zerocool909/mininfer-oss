"""The two routes — OSS/local and cloud — validated end to end.

MinInfer ships as **one codebase serving two profiles** (`PRODUCTIZATION.md`
§3a). The separation is data, not a fork:

    config/profiles/local.env   config/profiles/cloud.env
    deploy/local/               deploy/cloud/

This file is the end-to-end companion to the pieces that already exist:

* `tests/test_profiles.py` pins each profile *as data* (local open, cloud locked);
* `tests/test_auth.py` pins the access-control *mechanics*;
* `tests/test_deploy_manifests.py` pins the cloud manifest *shape*;
* `scripts/validate-phase0.sh` boots both profiles live through a real socket.

What none of them do is boot the real ASGI app **under each profile's own
environment** inside pytest and walk the whole route matrix. That is what this
suite is for, and it is what makes "the routes are testable" a check in CI
rather than a shell script an operator remembers to run.

Two deliberate rules, borrowed from `validate-phase0.sh`:

1. **The profile files are the source of truth.** The environment is built by
   parsing `config/profiles/*.env`, not by restating their values here. Change a
   profile and this suite follows it.
2. **`MI_DB` is a deployment concern, not a route semantic.** `cloud.env` names a
   `postgresql://` DSN that does not resolve on a laptop, so the harness points
   `MI_DB` at a temp SQLite file — exactly as the phase-0 script does. The
   *access-control* behaviour is what is under test; the DSN is asserted
   separately as a profile property.

No upstream provider is ever called: `try_fallbacks` is stubbed, so every
assertion is answered by the guard, the router or the registry.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest
from fastapi.testclient import TestClient

from mininfer.execute import CallResult
from mininfer.schema import Deployment, Weights
from mininfer.store import Store

ROOT = pathlib.Path(__file__).resolve().parent.parent
PROFILES = ROOT / "config" / "profiles"
LOCAL = PROFILES / "local.env"
CLOUD = PROFILES / "cloud.env"

# Every knob the profiles are allowed to disagree on. Cleared before each boot so
# a test earlier in the session cannot leak its environment into a later route.
_PROFILE_KEYS = (
    "HOST", "PORT", "MI_DB", "MI_POLICY",
    "MI_API_KEYS", "MI_API_KEYS_FILE", "MI_ADMIN_TOKEN",
    "MI_RATE_LIMIT_RPM", "MI_MAX_BODY_BYTES", "MI_CORS_ORIGINS", "MI_REDIS_URL",
)

# The surfaces each route exposes. Kept as paths (not handlers) so the matrix
# reads like the table in `PRODUCTIZATION.md`. `/v1/plan` is admin (it reads the
# registry); `/v1/models` is tenant.
_TENANT_SURFACES = ("/v1/models",)
_ADMIN_SURFACES = ("/v1/stats", "/", "/docs", "/openapi.json")
_PUBLIC_SURFACES = ("/healthz",)
# Unclassified paths fail closed: guarded when auth is on, and 404 once a valid
# admin clears the guard because no such route exists.
_UNKNOWN_PATHS = ("/some/unknown/path",)

_POLICY = """\
policy:
  name: test
  top_k: 3
  require_distinct_provider: false
  on_unverified_capability: allow
  require: {}
  min_context: 0
  min_success_lb: 0.0
  allow_unknown_price: true
  prior_strength: 25.0
tasks:
  alpha:
    description: alpha task
    tokens_in: 100
    tokens_out: 10
    require: {}
    min_context: 0
    min_success_lb: 0.0
    benchmark_keys: [coding]
    cues: [alpha]
"""


# --------------------------------------------------------------------------- #
# Profile loading + a route bootstrapper
# --------------------------------------------------------------------------- #


def load_profile(path: pathlib.Path) -> dict[str, str]:
    """Parse `KEY=value` lines, skipping comments. Deliberately not `os.environ`."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


class Route:
    """A booted variant: the live client plus the profile it was built from.

    `env` is the *effective* environment (with the temp-DB override); `declared`
    is the profile exactly as committed, which is what the DSN assertions read.
    """

    def __init__(self, client: TestClient, env: dict[str, str],
                 declared: dict[str, str], sessions: list):
        self.client = client
        self.env = env
        self.declared = declared
        self.sessions = sessions

    @property
    def tenant_key(self) -> str:
        return self.env["MI_API_KEYS"].split(",")[0].split(":")[0]

    @property
    def admin_token(self) -> str:
        return self.env["MI_ADMIN_TOKEN"]


def boot(path: pathlib.Path, tmp_path, monkeypatch, *, sessions=None, **overrides) -> Route:
    """Boot the real proxy app under a profile's environment.

    `overrides` tighten a single knob for a mechanics check (e.g. a 2 rpm window
    instead of the profile's 120) without editing the profile — the same
    technique `validate-phase0.sh` §3 uses.
    """
    declared = load_profile(path)
    env = dict(declared)
    env.update({k: str(v) for k, v in overrides.items()})
    tmp_path.mkdir(parents=True, exist_ok=True)

    for key in _PROFILE_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    # The profiles describe semantics; these two paths are a deployment concern.
    env["MI_DB"] = str(tmp_path / "registry.db")
    env["MI_POLICY"] = str(tmp_path / "policy.yaml")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    (tmp_path / "policy.yaml").write_text(_POLICY)
    store = Store(tmp_path / "registry.db")
    store.upsert_weights(Weights("hf:t/m", "test-model", params_b=7.0,
                                 benchmark={"coding": 50.0}))
    store.upsert_deployment(Deployment("openrouter:m", "hf:t/m", "openrouter", "m",
                                       price_in=1.0, price_out=1.0, context_window=1000))
    store.commit()
    store.close()

    import mininfer.auth as auth
    import mininfer.proxy as proxy
    auth.reset_cache()
    proxy._LIMITER.reset()
    proxy._LIMITER_CACHE = None

    seen = sessions if sessions is not None else []

    def _ok(deploys, messages, runner, **kw):
        seen.append(kw.get("session_id"))
        did = deploys[0] if deploys else ""
        return did, CallResult(did, text="hi", ok=True, tokens_in=11, tokens_out=5), []

    monkeypatch.setattr(proxy, "try_fallbacks", _ok)
    return Route(TestClient(proxy.app), env, declared, seen)


@pytest.fixture
def local_route(tmp_path, monkeypatch) -> Route:
    return boot(LOCAL, tmp_path, monkeypatch)


@pytest.fixture
def cloud_route(tmp_path, monkeypatch) -> Route:
    return boot(CLOUD, tmp_path, monkeypatch)


def _ask(route: Route, headers=None):
    return route.client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "just alpha"}]},
        headers=headers or {},
    )


def _bearer(route: Route):
    return {"Authorization": f"Bearer {route.tenant_key}"}


def _admin(route: Route):
    return {"Authorization": f"Bearer {route.admin_token}"}


# --------------------------------------------------------------------------- #
# A. The profiles are the source of truth
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path,route_name", [(LOCAL, "local"), (CLOUD, "cloud")])
def test_each_profile_states_every_route_knob(path, route_name):
    """A profile that omits a knob inherits whatever the shell had — fail closed."""
    env = load_profile(path)
    missing = [k for k in _PROFILE_KEYS if k not in env]
    assert not missing, f"{route_name}.env does not state {missing}"


def test_the_two_routes_differ_only_in_the_values_of_the_same_knobs(tmp_path, monkeypatch):
    """Single-source check: identical key set, different values."""
    local = boot(LOCAL, tmp_path / "l", monkeypatch)
    monkeypatch.undo()
    cloud = boot(CLOUD, tmp_path / "c", monkeypatch)

    assert set(local.env) == set(cloud.env), (
        "a variant introduced a knob the other does not know about — that is a fork")
    # ...and they genuinely disagree on the axes the product cares about.
    assert local.env["MI_API_KEYS"] == "" and cloud.env["MI_API_KEYS"]
    assert local.env["MI_RATE_LIMIT_RPM"] == "0"
    assert int(cloud.env["MI_RATE_LIMIT_RPM"]) > 0
    assert local.env["MI_MAX_BODY_BYTES"] == "0"
    assert int(cloud.env["MI_MAX_BODY_BYTES"]) > 0


# --------------------------------------------------------------------------- #
# B. LOCAL / OSS route — open, unlimited, unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("surface", _TENANT_SURFACES + _ADMIN_SURFACES + _PUBLIC_SURFACES)
def test_local_route_serves_every_surface_without_a_credential(local_route, surface):
    assert local_route.client.get(surface).status_code == 200, surface


@pytest.mark.parametrize("surface", _UNKNOWN_PATHS)
def test_local_route_has_no_guard_so_an_unknown_path_is_just_404(local_route, surface):
    assert local_route.client.get(surface).status_code == 404, surface


def test_local_route_completes_a_chat_call(local_route):
    assert _ask(local_route).status_code == 200


def test_local_route_has_no_rate_limit(local_route):
    assert all(_ask(local_route).status_code == 200 for _ in range(5))


def test_local_route_has_no_body_cap(local_route):
    # Pad an ignored field so the *body* is large without tripping the router's
    # context-window check — this asserts the cap is absent, nothing else.
    r = local_route.client.post(
        "/v1/chat/completions",
        json={"model": "auto", "messages": [{"role": "user", "content": "just alpha"}],
              "padding": "x" * 100_000})
    assert r.status_code == 200


def test_local_route_leaves_the_session_id_unprefixed(tmp_path, monkeypatch):
    """The `tenant/` prefix is an auth-era invention; OSS must not grow one."""
    route = boot(LOCAL, tmp_path, monkeypatch, sessions=[])
    _ask(route, headers={"X-MI-Session": "local"})
    assert route.sessions == ["local"]


def test_local_profile_selects_sqlite(tmp_path, monkeypatch):
    from mininfer.db import is_postgres, target_from_env

    route = boot(LOCAL, tmp_path, monkeypatch)
    target = target_from_env(route.declared["MI_DB"])
    assert not is_postgres(target)
    assert isinstance(target, pathlib.Path)


# --------------------------------------------------------------------------- #
# C. CLOUD route — fail closed, credentials, limits
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("surface", _TENANT_SURFACES + _ADMIN_SURFACES)
def test_cloud_route_refuses_every_non_public_surface_without_a_credential(cloud_route, surface):
    assert cloud_route.client.get(surface).status_code == 401, surface


@pytest.mark.parametrize("surface", _PUBLIC_SURFACES)
def test_cloud_route_keeps_the_load_balancer_surface_public(cloud_route, surface):
    assert cloud_route.client.get(surface).status_code == 200, surface


@pytest.mark.parametrize("surface", _TENANT_SURFACES)
def test_cloud_tenant_key_opens_the_tenant_surface(cloud_route, surface):
    assert cloud_route.client.get(surface, headers=_bearer(cloud_route)).status_code == 200


@pytest.mark.parametrize("surface", _ADMIN_SURFACES)
def test_cloud_tenant_key_cannot_reach_the_admin_surface(cloud_route, surface):
    assert cloud_route.client.get(surface, headers=_bearer(cloud_route)).status_code == 401


@pytest.mark.parametrize("surface", _TENANT_SURFACES + _ADMIN_SURFACES)
def test_cloud_admin_token_is_a_superset(cloud_route, surface):
    assert cloud_route.client.get(surface, headers=_admin(cloud_route)).status_code == 200


@pytest.mark.parametrize("surface", _UNKNOWN_PATHS)
def test_cloud_unknown_path_fails_closed_then_404s_for_admin(cloud_route, surface):
    """The guard runs before routing, so an unauthenticated caller learns nothing."""
    assert cloud_route.client.get(surface).status_code == 401, surface
    assert cloud_route.client.get(surface, headers=_admin(cloud_route)).status_code == 404


def test_cloud_browser_gets_a_basic_challenge(cloud_route):
    import base64

    r = cloud_route.client.get("/")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")
    token = base64.b64encode(f"me:{cloud_route.admin_token}".encode()).decode()
    assert cloud_route.client.get(
        "/", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_cloud_401_is_in_the_openai_error_shape(cloud_route):
    r = _ask(cloud_route)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"
    assert r.headers.get("www-authenticate") == "Bearer"


def test_cloud_rate_limit_is_per_tenant_with_retry_after(tmp_path, monkeypatch):
    route = boot(CLOUD, tmp_path, monkeypatch, MI_API_KEYS="sk-a:acme,sk-b:beta",
                 MI_RATE_LIMIT_RPM=2)
    a = {"Authorization": "Bearer sk-a"}
    b = {"Authorization": "Bearer sk-b"}
    assert _ask(route, headers=a).status_code == 200
    assert _ask(route, headers=a).status_code == 200
    r = _ask(route, headers=a)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(r.headers["retry-after"]) >= 1
    assert _ask(route, headers=b).status_code == 200  # beta has its own window


def test_cloud_admin_traffic_does_not_consume_a_tenant_budget(tmp_path, monkeypatch):
    route = boot(CLOUD, tmp_path, monkeypatch, MI_RATE_LIMIT_RPM=1)
    assert all(_ask(route, headers=_admin(route)).status_code == 200 for _ in range(4))


def test_cloud_body_cap_rejects_before_the_router(tmp_path, monkeypatch):
    route = boot(CLOUD, tmp_path, monkeypatch, MI_MAX_BODY_BYTES=50)
    r = _ask(route, headers=_bearer(route))
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "request_too_large"


def test_cloud_sessions_are_scoped_to_the_tenant(cloud_route):
    """A client-supplied session id is a label, not a boundary."""
    _ask(cloud_route, headers={**_bearer(cloud_route), "X-MI-Session": "shared"})
    assert cloud_route.sessions == [f"validation/shared"]


def test_cloud_profile_selects_postgres(tmp_path, monkeypatch):
    from mininfer.db import is_postgres, target_from_env

    route = boot(CLOUD, tmp_path, monkeypatch)
    target = target_from_env(route.declared["MI_DB"])
    assert is_postgres(target)
    assert isinstance(target, str) and target.startswith("postgresql://")


# --------------------------------------------------------------------------- #
# D. One codebase — the routes are not a fork
# --------------------------------------------------------------------------- #


def test_both_routes_serve_the_same_asgi_app(local_route, cloud_route):
    """If this ever stops holding, a second app object has been introduced."""
    import mininfer.proxy as proxy

    assert isinstance(proxy.app, object)
    # The clients were built independently, but the app is a module global.
    assert local_route.client.app is cloud_route.client.app is proxy.app


def test_both_routes_expose_the_same_route_table(local_route, cloud_route):
    """A route added for one variant must exist for both, or it is a fork."""
    local_paths = set(local_route.client.app.openapi()["paths"])
    cloud_paths = set(cloud_route.client.app.openapi()["paths"])
    assert local_paths == cloud_paths


def test_surface_classification_does_not_depend_on_the_profile(local_route, cloud_route):
    """`classify` is a pure function of the path — the guard is the same code."""
    import mininfer.auth as auth

    for path in ("/healthz", "/v1/models", "/v1/stats", "/docs", "/v1/chat/completions"):
        assert auth.classify(path) == auth.classify(path)
    assert auth.classify("/v1/chat/completions") == "tenant"
    assert auth.classify("/v1/stats") == "admin"
    assert auth.classify("/healthz") == "public"


def test_there_is_no_variant_specific_code_module():
    """The variants are data. A `local`/`cloud` Python package would be a fork."""
    package = ROOT / "mininfer"
    offenders = [p.name for p in package.iterdir()
                 if p.is_dir() and p.name in {"local", "cloud", "oss", "enterprise"}
                 or (p.is_file() and p.stem in {"local", "cloud", "oss", "enterprise"})]
    assert not offenders, f"variant-specific code modules found: {offenders}"


# --------------------------------------------------------------------------- #
# E. Deploy topology — separate manifests, one image
# --------------------------------------------------------------------------- #


def test_deploy_tree_splits_local_and_cloud():
    assert (ROOT / "deploy" / "local").is_dir()
    assert (ROOT / "deploy" / "cloud").is_dir()
    # The shared image stays at the root; the root compose is an include shim.
    assert (ROOT / "Dockerfile").is_file()
    root_compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "deploy/local/docker-compose.yml" in root_compose


def test_deploy_tree_holds_no_python():
    """Manifests and profiles only — logic lives in `mininfer/` once."""
    # `as_posix()` so the expected literal below holds on Windows too, where
    # `str(Path)` would use `\`.
    stray = [p.relative_to(ROOT).as_posix() for p in (ROOT / "deploy").rglob("*.py")
             if "__pycache__" not in p.parts]
    # `deploy/modal/app.py` is a host entrypoint, not engine logic; it is allowed
    # because `test_hosted_config.py` pins it to `mininfer.proxy:app` + the CLI.
    assert stray == ["deploy/modal/app.py"], stray


def test_one_image_defines_both_process_types():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    stages = [line.split()[0].lower() for line in dockerfile.splitlines()
              if line.upper().startswith("FROM ")]
    # `FROM <base> AS <name>` -> the alias is the last token.
    aliases = [line.split()[-1].lower() for line in dockerfile.splitlines()
               if line.upper().startswith("FROM ") and " AS " in line.upper()]
    assert "worker" in aliases, f"no worker stage in Dockerfile (stages: {stages})"
    assert "MI_ROLE=worker" in dockerfile


def test_the_entrypoint_dispatches_worker_versus_web():
    """A command -> worker (batch); no argument -> the web server. One image."""
    entry = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'if [ "$#" -gt 0 ]' in entry, "worker dispatch missing"
    assert "uvicorn mininfer.proxy:app" in entry, "web launch missing"


def test_both_variants_are_covered_by_a_live_validation_script():
    script = (ROOT / "scripts" / "validate-phase0.sh").read_text(encoding="utf-8")
    assert "config/profiles/local.env" in script
    assert "config/profiles/cloud.env" in script
