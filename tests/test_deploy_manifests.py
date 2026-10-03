"""The cloud manifests, pinned as data.

`PRODUCTIZATION.md` §4a moved the registry to Postgres and the limits to Redis;
`CLOUD_ACTIVITY.md` Phase 1.0 finishes that story in the deploy manifests. A
manifest is easy to edit back into the single-writer shape — one `replicas: 1`
or a dropped `MI_REDIS_URL` — and nothing fails until production, so the
invariants each manifest promises are asserted here instead.

These are structural checks, not a substitute for `kubectl apply --dry-run` /
`fly deploy`; they pin the things a reviewer would have to notice by eye.
"""
from __future__ import annotations

import pathlib
import tomllib

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
FLY = ROOT / "deploy" / "cloud" / "fly.toml"
K8S = ROOT / "deploy" / "cloud" / "k8s.yaml"


def _fly() -> dict:
    return tomllib.loads(FLY.read_text(encoding="utf-8"))


def _k8s() -> dict[str, dict]:
    docs = [d for d in yaml.safe_load_all(K8S.read_text(encoding="utf-8")) if d]
    return {f"{d['kind']}/{d['metadata']['name']}": d for d in docs}


def _containers(spec: dict) -> list[dict]:
    return spec["template"]["spec"]["containers"]


def _env_of(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


# --------------------------------------------------------------------------- #
# Kubernetes
# --------------------------------------------------------------------------- #

def test_k8s_deployment_runs_more_than_one_replica():
    dep = _k8s()["Deployment/mininfer"]
    assert dep["spec"]["replicas"] > 1, "the point of Postgres is > 1 replica"
    assert dep["spec"]["strategy"]["type"] == "RollingUpdate", (
        "Recreate was a SQLite/RWO constraint; Postgres lifts it")


def test_k8s_registry_and_limits_come_from_secrets():
    cont = _containers(_k8s()["Deployment/mininfer"]["spec"])[0]
    env = _env_of(cont)
    for name in ("MI_DB", "MI_REDIS_URL"):
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "mininfer-secrets", f"{name} must be a secret"
    # MI_DB must be the DSN, never a SQLite path.
    assert "postgresql://" in _k8s()["Secret/mininfer-secrets"]["stringData"]["database-url"]


def test_k8s_registry_volume_is_not_the_registry():
    """The Deployment must not mount a ReadWriteOnce volume as its registry.

    A shared RWO claim across replicas either fails to attach or silently
    serialises them; with `MI_DB` on Postgres the pod is stateless.
    """
    dep = _k8s()["Deployment/mininfer"]
    volumes = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
    assert "data-volume" not in volumes, "a PVC on every replica is the shape Phase 1 removed"
    assert any("emptyDir" in v for v in volumes.values()), "scratch space for the admin probe"


def test_k8s_mounts_the_optional_ca_bundle():
    """Checklist 7.4: a host-only CA path is invisible inside the pod."""
    for name in ("Deployment/mininfer", "CronJob/mininfer-worker"):
        doc = _k8s()[name]
        cont = (doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
                if name.startswith("CronJob") else _containers(doc["spec"])[0])
        env = _env_of(cont)
        assert env["MI_CA_BUNDLE"]["value"].startswith("/etc/mininfer/ca/")
        mounts = {m["name"]: m for m in cont["volumeMounts"]}
        assert "ca" in mounts, f"{name} sets MI_CA_BUNDLE but does not mount it"


def test_k8s_worker_is_a_batch_job_not_a_service():
    """The registry's batch jobs must not scale with requests."""
    cj = _k8s()["CronJob/mininfer-worker"]
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    cont = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert "mi ingest" in " ".join(cont["args"])


def test_k8s_runs_the_daily_verification_job():
    """A cached ingest cannot verify anything, so this has its own daily job."""
    cj = _k8s()["CronJob/mininfer-verify"]
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    assert cj["spec"]["schedule"] == "0 3 * * *"
    cont = cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    assert "verify" in " ".join(cont["args"])
    assert "--report" in cont["args"]


# --------------------------------------------------------------------------- #
# Fly
# --------------------------------------------------------------------------- #

def test_fly_splits_web_worker_and_verify_and_scopes_http_to_web():
    fly = _fly()
    assert set(fly["processes"]) == {"web", "worker", "verify"}
    assert fly["http_service"]["processes"] == ["web"]
    # The verification cadence is deliberately its own process.
    assert "mi verify" in fly["processes"]["verify"]


def test_fly_keeps_replicas_and_shared_state_possible():
    fly = _fly()
    # MI_DB/MI_REDIS_URL are secrets on Fly (they must not be committed), so the
    # manifest can only promise the *seam*: the env names are documented and the
    # CA bundle has a home on the volume.
    text = FLY.read_text(encoding="utf-8")
    assert "fly secrets set MI_DB" in text
    assert "fly secrets set MI_REDIS_URL" in text
    assert fly["env"]["MI_CA_BUNDLE"] == "/data/ca-bundle.pem"


def test_the_worker_reconciles_after_ingesting():
    """P8: the derived state has to be re-derived on the schedule, not only when a
    source happens to succeed.

    `mi ingest` reconciles at the end of its own run, so this is belt-and-braces for
    the case where *every* source failed: nothing was written, nothing triggered it,
    and the pricing states, anomalies and belief timeline would sit on a stale
    reading until a source came back.
    """
    for name, args in (
        ("fly", _fly()["processes"]["worker"]),
        ("k8s", " ".join(_containers(_k8s()["CronJob/mininfer-worker"]["spec"]["jobTemplate"]
                                     ["spec"])[0]["args"])),
    ):
        assert "mi ingest" in args, name
        assert "mi reconcile" in args, f"{name} worker ingests but never reconciles: {args}"
        assert args.index("mi reconcile") > args.index("mi ingest"), (
            f"{name} must reconcile *after* ingesting, or it re-derives yesterday's evidence")
