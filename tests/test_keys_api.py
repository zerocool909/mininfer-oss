"""Persisting a provider key to the server's `.env`, and only once it works.

The dashboard keeps keys in the browser and forwards them per request. Writing one
server-side is an opt-in second step, so this pins the rules around it: verified
only, never clobber, seed a missing file from `.env.example`.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from mininfer import env, proxy


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MI_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("MI_POLICY", "config/policy.yaml")
    return TestClient(proxy.app)


# ------------------------------------------------------- the .env writer


def test_a_missing_file_is_created_from_the_template(tmp_path):
    tmpl = tmp_path / ".env.example"
    tmpl.write_text("# template\nOPENROUTER_API_KEY=\n", encoding="utf-8")
    target = tmp_path / ".env"
    assert env.set_env_var(target, "OPENROUTER_API_KEY", "sk-or-abc",
                           template=tmpl) == "created"
    text = target.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-abc" in text
    assert "# template" in text                    # the rest of the template survives


def test_an_existing_value_is_never_clobbered(tmp_path):
    f = tmp_path / ".env"
    f.write_text("OPENROUTER_API_KEY=keep-me\n", encoding="utf-8")
    assert env.set_env_var(f, "OPENROUTER_API_KEY", "typed-in-a-web-form") == "already_set"
    assert f.read_text(encoding="utf-8") == "OPENROUTER_API_KEY=keep-me\n"


def test_an_empty_placeholder_is_filled(tmp_path):
    """The normal `.env.example` -> `.env` case: the line exists with no value."""
    f = tmp_path / ".env"
    f.write_text("# x\nGROQ_API_KEY=\n", encoding="utf-8")
    assert env.set_env_var(f, "GROQ_API_KEY", "gsk_1") == "written"
    assert "GROQ_API_KEY=gsk_1" in f.read_text(encoding="utf-8")


def test_a_commented_placeholder_is_not_a_match(tmp_path):
    """The template ships the key commented out; appending is correct, editing the
    comment is not."""
    f = tmp_path / ".env"
    f.write_text("# GROQ_API_KEY=gsk_old\n", encoding="utf-8")
    assert env.set_env_var(f, "GROQ_API_KEY", "gsk_new") == "written"
    text = f.read_text(encoding="utf-8")
    assert "# GROQ_API_KEY=gsk_old" in text        # the comment is untouched
    assert "GROQ_API_KEY=gsk_new" in text          # and a real line was appended


def test_a_value_that_needs_quoting_is_quoted(tmp_path):
    f = tmp_path / ".env"
    env.set_env_var(f, "ODD", "a b#c")
    assert 'ODD="a b#c"' in f.read_text(encoding="utf-8")


# ------------------------------------------------------- the endpoint


def test_a_verified_key_is_written(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)   # undo the os.environ write
    monkeypatch.setattr(proxy, "ENV_PATH", tmp_path / ".env")
    tmpl = tmp_path / ".env.example"
    tmpl.write_text("OPENROUTER_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(proxy, "ENV_TEMPLATE", tmpl)

    async def accepted(provider, key, key_source="none"):
        return {"ok": True, "reply": "key accepted", "error_class": None, "error_detail": None}
    monkeypatch.setattr(proxy, "_probe_provider", accepted)

    r = _client(tmp_path, monkeypatch).post(
        "/v1/keys", json={"provider": "openrouter", "api_key": "sk-or-xyz"})
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] is True and body["status"] == "created"
    assert body["env_var"] == "OPENROUTER_API_KEY"
    assert "sk-or-xyz" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "sk-or-xyz" not in r.text             # the key is never echoed back
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-xyz"   # live without a restart


def test_a_verified_key_does_not_replace_a_value_already_there(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    target.write_text("GROQ_API_KEY=the-operators-key\n", encoding="utf-8")
    monkeypatch.setattr(proxy, "ENV_PATH", target)

    async def accepted(provider, key, key_source="none"):
        return {"ok": True, "reply": "key accepted", "error_class": None, "error_detail": None}
    monkeypatch.setattr(proxy, "_probe_provider", accepted)

    r = _client(tmp_path, monkeypatch).post(
        "/v1/keys", json={"provider": "groq", "api_key": "gsk_new"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "stored": False, "status": "already_set",
                        "provider": "groq", "env_var": "GROQ_API_KEY",
                        "reply": "key accepted"}
    assert target.read_text(encoding="utf-8") == "GROQ_API_KEY=the-operators-key\n"


def test_an_unverified_key_is_never_written(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    monkeypatch.setattr(proxy, "ENV_PATH", target)

    async def rejected(provider, key, key_source="none"):
        return {"ok": False, "error_class": "auth_error",
                "error_detail": "the provider rejected the key"}
    monkeypatch.setattr(proxy, "_probe_provider", rejected)

    r = _client(tmp_path, monkeypatch).post(
        "/v1/keys", json={"provider": "openrouter", "api_key": "bad"})
    assert r.status_code == 400
    assert r.json()["stored"] is False
    assert r.json()["error"]["message"] == "the provider rejected the key"
    assert not target.exists(), "an unverified key must not create .env"


def test_an_unknown_provider_is_rejected(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).post(
        "/v1/keys", json={"provider": "nope", "api_key": "x"})
    assert r.status_code == 400


def test_a_missing_key_is_rejected(tmp_path, monkeypatch):
    r = _client(tmp_path, monkeypatch).post("/v1/keys", json={"provider": "groq"})
    assert r.status_code == 400
