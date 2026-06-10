from pathlib import Path

import pytest
import yaml


def test_compose_security_boundaries() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert "privileged" not in services["api"]
    assert services["worker"]["volumes"][0]["read_only"] is True
    assert services["watcher"]["volumes"][0]["read_only"] is True
    telegram_volumes = services["telegram-bot"].get("volumes", [])
    assert telegram_volumes == ["stt_model_cache:/models/stt"]
    assert "volumes" not in services["openclaw"]
    for service_name in ("api", "worker", "watcher", "scheduler", "telegram-bot", "openclaw"):
        assert "ALL" in services[service_name]["cap_drop"]


@pytest.mark.xfail(
    reason="Read-only root fs for the api container is desired hardening but is "
    "not yet validated against the Windows Docker Desktop deployment (kaleido/"
    "plotly and library caches may need writable paths). Enable once verified.",
    strict=False,
)
def test_api_container_has_read_only_root_filesystem() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert compose["services"]["api"]["read_only"] is True


def test_published_ports_bind_to_loopback_by_default() -> None:
    """Published host ports must default to the printer PC's loopback, not the LAN.

    The bind prefix is configurable via ${BIND_HOST:-127.0.0.1}; the default
    keeps the API, MinIO, MCP and Watchtower off the local network.
    """
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    for service_name in ("api", "minio", "watchtower"):
        for mapping in services[service_name].get("ports", []):
            assert "127.0.0.1" in str(mapping), (
                f"{service_name} port {mapping!r} is not bound to loopback by default"
            )
