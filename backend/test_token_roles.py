import pytest
from fastapi.testclient import TestClient

from backend import main
from backend.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("backend.main.LIVEKIT_API_KEY", "test-key")
    monkeypatch.setattr("backend.main.LIVEKIT_API_SECRET", "test-secret-0123456789abcdef")
    monkeypatch.setattr("backend.main.LIVEKIT_URL", "wss://example.livekit.cloud")
    return TestClient(app)


def test_token_defaults_to_agent_role(client):
    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "agent-desktop-1",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert body["serverUrl"] == "wss://example.livekit.cloud"


def test_agent_role_still_requires_agent_in_identity(client):
    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "desktop-1",
        "role": "agent",
    })
    assert resp.status_code == 422


def test_listener_role_accepts_non_agent_identity(client):
    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "vbcable-listener-1",
        "role": "listener",
    })
    assert resp.status_code == 200
    assert "token" in resp.json()


def test_listener_role_rejects_unsafe_identity(client):
    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "listener with spaces",
        "role": "listener",
    })
    assert resp.status_code == 422


def test_listener_role_grants_subscribe_only(client, monkeypatch):
    captured = {}

    original = main.api.VideoGrants

    class RecordingGrants(original):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("backend.main.api.VideoGrants", RecordingGrants)

    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "vbcable-listener-1",
        "role": "listener",
    })
    assert resp.status_code == 200
    assert captured["can_publish"] is False
    assert captured["can_subscribe"] is True
