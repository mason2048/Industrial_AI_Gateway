"""API acceptance tests use isolated installations, never the bundled data directory."""
import json
import socket
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opcua import Server, ua

from backend.configuration import ConfigStore
from backend.main import create_app
from backend.models import Tag, typed_value
from backend.preflight import validate_nodes
from backend.observability import Redactor
from backend.tag_manager import export_excel
from scripts.runtime import AlreadyRunning, ProcessLock, is_running


@pytest.fixture
def installation(tmp_path):
    for folder in ("config", "data", "frontend"):
        (tmp_path / folder).mkdir()
    (tmp_path / "config/config.json").write_text(json.dumps({
        "mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4840",
        "poll_interval": 0.1, "backup_enabled": False,
    }))
    tags = [Tag(id=101, name="液位", device="水箱", address="DB1.DBD0", type="FLOAT", permission="WRITE"),
            Tag(id=102, name="运行", device="水箱", address="M0.0", type="BOOL")]
    (tmp_path / "data/tags.xlsx").write_bytes(export_excel(tags))
    (tmp_path / "frontend/index.html").write_text("<!doctype html><title>Test</title>")
    return tmp_path


@pytest.fixture
def client(installation):
    app = create_app(installation)
    with TestClient(app) as client:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if client.get("/api/current").json()["good"] == 2 and app.state.gateway.diagnostics()["saved_batches"]:
                break
            time.sleep(0.01)
        yield client
    assert app.state.shutdown_result["drained"]


def management(client, resource="tags"):
    result = client.get("/api/" + resource)
    return {"X-Operator-Pin": client.app.state.operator_pin, "If-Match": result.headers["ETag"]}


def test_mutation_requires_management_and_current_revision(client):
    tags = client.get("/api/tags").json()
    assert client.put("/api/tags", json=tags).status_code == 403
    pin = {"X-Operator-Pin": client.app.state.operator_pin}
    assert client.put("/api/tags", json=tags, headers=pin).status_code == 428
    headers = management(client)
    tags[0]["name"] = "水位A"
    tags[0]["revision"] = 999  # Client cannot choose persisted definition revision.
    saved = client.put("/api/tags", json=tags, headers=headers)
    assert saved.status_code == 200
    assert saved.headers["ETag"] != headers["If-Match"]
    tags[0]["name"] = "水位B"
    assert client.put("/api/tags", json=tags, headers=headers).status_code == 412
    actual = client.get("/api/tags").json()[0]
    assert actual["name"] == "水位A" and actual["revision"] == 2


def test_excel_preview_is_read_only_and_conflicts_on_apply(client):
    tags = [Tag.model_validate(t) for t in client.get("/api/tags").json()]
    tags[0] = tags[0].model_copy(update={"name": "新液位"})
    content = export_excel(tags)
    headers = management(client)
    preview = client.post("/api/tags/import?dry_run=true", files={"file": ("tags.xlsx", content)}, headers=headers)
    assert preview.status_code == 200
    assert preview.json()["diff"]["changed"] == [{"id": 101, "name": "新液位"}]
    assert client.get("/api/tags").json()[0]["name"] == "液位"
    # An intervening update must prevent previewed contents from replacing newer data.
    current = client.get("/api/tags").json()
    current[1]["name"] = "运行中"
    assert client.put("/api/tags", json=current, headers=headers).status_code == 200
    response = client.post("/api/tags/import", files={"file": ("tags.xlsx", content)}, headers={
        "X-Operator-Pin": headers["X-Operator-Pin"], "If-Match": preview.headers["ETag"]})
    assert response.status_code == 412
    assert client.get("/api/tags").json()[1]["name"] == "运行中"


def test_excel_preview_rejects_permanent_identity_change(client):
    tags = [Tag.model_validate(t) for t in client.get("/api/tags").json()]
    tags[0] = tags[0].model_copy(update={"address": "different"})
    response = client.post("/api/tags/import?dry_run=true", files={"file": ("tags.xlsx", export_excel(tags))},
                           headers=management(client))
    assert response.status_code == 400
    assert client.get("/api/tags").json()[0]["address"] == "DB1.DBD0"


