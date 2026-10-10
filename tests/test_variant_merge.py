"""Hidden-variant merges: `turbo`/`thinking` differences must not be merged away.

`normalize` strips `turbo`, which made `whisper-large-v3` and
`whisper-large-v3-turbo` (a smaller model) normalize identically — and the
auto-merge joined them silently. These pin the guard that stops it and the
recovery that undoes it.
"""
from __future__ import annotations

from mininfer import fetch
from mininfer.normalize import normalize, normalize_identity
from mininfer.resolve import propose
from mininfer.schema import Deployment, Weights
from mininfer.store import Store


def test_normalize_stays_but_identity_keeps_the_variant():
    assert normalize("whisper-large-v3-turbo") == "whisper-large-v3"
    assert normalize_identity("whisper-large-v3-turbo") == "whisper-large-v3-turbo"
    assert normalize_identity("whisper-large-v3") == "whisper-large-v3"
    # Quantization is still serving noise, not identity.
    assert normalize_identity("gemma-4-31b-fp8") == "gemma-4-31b"


def test_a_variant_pair_is_proposed_not_auto_merged(tmp_path):
    s = Store(tmp_path / "v.db")
    s.upsert_weights(Weights("hf:openai/whisper-large-v3", "openai/whisper-large-v3"))
    s.upsert_weights(Weights("hf:openai/whisper-large-v3-turbo",
                             "openai/whisper-large-v3-turbo"))
    s.commit()
    merged, review = propose(s, auto=True)
    s.close()
    assert merged == []
    assert any("variant token" in p.reason for p in review)

    # A genuinely-identical pair (provider prefix only) still auto-merges.
    s = Store(tmp_path / "v2.db")
    s.upsert_weights(Weights("hf:qwen/qwen3-30b", "qwen3-30b"))
    s.upsert_weights(Weights("slug:qwen3-30b", "Qwen3 30B"))
    s.commit()
    merged, _ = propose(s, auto=True)
    s.close()
    assert len(merged) == 1


def test_unmerge_variant_splits_the_absorbed_model(tmp_path):
    s = Store(tmp_path / "u.db")
    s.upsert_weights(Weights("hf:openai/whisper-large-v3-turbo",
                             "openai/whisper-large-v3-turbo"))
    s.upsert_deployment(Deployment("groq:whisper-large-v3-turbo",
                                   "hf:openai/whisper-large-v3-turbo",
                                   "groq", "whisper-large-v3-turbo", zero_price=True))
    # The base was merged in: a deployment of it sits on the turbo, plus the alias.
    s.upsert_deployment(Deployment("groq:whisper-large-v3",
                                   "hf:openai/whisper-large-v3-turbo",
                                   "groq", "whisper-large-v3", zero_price=True))
    s.conn.execute("INSERT INTO weight_aliases (alias_id, canonical_id, reason, confidence)"
                   " VALUES ('hf:openai/whisper-large-v3',"
                   " 'hf:openai/whisper-large-v3-turbo', 'normalised name identical', 1.0)")
    s.commit()

    cands = s.variant_split_candidates()
    assert [c["canonical_id"] for c in cands] == ["hf:openai/whisper-large-v3-turbo"]

    # dry-run writes nothing
    s.unmerge_variant("hf:openai/whisper-large-v3-turbo", dry_run=True)
    assert s.variant_split_candidates(), "dry-run must not change anything"

    rep = s.unmerge_variant("hf:openai/whisper-large-v3-turbo")
    s.commit()
    assert rep["ok"] is True
    base = s.conn.execute(
        "SELECT weights_id FROM deployments WHERE deploy_id='groq:whisper-large-v3'"
    ).fetchone()
    turbo = s.conn.execute(
        "SELECT weights_id FROM deployments WHERE deploy_id='groq:whisper-large-v3-turbo'"
    ).fetchone()
    assert base["weights_id"] == "hf:openai/whisper-large-v3"
    assert turbo["weights_id"] == "hf:openai/whisper-large-v3-turbo"
    assert s.variant_split_candidates() == []      # nothing left to split
    s.close()


def test_fetch_sends_a_compliant_agent_to_wikimedia():
    """The generic UA is exactly what Wikimedia 403s; the host picks the agent."""
    assert fetch._ua_for("https://en.wikipedia.org/wiki/X") == fetch.WIKI_UA
    assert fetch._ua_for("https://example.com/x") == fetch._UA
    assert "mininfer" in fetch.WIKI_UA.lower() and "(" in fetch.WIKI_UA
