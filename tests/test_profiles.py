"""The two deployment variants, pinned as data.

MinInfer ships as one codebase serving two profiles: a local tool and a cloud
service. The failure mode this file exists to prevent is *drift by edit* — a
cloud-hardening change that quietly turns access control on in `local.env`, or a
local convenience that weakens `cloud.env`.

So the profiles are not documentation. They are parsed here, fed to the real
config loader, and asserted against the invariants each variant promises. Both
are loaded through the same `AuthConfig`, which is what makes "the codebase is
single-source" checkable rather than aspirational.
"""
from __future__ import annotations

import pathlib

import pytest

import mininfer.auth as auth

PROFILES = pathlib.Path(__file__).resolve().parent.parent / "config" / "profiles"
LOCAL = PROFILES / "local.env"
CLOUD = PROFILES / "cloud.env"


def load(path: pathlib.Path) -> dict[str, str]:
    """Parse a profile into a mapping. Deliberately not `os.environ`."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


@pytest.mark.parametrize("path", [LOCAL, CLOUD])
def test_both_profiles_exist_and_parse(path):
    assert path.exists(), f"missing profile: {path}"
    env = load(path)
    # The knobs that distinguish the variants must be stated, not defaulted —
    # a profile that omits them would silently inherit whatever the shell had.
    for key in ("HOST", "PORT", "MI_DB", "MI_API_KEYS", "MI_ADMIN_TOKEN",
                "MI_RATE_LIMIT_RPM", "MI_MAX_BODY_BYTES"):
        assert key in env, f"{path.name} does not state {key}"


def test_local_profile_is_open():
    """Local development must stay exactly as it was: no auth, no limits."""
    env = load(LOCAL)
    cfg = auth.load_config(env)
    assert not cfg.enabled, "local.env must not enable access control"
    assert cfg.tenants == {}
    assert cfg.admin_digest is None
    assert cfg.default_rpm == 0
    assert cfg.max_body_bytes == 0
    # ...and the auth module agrees every surface is reachable.
    assert auth.classify("/v1/chat/completions") == "tenant"


def test_cloud_profile_is_locked_down():
    """Cloud must fail closed: credentials present, limits set."""
    env = load(CLOUD)
    cfg = auth.load_config(env)
    assert cfg.enabled, "cloud.env must enable access control"
    assert cfg.tenants, "cloud.env must define at least one tenant key"
    assert cfg.admin_digest, "cloud.env must define an admin token"
    assert cfg.default_rpm > 0, "cloud.env must rate limit"
    assert cfg.max_body_bytes > 0, "cloud.env must cap request bodies"


def test_cloud_profile_holds_no_usable_credential():
    """The committed profile is a template. Shipping it must be a visible mistake.

    Real keys arrive from the secret store as environment variables, which
    override the file — so a placeholder that reads as a placeholder is the
    safe default.
    """
    env = load(CLOUD)
    for key in ("MI_API_KEYS", "MI_ADMIN_TOKEN"):
        assert "do-not-deploy" in env[key], (
            f"{key} looks like a real credential; cloud.env is a template")


def test_a_tenant_key_in_the_cloud_profile_is_not_an_admin():
    """The surface split must survive contact with the profile's own values."""
    cfg = auth.load_config(load(CLOUD))
    tenant_key = load(CLOUD)["MI_API_KEYS"].split(":")[0]
    admin_token = load(CLOUD)["MI_ADMIN_TOKEN"]

    assert cfg.authenticate(tenant_key) is not None
    assert not cfg.is_admin(tenant_key)
    assert cfg.is_admin(admin_token)
    assert cfg.authenticate(admin_token) is None  # admin is not a tenant
