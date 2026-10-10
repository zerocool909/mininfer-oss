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


def test_a_mount_point_falls_back_to_an_in_place_write(tmp_path, monkeypatch):
    """A bind-mounted `.env` is a mount point: Linux refuses to rename over it
    (`EBUSY`), so the atomic swap cannot be used. The write still has to land, or
    "To server" is dead in the container."""
    f = tmp_path / ".env"
    f.write_text("GROQ_API_KEY=\n", encoding="utf-8")

    def busy(src, dst):
        raise OSError(16, "Device or resource busy")
    monkeypatch.setattr(env.os, "replace", busy)

    assert env.set_env_var(f, "GROQ_API_KEY", "gsk_1") == "written"
    assert "GROQ_API_KEY=gsk_1" in f.read_text(encoding="utf-8")
    assert not (tmp_path / ".env.tmp").exists()


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


# --------------------------------------------------- listing what is configured


def test_listing_keys_reports_presence_and_never_the_value(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    body = _client(tmp_path, monkeypatch).get("/v1/keys").json()
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["groq"]["env_var"] == "GROQ_API_KEY"
    assert by_id["groq"]["configured"] is False
    assert "api_key" not in by_id["groq"]
    assert by_id["ollama"]["is_local"] is True


def test_a_stored_key_shows_as_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setattr(proxy, "ENV_PATH", tmp_path / ".env")

    async def accepted(provider, key, key_source="none"):
        return {"ok": True, "reply": "ok", "error_class": None, "error_detail": None}
    monkeypatch.setattr(proxy, "_probe_provider", accepted)

    client = _client(tmp_path, monkeypatch)
    client.post("/v1/keys", json={"provider": "groq", "api_key": "gsk_x"})
    by_id = {p["id"]: p for p in client.get("/v1/keys").json()["providers"]}
    assert by_id["groq"]["configured"] is True


# ------------------------------------- `.env` as a directory (bind-mount footgun)


def test_set_env_var_replaces_an_empty_directory(tmp_path):
    """An empty directory is the shape Docker creates for a missing bind source.

    It cannot be read as a file, so the old code raised `IsADirectoryError` and
    took the whole server down. It is ours to remove, so the write self-heals.
    """
    target = tmp_path / ".env"
    target.mkdir()
    assert env.set_env_var(target, "GROQ_API_KEY", "gsk_new") == "written"
    assert target.is_file()
    assert "GROQ_API_KEY=gsk_new" in target.read_text(encoding="utf-8")


def test_set_env_var_refuses_a_non_empty_directory(tmp_path):
    """A directory with content is the operator's; never delete it silently."""
    target = tmp_path / ".env"
    target.mkdir()
    (target / "keep").write_text("x")
    with pytest.raises(IsADirectoryError):
        env.set_env_var(target, "GROQ_API_KEY", "gsk_new")
    assert (target / "keep").exists()


# ----------------------------------------- search providers are keys too
# TinyFish (and Tavily) are not model providers, but they take a key and the UI
# should be able to configure them exactly like one — same verified-persist flow.


def test_search_providers_are_listed_with_a_kind(tmp_path, monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    body = _client(tmp_path, monkeypatch).get("/v1/keys").json()
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["tinyfish"]["env_var"] == "TINYFISH_API_KEY"
    assert by_id["tinyfish"]["kind"] == "search"
    assert by_id["tinyfish"]["configured"] is False


def test_search_providers_appear_in_the_provider_list(tmp_path, monkeypatch):
    body = _client(tmp_path, monkeypatch).get("/v1/providers").json()
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["tinyfish"]["kind"] == "search"
    assert by_id["tinyfish"]["models"] == []
    assert by_id["tinyfish"]["key_env"] == "TINYFISH_API_KEY"


def test_a_verified_search_key_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "placeholder")  # teardown restores it
    monkeypatch.setattr(proxy, "ENV_PATH", tmp_path / ".env")

    def accepted(provider, key, **kw):
        return True, "TinyFish key accepted"
    monkeypatch.setattr(proxy.search_mod, "verify_key", accepted)

    r = _client(tmp_path, monkeypatch).post(
        "/v1/keys", json={"provider": "tinyfish", "api_key": "tf-abc"})
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] is True and body["env_var"] == "TINYFISH_API_KEY"
    assert "tf-abc" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "tf-abc" not in r.text
    assert os.environ["TINYFISH_API_KEY"] == "tf-abc"   # live without a restart


def test_an_unverified_search_key_is_never_written(tmp_path, monkeypatch):
    target = tmp_path / ".env"
    monkeypatch.setattr(proxy, "ENV_PATH", target)

    def rejected(provider, key, **kw):
        return False, "TinyFish rejected the key (401)"
    monkeypatch.setattr(proxy.search_mod, "verify_key", rejected)

    r = _client(tmp_path, monkeypatch).post(
        "/v1/keys", json={"provider": "tinyfish", "api_key": "bad"})
    assert r.status_code == 400
    assert r.json()["stored"] is False
    assert r.json()["error"]["message"] == "TinyFish rejected the key (401)"
    assert not target.exists()


def test_search_verify_key_refuses_an_unknown_provider_without_network():
    from mininfer import search as search_mod
    ok, detail = search_mod.verify_key("nope", "x")
    assert ok is False and "not a search provider" in detail


# ------------------------------------------- testing a search provider's key


def test_testing_a_search_provider_verifies_the_search_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "tf-x")

    def accepted(provider, key, **kw):
        assert provider == "tinyfish" and key == "tf-x"
        return True, "TinyFish key accepted"
    monkeypatch.setattr(proxy.search_mod, "verify_key", accepted)

    r = _client(tmp_path, monkeypatch).post(
        "/v1/providers/test", json={"provider": "tinyfish"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["key_source"] == "env"
    assert body["model"] == "web search"
    assert body["is_free"] is True


def test_testing_a_search_provider_without_a_key_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    r = _client(tmp_path, monkeypatch).post(
        "/v1/providers/test", json={"provider": "tinyfish"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error_class"] == "no_api_key"
    assert body["key_source"] == "none"
    assert "TINYFISH_API_KEY" in body["error_detail"]


def test_testing_a_search_provider_reports_a_rejected_key(tmp_path, monkeypatch):
    def rejected(provider, key, **kw):
        return False, "TinyFish rejected the key (401)"
    monkeypatch.setattr(proxy.search_mod, "verify_key", rejected)
    r = _client(tmp_path, monkeypatch).post(
        "/v1/providers/test", json={"provider": "tinyfish", "api_key": "bad"})
    body = r.json()
    assert body["ok"] is False and body["key_source"] == "custom"
    assert body["error_class"] == "request_failed"
    assert "rejected" in body["error_detail"]
