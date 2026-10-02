"""Operational tests use new temporary databases, never the bundled field data."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request

import pytest

from backend.maintenance import create_backup, restore_backup, validate_backup
from scripts.runtime import AlreadyRunning, ProcessLock, is_running, request_stop
from scripts import bootstrap
from launch import check_installation


@pytest.fixture
def installation(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    config = {"mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4840", "poll_interval": 1,
              "batch_size": 100, "heartbeat_seconds": 1800, "retention_days": 7,
              "password_env": "TEST_PLC_PASSWORD", "password": "do-not-copy"}
    (tmp_path / "config/config.json").write_text(json.dumps(config))
    (tmp_path / "data/operator_pin.txt").write_text("never-read-this")
    (tmp_path / "config/private.pem").write_text("never-copy-this")
    with closing(sqlite3.connect(tmp_path / "data/history.db")) as db:
        db.executescript("PRAGMA user_version=1; PRAGMA journal_mode=WAL; CREATE TABLE samples(id INTEGER PRIMARY KEY, value REAL); INSERT INTO samples VALUES(1, 42); CREATE TABLE write_audit(outcome TEXT); INSERT INTO write_audit VALUES('old audit');")
    return tmp_path


def rows(root):
    with closing(sqlite3.connect(root / "data/history.db")) as db:
        return db.execute("SELECT * FROM samples ORDER BY id").fetchall()


def test_backup_restore_old_schema_without_migration_and_secrets(installation):
    backup = create_backup(installation)
    manifest = validate_backup(backup)
    assert manifest["database_version"] == 1
    assert set(path.name for path in backup.iterdir()) == {"manifest.json", "history.db", "config.json"}
    config = json.loads((backup / "config.json").read_text())
    assert "password" not in config and config["password_env"] == "TEST_PLC_PASSWORD"
    with closing(sqlite3.connect(installation / "data/history.db")) as db:
        db.execute("INSERT INTO samples VALUES(2, 99)")
        db.commit()
    restored = restore_backup(installation, backup)
    assert restored["migrated"] is False
    assert rows(installation) == [(1, 42)]
    assert validate_backup(Path(restored["pre_restore_backup"]))["database_version"] == 1
    assert (installation / "data/operator_pin.txt").read_text() == "never-read-this"
    assert (installation / "config/private.pem").read_text() == "never-copy-this"
    with closing(sqlite3.connect(installation / "data/history.db")) as db:
        assert db.execute("SELECT * FROM write_audit").fetchall() == [("old audit",)]
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_backup_includes_committed_wal_and_retains_seven(installation):
    with closing(sqlite3.connect(installation / "data/history.db")) as active:
        active.execute("INSERT INTO samples VALUES(2, 88)")
        active.commit()
        latest = create_backup(installation)
        with closing(sqlite3.connect(latest / "history.db")) as backup:
            assert backup.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 2
    for _ in range(8):
        create_backup(installation)
    backups = [p for p in (installation / "data/backups").iterdir() if (p / "manifest.json").exists()]
    assert len(backups) == 7


def test_corrupt_backup_rejected_without_touching_live_database(installation):
    backup = create_backup(installation)
    with (backup / "config.json").open("a") as handle:
        handle.write(" ")
    before = (installation / "data/history.db").read_bytes()
    with pytest.raises(ValueError, match="checksum"):
        restore_backup(installation, backup)
    assert (installation / "data/history.db").read_bytes() == before


def test_malformed_backup_manifest_is_a_validation_error(installation):
    backup = create_backup(installation)
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["history.db"].pop("size")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="invalid"):
        validate_backup(backup)


def test_restore_failure_rolls_back_database_and_config(installation, monkeypatch):
    backup = create_backup(installation)
    with closing(sqlite3.connect(installation / "data/history.db")) as db:
        db.execute("INSERT INTO samples VALUES(2, 99)")
        db.commit()
    before_config = (installation / "config/config.json").read_bytes()
    original_replace = Path.replace
    def fail_config_once(path, target):
        if path.name == "config.json" and path.parent.name.startswith(".restore-"):
            raise OSError("injected config replacement failure")
        return original_replace(path, target)
    monkeypatch.setattr(Path, "replace", fail_config_once)
    with pytest.raises(OSError, match="injected"):
        restore_backup(installation, backup)
    assert rows(installation) == [(1, 42), (2, 99)]
    assert (installation / "config/config.json").read_bytes() == before_config


def test_process_lock_reentrant_and_restore_rejected_while_running(installation):
    backup = create_backup(installation)
    assert not is_running(installation)
    with ProcessLock(installation) as outer:
        with ProcessLock(installation) as inner:
            assert outer.token == inner.token
            with pytest.raises(AlreadyRunning):
                restore_backup(installation, backup)
        assert is_running(installation)
    assert not is_running(installation)
    assert not (installation / "data/gateway.pid").exists()


def test_lock_is_cross_process_and_stop_uses_verified_token(tmp_path):
    code = """import sys,time