def test_connection_candidate_is_not_applied_and_updates_are_revisioned(client):
    before = client.get("/api/config")
    headers = management(client, "config")
    candidate = {"mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4999"}
    assert client.post("/api/connection/test", json=candidate).status_code == 403
    assert client.post("/api/connection/test", json=candidate, headers=headers).json()["success"]
    assert client.get("/api/config").json() == before.json()
    applied = client.post("/api/connection", json=candidate, headers=headers)
    assert applied.status_code == 200
    assert applied.json()["connection_id"] != before.json()["connection_id"]
    assert client.post("/api/connection", json=candidate, headers=headers).status_code == 412


def test_field_write_is_denied_even_with_permission_and_pin(client):
    # Simulation writes default off. A WRITE point plus the management PIN is insufficient.
    assert client.post("/api/operator/write-request", json={"tag_id": 101, "value": 2},
                       headers=management(client)).status_code == 403
    client.app.state.gateway.config.update(mode="opcua", simulation_write_enabled=True)
    with pytest.raises(PermissionError):
        client.app.state.gateway.manual_write(101, 2)
    client.app.state.gateway.config["mode"] = "simulation"


def test_history_versions_and_retired_points_visible(client):
    initial = client.get("/api/history/variables").json()["items"]
    assert any(row["tag_id"] == 101 and row["active"] for row in initial)
    tags = client.get("/api/tags").json()
    assert client.put("/api/tags", json=[tags[1]], headers=management(client)).status_code == 200
    rows = client.get("/api/history/variables?device=水箱&source=simulation").json()["items"]
    retired = next(row for row in rows if row["tag_id"] == 101)
    assert retired["active"] is False
    params = {k: retired[k] for k in ("tag_id", "connection_id", "tag_revision", "source")}
    assert client.get("/api/history", params=params).json()["total"] >= 1
    series = client.get("/api/history/series", params={**params, "buckets": 10}).json()
    assert len(series["items"]) == 10 and len(series["series"]) == 1
    assert client.get("/api/history/series", params={**params, "buckets": 2001}).status_code == 422


def test_ai_source_is_current_connection_and_health_is_unknown(client):
    response = client.post("/api/ai/query", json={"question": "分析液位", "device": "水箱"})
    assert response.status_code == 200
    result = response.json()
    assert result["connection_id"] == client.get("/api/current").json()["connection_id"]
    assert result["evidence_count"] >= 1
    assert client.get("/api/ai/status").json()["health"] == "unknown"
    assert client.get("/api/ai/history?source=anything").status_code == 422


def test_readiness_reflects_writer_failure_without_affecting_liveness(client, monkeypatch):
    assert client.get("/api/health").json()["status"] == "running"
    original = client.app.state.gateway.writer.diagnostics
    monkeypatch.setattr(client.app.state.gateway.writer, "diagnostics", lambda: {
        **original(), "writer_alive": False, "storage_error": "injected failure"})
    result = client.get("/api/ready")
    assert result.status_code == 503 and not result.json()["checks"]["storage"]["ok"]
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/current").json()["good"] == 2


def test_stalled_event_database_does_not_block_diagnostics(client, monkeypatch):
    entered, released = threading.Event(), threading.Event()
    original = client.app.state.db.events
    def stalled(*args, **kwargs):
        entered.set()
        released.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(client.app.state.db, "events", stalled)
    try:
        assert entered.wait(2)
        elapsed = []
        for path in ("/api/current", "/api/ready", "/api/diagnostics"):
            start = time.perf_counter()
            assert client.get(path).status_code in (200, 503)
            elapsed.append(time.perf_counter() - start)
        assert max(elapsed) < .5
    finally:
        released.set()


