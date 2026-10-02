"""Standalone launch checks use isolated directories and only simulation data."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from scripts import desktop_runtime as desktop
from scripts.runtime import ProcessLock, is_running, request_stop


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def resources(tmp_path):
    resource = tmp_path / "resources"
    (resource / "frontend/vendor").mkdir(parents=True)
    (resource / "frontend/index.html").write_text("new frontend", encoding="utf-8")
    (resource / "frontend/vendor/vue.global.prod.js").write_text("bundled Vue", encoding="utf-8")
    return resource


def test_fresh_installation_has_six_explicit_simulation_points_without_history(tmp_path, resources):
    from backend.tag_manager import import_excel
    root = tmp_path / "installation"
    with ProcessLock(root):
        desktop.initialize_installation(root, resources)
    config = json.loads((root / "config/config.json").read_text(encoding="utf-8"))
    assert config["mode"] == "simulation"
    assert config["endpoint"] == desktop.SAFE_ENDPOINT
    assert config["simulation_write_enabled"] is False
    assert "connection_id" not in config and not config.get("username")
    tags = import_excel((root / "data/tags.xlsx").read_bytes())
    assert len(tags) == 6
    assert {tag.type for tag in tags} == {"BOOL", "WORD", "DWORD", "FLOAT"}
    assert all(tag.name.startswith("模拟") and tag.permission == "READ" for tag in tags)
    assert all(tag.history_interval_seconds == 1800 and not tag.record_changes for tag in tags)
    assert not (root / "data/history.db").exists()


def test_upgrade_refreshes_resources_and_preserves_configuration_database_and_excel(tmp_path, resources):
    root = tmp_path / "installation"
    with ProcessLock(root):
        desktop.initialize_installation(root, resources)
    config = root / "config/config.json"
    config.write_text('{"mode":"opcua","username":"private-account"}', encoding="utf-8")
    (root / "data/history.db").write_bytes(b"existing-real-history")
    (root / "data/tags.xlsx").write_bytes(b"existing-field-point-definitions")
    (root / "frontend/index.html").write_text("old interface", encoding="utf-8")
    with ProcessLock(root):
        desktop.initialize_installation(root, resources)
    assert config.read_text(encoding="utf-8") == '{"mode":"opcua","username":"private-account"}'
    assert (root / "data/history.db").read_bytes() == b"existing-real-history"
    assert (root / "data/tags.xlsx").read_bytes() == b"existing-field-point-definitions"
    assert (root / "frontend/index.html").read_text(encoding="utf-8") == "new frontend"
    assert not list(root.glob(".frontend-*"))


def test_missing_configuration_with_existing_history_is_not_replaced(tmp_path, resources):
    root = tmp_path / "installation"
    (root / "data").mkdir(parents=True)
    (root / "data/history.db").write_bytes(b"field-data")
    with ProcessLock(root), pytest.raises(desktop.DesktopError, match="已有历史数据库"):
        desktop.initialize_installation(root, resources)
    assert not (root / "config/config.json").exists()
    assert (root / "data/history.db").read_bytes() == b"field-data"


@pytest.mark.parametrize("field,value", [
    ("mode", "opcua"), ("endpoint", "opc.tcp://field.example:4840"),
    ("username", "private-user"), ("security_string", "certificate-path"),
    ("password", "private-password"), ("connection_id", "field-series"),
])
def test_installation_rejects_bundle_with_field_connection_data(tmp_path, resources, field, value):
    (resources / "config").mkdir()
    (resources / "config/config.default.json").write_text(json.dumps({field: value}), encoding="utf-8")
    root = tmp_path / "installation"
    with ProcessLock(root), pytest.raises(desktop.DesktopError, match="现场配置"):
        desktop.initialize_installation(root, resources)
    assert not (root / "config/config.json").exists()
    assert not (root / "data/tags.xlsx").exists()


def test_existing_frontend_remains_when_bundle_is_incomplete(tmp_path, resources):
    root = tmp_path / "installation"
    (root / "frontend").mkdir(parents=True)
    (root / "frontend/index.html").write_text("previous-version", encoding="utf-8")
    (resources / "frontend/vendor/vue.global.prod.js").unlink()
    with ProcessLock(root), pytest.raises(desktop.DesktopError, match="缺少网页资源"):
        desktop.initialize_installation(root, resources)
    assert (root / "frontend/index.html").read_text(encoding="utf-8") == "previous-version"


def test_root_uses_portable_then_fallback_and_preserves_existing_choice(tmp_path, monkeypatch):
    executable = tmp_path / "program/IndustrialAIGateway.exe"
    local = tmp_path / "user-local"
    portable = executable.parent / "gateway-data"
    fallback = local / "IndustrialAIGateway"
    monkeypatch.setattr(desktop, "_writable_directory", lambda root: root != portable)
    assert desktop.resolve_root(executable=executable, local_app_data=local) == fallback
    (fallback / "config").mkdir(parents=True)
    (fallback / "config/config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(desktop, "_writable_directory", lambda root: True)
    assert desktop.resolve_root(executable=executable, local_app_data=local) == fallback
    # A changed permission cannot silently send an installation to another root.
    monkeypatch.setattr(desktop, "_writable_directory", lambda root: root != fallback)
    with pytest.raises(desktop.DesktopError, match="现有数据目录无法写入"):
        desktop.resolve_root(executable=executable, local_app_data=local)


def test_stop_root_resolution_requires_no_write_probe(tmp_path, monkeypatch):
    executable = tmp_path / "program/IndustrialAIGateway.exe"
    fallback = tmp_path / "local/IndustrialAIGateway"
    (fallback / "config").mkdir(parents=True)
    (fallback / "config/config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(desktop, "_writable_directory", lambda root: pytest.fail("must not probe"))
    assert desktop.resolve_root(executable=executable, local_app_data=fallback.parent,
                                write_probe=False) == fallback


def test_explicit_port_rejects_occupied_socket_and_default_selects_next_port(monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        busy = listener.getsockname()[1]
        with pytest.raises(desktop.DesktopError, match="已被占用"):
            desktop.choose_port(busy)
    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def bind(self, address):
            if address[1] == 8080:
                raise OSError("occupied")
    monkeypatch.setattr(desktop.socket, "socket", lambda *args: Probe())
    assert desktop.choose_port() == 8081


def test_check_creates_machine_readable_result_without_collecting(tmp_path, monkeypatch, capsys):
    root = tmp_path / "check-installation"
    monkeypatch.setattr(desktop, "bundle_root", lambda: ROOT)
    assert desktop.main(["--root", str(root), "--check"]) == 0
    printed = json.loads(capsys.readouterr().out)
    stored = json.loads((root / "check-result.json").read_text(encoding="utf-8"))
    assert stored == printed == json.loads((root / "data/desktop-check.json").read_text(encoding="utf-8"))
    assert stored["ok"] and stored["mode"] == "simulation" and stored["read_only"]
    assert not stored["database_exists"]
    assert not (root / "data/history.db").exists()
    assert not is_running(root)


def test_safe_errors_never_log_credentials(tmp_path, monkeypatch, capsys):
    import launch
    root = tmp_path / "failed-check"
    monkeypatch.setattr(desktop, "bundle_root", lambda: ROOT)
    secret = "injected-private-password"
    def failure(root):
        raise ValueError(secret)
    monkeypatch.setattr(launch, "check_installation", failure)
    assert desktop.main(["--root", str(root), "--check"]) == 1
    output = capsys.readouterr()
    result = json.loads((root / "check-result.json").read_text(encoding="utf-8"))
    logs = (root / "logs/desktop.log").read_text(encoding="utf-8")
    assert result["error"] == "startup_failed"
    assert secret not in output.err + output.out + json.dumps(result) + logs
    assert "ValueError" in logs


def test_existing_instance_uses_verified_installation_metadata(tmp_path, monkeypatch):
    with ProcessLock(tmp_path) as owner:
        desktop._atomic_json(tmp_path / "data/desktop-instance.json", {
            "root": str(tmp_path), "port": 8765, "owner_started_at": owner.entry["started_at"]})
        opened = []
        monkeypatch.setattr(desktop, "_gateway_healthy", lambda port: port == 8765)
        monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
        result = desktop.open_existing_instance(tmp_path, timeout=.1)
        assert result["existing_instance"] and result["url"] == "http://127.0.0.1:8765"
        assert opened == [result["url"]]
        desktop._atomic_json(tmp_path / "data/desktop-instance.json", {
            "root": str(tmp_path), "port": 8765, "owner_started_at": 0})
        with pytest.raises(desktop.DesktopError, match="尚未就绪"):
            desktop.open_existing_instance(tmp_path, timeout=.02)
        assert len(opened) == 1


def test_desktop_subprocess_starts_six_points_reuses_instance_and_stops_cleanly(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    command = [sys.executable, str(ROOT / "desktop.py"), "--root", str(tmp_path)]
    child = subprocess.Popen([*command, "--no-browser", "--port", str(port)],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if child.poll() is not None:
                pytest.fail(child.stdout.read())
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/current", timeout=.3) as response:
                    snapshot = json.load(response)
                if snapshot["good"] == 6:
                    break
            except OSError:
                pass
            time.sleep(.05)
        else:
            pytest.fail("Desktop gateway did not collect six Good simulation points")
        assert len(snapshot["items"]) == 6
        assert (tmp_path / "data/history.db").is_file()
        owner = json.loads((tmp_path / "data/gateway.pid").read_text(encoding="utf-8"))
        again = subprocess.run([*command, "--no-browser"], capture_output=True, text=True,
                               cwd=ROOT, timeout=20)
        assert again.returncode == 0, again.stderr
        assert json.loads(again.stdout)["existing_instance"]
        assert json.loads((tmp_path / "data/gateway.pid").read_text(encoding="utf-8"))["token"] == owner["token"]
        stop = subprocess.run([*command, "--stop"], capture_output=True, text=True, cwd=ROOT, timeout=40)
        assert stop.returncode == 0 and json.loads(stop.stdout)["stopped"], stop.stderr
        assert child.wait(timeout=5) == 0, child.stdout.read()
        assert not is_running(tmp_path)
        assert not (tmp_path / "data/desktop-instance.json").exists()
        assert not (tmp_path / "data/gateway.pid").exists()
    finally:
        if child.poll() is None:
            request_stop(tmp_path, timeout=10)
            if child.poll() is None:
                child.terminate()  # Only the temporary test process is cleaned up.
            child.wait(timeout=5)
        child.stdout.close()
