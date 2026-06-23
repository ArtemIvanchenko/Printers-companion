from pathlib import Path

import yaml


def test_compose_security_boundaries() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "privileged" not in services["api"]
    assert services["api"]["read_only"] is True
    assert services["worker"]["volumes"][0]["read_only"] is True
    assert services["watcher"]["volumes"][0]["read_only"] is True
    assert "volumes" not in services["openclaw"]
    for service_name in ("api", "worker", "watcher", "scheduler", "openclaw"):
        assert "ALL" in services[service_name]["cap_drop"]