def test_credentials_are_not_returned_or_logged(client, monkeypatch):
    secret = "test-password-not-for-output"
    monkeypatch.setenv("TEST_PLC_SECRET", secret)
    state = client.app.state
    state.config_store.update({"username": "test-plc-account", "password_env": "TEST_PLC_SECRET"},
                              state.config_store.snapshot()["revision"])
    state.refresh_redaction()
    state.operations.record("redaction_test", {"message": secret + " " + state.operator_pin})
    for path in ("/api/config", "/api/current", "/api/diagnostics", "/api/ready"):
        body = client.get(path).text
        assert secret not in body and state.operator_pin not in body and "test-plc-account" not in body
    logs = (state.root / "data/logs/gateway.log").read_text()
    assert secret not in logs and state.operator_pin not in logs and "[REDACTED]" in logs


def test_bad_security_preflight_is_local_and_preserves_config(client):
    before = client.get("/api/config").json()
    response = client.post("/api/connection/test", json={"mode": "opcua", "endpoint": "opc.tcp://127.0.0.1:4840",
        "security_string": "Basic256Sha256,SignAndEncrypt,/missing/cert.der,/missing/key.pem"}, headers=management(client))
    assert response.status_code == 400
    assert client.get("/api/config").json() == before


def test_missing_config_does_not_seed_data(tmp_path):
    with pytest.raises(ValueError, match="config"):
        create_app(tmp_path)
    assert not (tmp_path / "data/history.db").exists()
    assert not (tmp_path / "data/tags.xlsx").exists()
    assert not is_running(tmp_path)


def test_second_in_process_app_cannot_migrate_live_installation(client):
    with pytest.raises(RuntimeError, match="运行"):
        create_app(client.app.state.root)


def test_partial_startup_failure_stops_workers(installation, monkeypatch):
    app = create_app(installation)
    monkeypatch.setattr(app.state.operations, "start", lambda: (_ for _ in ()).throw(RuntimeError("injected startup")))
    with pytest.raises(RuntimeError, match="injected startup"):
        with TestClient(app):
            pass
    assert app.state.shutdown_result["collector_stopped"]
    assert app.state.shutdown_result["storage_stopped"]
    assert not is_running(installation)


def test_node_preflight_checks_declared_types_and_permissions(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = Server()
    endpoint = f"opc.tcp://127.0.0.1:{port}"
    server.set_endpoint(endpoint)
    namespace = server.register_namespace("gateway-preflight")
    obj = server.get_objects_node().add_object(namespace, "Tank")
    node = obj.add_variable(namespace, "Level", ua.Variant(42., ua.VariantType.Float))
    tag = Tag(id=1, name="Level", address="A", type="FLOAT", node_id=node.nodeid.to_string())
    config = {"mode": "opcua", "endpoint": endpoint, "batch_size": 100}
    server.start()
    try:
        assert validate_nodes([tag], config, tmp_path)[0]["quality"] == "Good"
        mismatch = tag.model_copy(update={"type": "WORD"})
        assert validate_nodes([mismatch], config, tmp_path)[0]["quality"] == "BadTypeMismatch"
    finally:
        server.stop()


def test_extreme_integer_is_a_validation_error():
    with pytest.raises(ValueError):
        typed_value(10**400, "FLOAT")


def test_redaction_preserves_structured_protocol_fields():
    redact = Redactor(["opcua", "A", "Good", "2026"])
    reading = {"mode": "opcua", "source": "opcua", "unit": "A", "quality": "Good",
               "timestamp": "2026-09-05T00:00:00+00:00", "error": "password=opcua"}
    result = redact.clean(reading)
    assert {k: v for k, v in result.items() if k != "error"} == {k: v for k, v in reading.items() if k != "error"}
    assert "opcua" not in result["error"]


def test_offline_owner_cannot_be_reentered_by_application(installation):
    with ProcessLock(installation, purpose="restore", reentrant=False):
        with pytest.raises(AlreadyRunning):
            create_app(installation)
    assert not is_running(installation)
