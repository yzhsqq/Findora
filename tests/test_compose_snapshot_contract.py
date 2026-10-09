"""Inspect actual Compose merge output without starting services or using secrets."""
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI unavailable")
@pytest.mark.parametrize("extras", [[], ["amazon"], ["ebay"], ["amazon", "ebay"]])
def test_snapshot_compose_has_one_frontend_and_consistent_worker(extras):
    root = Path(__file__).resolve().parents[1]
    args = ["docker", "compose", "-f", "docker/docker-compose.yaml", "-f", "docker/docker-compose.cj.yaml"]
    for platform in extras:
        args += ["-f", f"docker/docker-compose.{platform}.yaml"]
    result = subprocess.run(args + ["config", "--no-interpolate", "--no-path-resolution"],
                            cwd=root, capture_output=True, encoding="utf-8", check=True)
    services = yaml.safe_load(result.stdout)["services"]
    frontends = [name for name, service in services.items()
                 if any(str(p.get("published")) == "5173" for p in service.get("ports", []))]
    assert frontends == ["frontend"]
    assert not services["frontend"].get("build")
    assert services["worker"]["profiles"] == ["snapshot-worker"]
    app, worker = services["app"], services["worker"]
    def environment(service):
        values = service["environment"]
        return values if isinstance(values, dict) else dict(v.split("=", 1) for v in values)
    app_env, worker_env = environment(app), environment(worker)
    for key in ("CATALOG_SOURCE", "CJ_CATALOG_PATH", "AMAZON_CATALOG_PATH", "EBAY_CATALOG_PATH",
                "QUEUE_ENABLED", "REDIS_URL", "HYBRID_RECALL_ENABLED", "QDRANT_COLLECTION"):
        assert app_env.get(key) == worker_env.get(key)
    assert sorted(v["target"] for v in app["volumes"]) == sorted(v["target"] for v in worker["volumes"])
