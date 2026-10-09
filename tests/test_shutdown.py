import json
import threading
import time
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from backend.main import create_app
from scripts.runtime import AlreadyRunning, ProcessLock, is_running


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


def test_shutdown_waits_for_an_in_progress_backup_within_the_shared_deadline(tmp_path, monkeypatch):
    from backend.observability import Operations

    (tmp_path / "config").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "config/config.json").write_text(json.dumps({"mode": "simulation",
        "endpoint": "opc.tcp://127.0.0.1:4840", "backup_enabled": True}))
    backup_started = threading.Event()
    backup_finished = threading.Event()

    def slow_in_progress_backup(self):
        backup_started.set()
        self.stop_event.wait()
        # A disk operation already in progress can outlast the initial stop join.
        # Release it within the existing 30-second shared shutdown allowance.
        backup_finished.wait(1.3)

    monkeypatch.setattr(Operations, "_backups", slow_in_progress_backup)
    app = create_app(tmp_path)
    try:
        with TestClient(app):
            assert backup_started.wait(5)
        result = app.state.shutdown_result
        assert result["operations_stopped"]
        assert result["collector_stopped"] and result["storage_stopped"] and result["drained"]
        assert not app.state.operations.backup_thread.is_alive()
        assert not is_running(tmp_path)
    finally:
        backup_finished.set()
        app.state.operations.backup_thread.join(5)


def test_backup_past_shared_deadline_keeps_installation_owned_until_it_finishes(tmp_path, monkeypatch):
    import backend.main as composition
    from backend.observability import Operations

    (tmp_path / "config").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "config/config.json").write_text(json.dumps({"mode": "simulation",
        "endpoint": "opc.tcp://127.0.0.1:4840", "backup_enabled": True}))
    backup_started, release_backup = threading.Event(), threading.Event()

    def unfinished_backup(self):
        backup_started.set()
        release_backup.wait(5)

    monkeypatch.setattr(Operations, "_backups", unfinished_backup)
    # Only the composition clock advances; the workers retain their real clocks.
    clock = {"now": 0}
    monkeypatch.setattr(composition, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    app = create_app(tmp_path)
    stop_gateway = app.state.gateway.stop

    def drain_after_budget(timeout):
        result = stop_gateway(timeout)
        clock["now"] = 31
        return result

    monkeypatch.setattr(app.state.gateway, "stop", drain_after_budget)
    try:
        with TestClient(app):
            assert backup_started.wait(5)
        assert app.state.shutdown_result["operations_stopped"] is False
        assert app.state.shutdown_result["storage_stopped"]
        assert is_running(tmp_path)
        with pytest.raises(AlreadyRunning):
            with ProcessLock(tmp_path, reentrant=False, purpose="restore"):
                pass
        release_backup.set()
        deadline = time.monotonic() + 5
        while is_running(tmp_path) and time.monotonic() < deadline:
            time.sleep(.01)
        assert not is_running(tmp_path)
    finally:
        release_backup.set()
        app.state.operations.backup_thread.join(5)
