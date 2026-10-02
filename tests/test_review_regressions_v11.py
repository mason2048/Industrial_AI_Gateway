"""Independent review regressions; all application roots are disposable."""

import json
import os
import threading
import time

import anyio
import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.models import Tag
from backend.tag_manager import export_excel
from scripts.runtime import AlreadyRunning, ProcessLock, is_running


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def review_root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/index.html").write_text("review", encoding="utf-8")
    (tmp_path / "config/config.json").write_text(json.dumps({
        "mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4840",
        "poll_interval": .1, "batch_size": 100, "heartbeat_seconds": 1800,
        "retention_days": 7, "backup_enabled": False,
    }), encoding="utf-8")
    (tmp_path / "data/tags.xlsx").write_bytes(export_excel([
        Tag(id=1, address="A1", name="Pressure", type="WORD", device="Review")
    ]))
    return tmp_path


def test_partial_startup_failure_stops_already_started_workers(review_root, monkeypatch):
    app = create_app(review_root)

    def fail_operations_start():
        raise RuntimeError("injected startup failure")

    monkeypatch.setattr(app.state.operations, "start", fail_operations_start)
    try:
        with pytest.raises(RuntimeError, match="injected startup failure"):
            with TestClient(app):
                pass
        gateway = app.state.gateway
        workers = (gateway.thread, gateway.writer._thread, gateway.writer._cleanup_thread)
        assert not any(worker and worker.is_alive() for worker in workers), (
            "Startup failed after acquisition began, but workers are still running outside ProcessLock"
        )
    finally:
        app.state.operations.stop(.5)
        app.state.gateway.stop(2)


def test_unsaved_preflight_credentials_are_redacted(review_root, monkeypatch):
    app = create_app(review_root)
    candidate_user = "review-user-never-saved"
    candidate_secret = "review-secret-never-saved-9472"
    monkeypatch.setenv("REVIEW_PREFLIGHT_PASSWORD", candidate_secret)

    def fail_with_candidate(config, root):
        # Client/server diagnostics can include credential text in exceptions.
        raise OSError(f"preflight rejected {config['username']} {os.environ[config['password_env']]}")

    monkeypatch.setattr("backend.routes.connection_test", fail_with_candidate)
    with TestClient(app) as client:
        response = client.post("/api/connection/test", headers={"X-Operator-Pin": app.state.operator_pin},
                               json={"mode": "opcua", "endpoint": "opc.tcp://127.0.0.1:4840",
                                     "username": candidate_user, "password_env": "REVIEW_PREFLIGHT_PASSWORD"})
        assert response.status_code == 502
        assert candidate_secret not in response.text and candidate_user not in response.text


def test_health_does_not_need_a_database_request_thread(review_root):
    app = create_app(review_root)
    entered, release, health_done = threading.Event(), threading.Event(), threading.Event()
    responses = []

    @app.get("/api/review/blocking-database")
    def blocking_database():
        entered.set()
        release.wait(3)
        return {"done": True}

    # This route must precede the catch-all static mount for the isolated probe.
    app.router.routes.insert(0, app.router.routes.pop())

    async def limit_threads(value):
        limiter = anyio.to_thread.current_default_thread_limiter()
        old = limiter.total_tokens
        limiter.total_tokens = value
        return old

    with TestClient(app) as client:
        previous = client.portal.call(limit_threads, 1)

        def get_blocked():
            responses.append(client.get("/api/review/blocking-database"))

        def get_health():
            responses.append(client.get("/api/health"))
            health_done.set()

        occupied = threading.Thread(target=get_blocked)
        probe = threading.Thread(target=get_health)
        try:
            occupied.start()
            assert entered.wait(1)
            probe.start()
            assert health_done.wait(.3), "Read-only health is queued behind a blocked database request worker"
        finally:
            release.set()
            occupied.join(2)
            if probe.ident:
                probe.join(2)
            client.portal.call(limit_threads, previous)


def test_timeout_retains_process_lease_until_late_writer_finishes(review_root, monkeypatch):
    app = create_app(review_root)
    entered, release = threading.Event(), threading.Event()
    original_save = app.state.history.save
    original_stop = app.state.gateway.stop

    def delayed_save(*args, **kwargs):
        entered.set()
        release.wait(3)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(app.state.history, "save", delayed_save)
    monkeypatch.setattr(app.state.gateway, "stop", lambda timeout=30: original_stop(.01))
    try:
        with TestClient(app):
            assert entered.wait(2)
        assert app.state.shutdown_result["storage_stopped"] is False
        assert is_running(review_root)
        with pytest.raises(AlreadyRunning):
            with ProcessLock(review_root, reentrant=False, purpose="restore"):
                pass
        with pytest.raises(RuntimeError, match="运行"):
            create_app(review_root)
        release.set()
        wait_for(lambda: not is_running(review_root))
        with ProcessLock(review_root, reentrant=False, purpose="restore"):
            pass
    finally:
        release.set()
        original_stop(2)


def test_failed_collector_thread_start_does_not_leave_writer_unowned(review_root, monkeypatch):
    app = create_app(review_root)
    original_start = threading.Thread.start

    def fail_collector(thread, *args, **kwargs):
        if thread.name == "plc-collector":
            raise RuntimeError("injected collector Thread.start failure")
        return original_start(thread, *args, **kwargs)

    monkeypatch.setattr(threading.Thread, "start", fail_collector)
    try:
        with pytest.raises(RuntimeError, match="injected collector"):
            with TestClient(app):
                pass
        writer = app.state.gateway.writer
        wait_for(lambda: not any(thread and thread.is_alive() for thread in
                                (writer._thread, writer._cleanup_thread)), timeout=.5)
        assert not is_running(review_root)
    finally:
        # A failed start leaves a Thread object that cannot be joined on affected versions.
        if app.state.gateway.thread and app.state.gateway.thread.ident is None:
            app.state.gateway.thread = None
        app.state.operations.stop(.5)
        app.state.gateway.stop(2)