from pathlib import Path
from scripts.runtime import ProcessLock
with ProcessLock(Path(sys.argv[1])) as owner:
    print('ready',flush=True)
    while not owner.stop_requested(): time.sleep(.02)
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        assert is_running(tmp_path)
        with pytest.raises(AlreadyRunning):
            with ProcessLock(tmp_path):
                pass
        # A stale shutdown request cannot stop a new process.
        (tmp_path / "data/gateway.stop").write_text(json.dumps({"token": "wrong-token"}))
        time.sleep(.05)
        assert child.poll() is None
        assert request_stop(tmp_path, timeout=3)["stopped"]
        assert child.wait(timeout=3) == 0
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=3)
        child.stdout.close()


def test_stale_pid_alone_never_signals_any_process(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data/gateway.pid").write_text("12345")
    assert request_stop(tmp_path)["stopped"]
    assert not (tmp_path / "data/gateway.stop").exists()


def test_read_only_startup_check_never_initializes_database(installation):
    (installation / "data/history.db").unlink()
    path = installation / "config/config.json"
    config = json.loads(path.read_text())
    config.pop("password")
    path.write_text(json.dumps(config))
    result = check_installation(installation)
    assert not result["database_exists"]
    assert not (installation / "data/history.db").exists()
    assert result["mode"] == "simulation"


def test_missing_config_does_not_seed(tmp_path):
    with pytest.raises(RuntimeError, match="Missing config"):
        check_installation(tmp_path)
    assert not (tmp_path / "data").exists()


def test_explicit_demo_init_uses_current_connection_and_leaves_existing_db(tmp_path):
    from seed_data import seed
    seed(tmp_path)
    config = json.loads((tmp_path / "config/config.json").read_text())
    database = tmp_path / "data/history.db"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT DISTINCT connection_id FROM history_data").fetchall() == [(config["connection_id"],)]
        assert db.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 7200
    before = database.read_bytes()
    seed(tmp_path)
    assert database.read_bytes() == before


def test_launcher_and_cooperative_shutdown_on_temporary_installation(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    from seed_data import seed
    seed(tmp_path)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/index.html").write_text("temporary launcher test")
    config_path = tmp_path / "config/config.json"
    config = json.loads(config_path.read_text())
    config["backup_enabled"] = False
    config_path.write_text(json.dumps(config))
    script = Path(__file__).resolve().parents[1] / "launch.py"
    child = subprocess.Popen([sys.executable, str(script), "--root", str(tmp_path), "--no-browser", "--port", str(port)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if child.poll() is not None:
                pytest.fail(child.stdout.read())
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=.3) as response:
                    from backend.version import APP_VERSION
                    if json.load(response)["version"] == APP_VERSION:
                        break
            except OSError:
                time.sleep(.05)
        else:
            pytest.fail("Launcher did not become healthy in 10 seconds")
        assert is_running(tmp_path)
        assert request_stop(tmp_path, timeout=10)["stopped"]
        assert child.wait(timeout=3) == 0, child.stdout.read()
        assert not is_running(tmp_path)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        child.stdout.close()


def test_dependency_stamp_changes_only_after_successful_install(tmp_path, monkeypatch):
    environment = tmp_path / ".venv"
    interpreter = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    lock = tmp_path / "requirements-lock.txt"
    lock.write_text("example==1\n")
    monkeypatch.setattr(bootstrap.subprocess, "check_output", lambda *a, **k: "3.12\n")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    bootstrap.ensure_environment(tmp_path)
    assert len(calls) == 2
    stamp = environment / "dependencies.sha256"
    first = stamp.read_text()
    bootstrap.ensure_environment(tmp_path)
    assert len(calls) == 2
    lock.write_text("example==2\n")
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(bootstrap.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.ensure_environment(tmp_path)
    assert stamp.read_text() == first
    monkeypatch.setattr(bootstrap.subprocess, "run", run)
    bootstrap.ensure_environment(tmp_path)
    assert stamp.read_text() != first


@pytest.mark.parametrize("failed", ["collector_stopped", "storage_stopped", "drained", "operations_stopped"])
def test_launcher_reports_incomplete_shutdown_as_failure(tmp_path, monkeypatch, failed):
    import launch
    import uvicorn
    import backend.main
    result = {name: name != failed for name in ("collector_stopped", "storage_stopped", "drained", "operations_stopped")}
    application = SimpleNamespace(state=SimpleNamespace(shutdown_result=result))
    class Server:
        def __init__(self, config):
            self.started = True
        def run(self):
            pass
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(backend.main, "create_app", lambda root: application)
    monkeypatch.setattr(launch, "check_installation", lambda root: {})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(RuntimeError, match=failed):
        launch.run_gateway(tmp_path, no_browser=True, port=port)


def _virtual_backup_operations(root, monkeypatch):
    import threading
    import backend.observability as observability
    clock = {"wall": 200000.0, "monotonic": 1000.0}
    class Stop:
        stopped = False
        waits = []
        def is_set(self):
            return self.stopped
        def wait(self, duration):
            self.waits.append(duration)
            clock["monotonic"] += duration
            # Deliberate repeated wall-clock corrections must not affect cadence.
            clock["wall"] += duration + 100000
            return self.stopped
    stop = Stop()
    monkeypatch.setattr(observability, "time", SimpleNamespace(
        time=lambda: clock["wall"], monotonic=lambda: clock["monotonic"]))
    events = []
    operations = SimpleNamespace(root=root, lock=threading.RLock(), stop_event=stop,
        last_backup=None, backup_error="", redactor=SimpleNamespace(text=str),
        record=lambda kind, detail, persist=True: events.append((kind, detail)))
    return observability, operations, clock, events


def test_backup_monitor_verifies_newest_once_and_uses_monotonic_daily_schedule(tmp_path, monkeypatch):
    import os
    import backend.maintenance as maintenance
    module, operations, clock, events = _virtual_backup_operations(tmp_path, monkeypatch)
    for name, age in (("old", 86500), ("valid", 86300), ("corrupt", 10), (".pending", 0)):
        path = tmp_path / "data/backups" / name / "manifest.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}")
        stamp = clock["wall"] - age
        os.utime(path, (stamp, stamp))
    checked = []
    def validate(path):
        checked.append(path.name)
        if path.name == "corrupt":
            raise ValueError("invalid backup")
        return {}
    attempts = []
    initial = clock["monotonic"]
    def create(root):
        attempts.append(clock["monotonic"] - initial)
        if len(attempts) == 2:
            operations.stop_event.stopped = True
        return root / "data/backups" / f"new-{len(attempts)}"
    monkeypatch.setattr(maintenance, "validate_backup", validate)
    monkeypatch.setattr(maintenance, "create_backup", create)
    module.Operations._backups(operations)
    assert checked == ["corrupt", "valid"]  # Never scans old or rehashes on each tick.
    assert attempts == [100, 86500]
    assert operations.last_backup.endswith("new-2")
    assert operations.backup_error == ""
    assert [kind for kind, _ in events] == ["backup_complete", "backup_complete"]


def test_backup_monitor_retries_after_300_seconds_and_deduplicates_failure(tmp_path, monkeypatch):
    import backend.maintenance as maintenance
    module, operations, clock, events = _virtual_backup_operations(tmp_path, monkeypatch)
    attempts = []
    initial = clock["monotonic"]
    def create(root):
        attempts.append(clock["monotonic"] - initial)
        if len(attempts) <= 2:
            raise OSError("disk unavailable")
        operations.stop_event.stopped = True
        return root / "data/backups/recovered"
    monkeypatch.setattr(maintenance, "create_backup", create)
    module.Operations._backups(operations)
    assert attempts == [0, 300, 600]
    assert [kind for kind, _ in events] == ["backup_failed", "backup_recovered", "backup_complete"]
    assert operations.backup_error == ""
    assert operations.last_backup.endswith("recovered")
