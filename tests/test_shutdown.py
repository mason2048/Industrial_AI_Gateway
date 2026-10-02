import json
from fastapi.testclient import TestClient
from backend.main import create_app


def test_shutdown_requires_operator_and_signals_only_owned_installation(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "config/config.json").write_text(json.dumps({"mode": "simulation",
        "endpoint": "opc.tcp://127.0.0.1:4840", "backup_enabled": False}))
    app = create_app(tmp_path)
    with TestClient(app) as client:
        stopfile = tmp_path / "data/gateway.stop"
        assert client.post("/api/shutdown").status_code == 403
        assert not stopfile.exists()
        reply = client.post("/api/shutdown", headers={"X-Operator-Pin": app.state.operator_pin})
        assert reply.status_code == 202 and reply.json()["status"] == "stopping"
        message = json.loads(stopfile.read_text())
        owner = json.loads((tmp_path / "data/gateway.pid").read_text())
        assert message["token"] == owner["token"]
    assert not stopfile.exists()
