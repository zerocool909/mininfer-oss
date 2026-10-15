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
from mininfer.schema import Deployment, Evidence, Weights
from mininfer.store import Store


def _weights_evidence(alias_id: str, field: str, value, source: str = "openrouter") -> Evidence:
    return Evidence("weights", alias_id, field, value, source,
                    "https://openrouter.ai/api/v1/models",
                    "2026-10-03T20:31:20+00:00", 0.85, "aggregator_api")


def test_merge_preserves_the_absorbed_rows_facts(tmp_path):
    """A merge must not lose the alias's facts.

    `merge_weights` repoints deployments, records an alias, and deletes the
    weights row. Before this guard it deleted the row's `modalities` and
    `benchmark` with it, and `unmerge_variant` cannot bring them back — it
    recreates the row from three columns and a *deployment's* display name.
    """
    s = Store(tmp_path / "m.db")
    s.upsert_weights(Weights("slug:nano-banana-pro-(gemini-3-pro-image)",
                             "Nano Banana Pro", modalities=("image", "text"),
                             benchmark={"design_arena_elo": 1248.8},
                             benchmark_source="openrouter"))
    s.upsert_weights(Weights("hf:google/gemini-3-pro-image", "gemini-3-pro-image"))
    s.upsert_deployment(Deployment("openrouter:google/gemini-3-pro-image",
                                   "slug:nano-banana-pro-(gemini-3-pro-image)",
                                   "openrouter", "google/gemini-3-pro-image"))
    s.commit()

    assert s.merge_weights("slug:nano-banana-pro-(gemini-3-pro-image)",
                           "hf:google/gemini-3-pro-image", "same model", 1.0) == 1
    s.commit()

    canon = s.conn.execute(
        "SELECT modalities, benchmark, benchmark_source FROM weights"
        " WHERE weights_id='hf:google/gemini-3-pro-image'").fetchone()
    assert "image" in canon["modalities"], "the alias's modalities were discarded"
    assert "design_arena_elo" in canon["benchmark"], "the alias's benchmark was discarded"
    assert canon["benchmark_source"] == "openrouter"
    s.close()


def test_merge_never_overwrites_a_value_the_canonical_already_has(tmp_path):
    """Preserving a gap is not the same as picking a winner.

    The fill is a *preservation* rule: the canonical's own value always wins,
    and the alias only fills what is genuinely empty. Choosing between two
    present values is the resolver's job, not the merge's.
    """
    s = Store(tmp_path / "m2.db")
    s.upsert_weights(Weights("slug:a", "A", modalities=("audio",)))
    s.upsert_weights(Weights("hf:x/a", "A", modalities=("image", "text")))
    s.commit()

    s.merge_weights("slug:a", "hf:x/a", "same model", 1.0)
    s.commit()
    got = s.conn.execute(
        "SELECT modalities FROM weights WHERE weights_id='hf:x/a'").fetchone()["modalities"]
    assert "image" in got and "audio" not in got
    s.close()


def test_merge_repoints_the_absorbed_rows_evidence(tmp_path):
    """Weights-scoped evidence must follow the id, or it is orphaned.

    2,058 rows in the live registry key `entity_id` on a `weights_id`, so a
    merge that does not repoint them points them at a row that no longer exists.
    """
    s = Store(tmp_path / "m3.db")
    s.upsert_weights(Weights("slug:b", "B"))
    s.upsert_weights(Weights("hf:x/b", "B"))
    s.upsert_weights(Weights("hf:x/other", "Other"),
                     [_weights_evidence("hf:x/other", "display_name", "Other")])
    # The alias carries evidence of its own, plus an unrelated row that must not move.
    s.upsert_weights(Weights("slug:b", "B"),
                     [_weights_evidence("slug:b", "benchmark", {"mmlu": 80.0})])
    s.commit()

    s.merge_weights("slug:b", "hf:x/b", "same model", 1.0)
    s.commit()

    moved = s.conn.execute(
        "SELECT COUNT(*) c FROM evidence WHERE entity_kind='weights'"
        " AND entity_id='hf:x/b'").fetchone()["c"]
    orphaned = s.conn.execute(
        "SELECT COUNT(*) c FROM evidence WHERE entity_kind='weights'"
        " AND entity_id='slug:b'").fetchone()["c"]
    untouched = s.conn.execute(
        "SELECT COUNT(*) c FROM evidence WHERE entity_kind='weights'"
        " AND entity_id='hf:x/other'").fetchone()["c"]
    assert moved == 1, "the alias's evidence did not follow the id"
    assert orphaned == 0, "evidence left pointing at a deleted weights row"
    assert untouched == 1, "an unrelated row's evidence was moved"
    s.close()


def test_propose_dry_run_reports_but_writes_nothing(tmp_path):
    """`propose` merges by deleting the absorbed row, so dry-run must not commit.

    `mi resolve --dry-run` passed the flag only to `--split-variants` and
    `--adjudicate`; the auto-merge half ran and committed anyway, which made the
    flag a lie on the one pass that deletes data.
    """
    s = Store(tmp_path / "dr.db")
    s.upsert_weights(Weights("hf:qwen/qwen3-30b", "qwen3-30b"))
    s.upsert_weights(Weights("slug:qwen3-30b", "Qwen3 30B"))
    s.commit()

    merged, _ = propose(s, auto=True, dry_run=True)
    assert len(merged) == 1, "dry-run should still report what it would merge"
    assert s.weights_exists("slug:qwen3-30b"), "dry-run deleted a row"
    assert not s.merged_ok("slug:qwen3-30b"), "dry-run recorded an alias"

    # The real pass still does the work.
    merged, _ = propose(s, auto=True)
    s.commit()
    assert len(merged) == 1
    assert not s.weights_exists("slug:qwen3-30b")
    s.close()


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
