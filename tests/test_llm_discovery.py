import asyncio

from reporting.llm import discovery
from reporting.llm.discovery import LMStudioDiscovery, discover_lmstudio, select_model


def test_select_model_prefers_configured():
    assert select_model(["a", "qwen3.6", "b"], "qwen3.6") == "qwen3.6"


def test_select_model_falls_back_to_first_loaded():
    assert select_model(["llama-3", "phi-4"], "qwen3.6") == "llama-3"


def test_select_model_empty_returns_none():
    assert select_model([], "qwen3.6") is None


def _patch_probe(monkeypatch, mapping):
    """Patch probe_lmstudio so each base_url maps to a models list or None (unreachable)."""
    async def fake_probe(base_url, timeout=2.0):
        return mapping.get(base_url)
    monkeypatch.setattr(discovery, "probe_lmstudio", fake_probe)


def test_discover_returns_first_server_with_model(monkeypatch):
    _patch_probe(monkeypatch, {
        "http://host.docker.internal:1234/v1": None,            # unreachable
        "http://172.17.0.1:1234/v1": ["qwen3.6", "phi-4"],      # has models
        "http://localhost:1234/v1": ["other"],
    })
    result = asyncio.run(discover_lmstudio(
        candidates=[
            "http://host.docker.internal:1234/v1",
            "http://172.17.0.1:1234/v1",
            "http://localhost:1234/v1",
        ],
        preferred_model="qwen3.6",
    ))
    assert result.available is True
    assert result.base_url == "http://172.17.0.1:1234/v1"
    assert result.selected_model == "qwen3.6"


def test_discover_auto_selects_loaded_model(monkeypatch):
    _patch_probe(monkeypatch, {"http://localhost:1234/v1": ["llama-3"]})
    result = asyncio.run(discover_lmstudio(
        candidates=["http://localhost:1234/v1"], preferred_model="qwen3.6",
    ))
    assert result.available is True
    assert result.selected_model == "llama-3"


def test_discover_reports_server_up_but_no_model(monkeypatch):
    _patch_probe(monkeypatch, {"http://localhost:1234/v1": []})
    result = asyncio.run(discover_lmstudio(candidates=["http://localhost:1234/v1"]))
    assert result.available is False
    assert result.base_url == "http://localhost:1234/v1"
    assert "no model loaded" in result.error


def test_discover_no_server_reachable(monkeypatch):
    _patch_probe(monkeypatch, {})  # every url -> None
    result = asyncio.run(discover_lmstudio(candidates=["http://localhost:1234/v1"]))
    assert result.available is False
    assert result.base_url is None
    assert "no LM Studio server reachable" in result.error


def test_discovery_to_dict_roundtrip():
    d = LMStudioDiscovery(available=True, base_url="http://x/v1", models=["m"], selected_model="m")
    assert d.to_dict() == {
        "available": True,
        "base_url": "http://x/v1",
        "models": ["m"],
        "selected_model": "m",
        "error": None,
    }
