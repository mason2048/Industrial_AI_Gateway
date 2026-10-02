"""Opt-in model integration uses fake local HTTP services and isolated data only."""
from contextlib import contextmanager
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import stat
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from backend.ai_api import LocalDataProvider, router
from backend.llm import AIConfigRequest, AIConfigStore, AISettings, ModelError, ModelService
from backend.main import create_app
from backend.maintenance import create_backup
from backend.models import AIQuery
from backend.observability import Redactor
from tests.test_ai_basic import history_rows, point, reader, snapshot


@contextmanager
def fake_model(*, body=None, status=200, delay=0, chunk_delay=0):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            incoming = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "body": incoming, "authorization": self.headers.get("Authorization")})
            time.sleep(delay)
            result = body if body is not None else ({"message": {"content": "模拟模型回答：只读数据已检查。"}} if self.path.endswith("/api/chat")
                                                    else {"choices": [{"message": {"content": "模拟模型回答：只读数据已检查。"}}]})
            raw = result if isinstance(result, bytes) else json.dumps(result).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if chunk_delay:
                    for index in range(0, len(raw), 2):
                        self.wfile.write(raw[index:index + 2])
                        self.wfile.flush()
                        time.sleep(chunk_delay)
                else:
                    self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def local_service(tmp_path):
    store = AIConfigStore(tmp_path)
    rows = history_rows([1.0, 1.00001, 1.00005, 1.00006, 1.00010])
    provider = LocalDataProvider(lambda device=None: snapshot([point()]), reader(rows))
    service = ModelService(store, provider, Redactor(["fake-operator-pin"]))
    events = []
    state = SimpleNamespace(operator_pin="fake-operator-pin", operations=SimpleNamespace(record=lambda *args: events.append(args)))
    app = FastAPI()
    app.include_router(router(provider, service, state))
    return service, TestClient(app), events


def headers(client):
    response = client.get("/api/ai/config")
    return {"X-Operator-Pin": "fake-operator-pin", "If-Match": response.headers["ETag"]}


def save(client, url, *, provider="ollama", key="", **extra):
    response = client.post("/api/ai/config", headers=headers(client), json={"provider": provider, "base_url": url,
                           "model": "mock-model", "api_key": key, **extra})
    assert response.status_code == 200, response.text
    return response


def test_default_rules_remain_opt_in_and_pin_free(local_service):
    service, client, events = local_service
    assert service.store.public()["provider"] == "local_rules"
    result = client.post("/api/ai/query", json={"question": "当前真空压力多少"})
    assert result.status_code == 200 and result.json()["provider"] == "local_rules"
    assert result.json()["query_type"] == "current" and "0.00006" in result.json()["answer"]
    assert not events


def test_ai_configuration_requires_pin_revision_and_restarts_independently(local_service):
    service, client, events = local_service
    body = {"provider": "ollama", "model": "mock-model"}
    assert client.post("/api/ai/config", json=body).status_code == 403
    assert client.post("/api/ai/config", json=body, headers={"X-Operator-Pin": "fake-operator-pin"}).status_code == 428
    old_headers = headers(client)
    result = client.post("/api/ai/config", json=body, headers=old_headers)
    assert result.status_code == 200 and result.headers["ETag"] == '"ai-2"'
    assert client.post("/api/ai/config", json=body, headers=old_headers).status_code == 412
    restored = AIConfigStore(service.store.root)
    assert restored.public()["model"] == "mock-model" and restored.public()["revision"] == 2
    assert events == [("ai_configuration_changed", {"provider": "ollama", "revision": 2})]


@pytest.mark.parametrize("provider,path_suffix", [("ollama", "/api/chat"), ("openai_compatible", "/v1/chat/completions")])
def test_both_protocols_current_qa_are_authorized_readonly_and_bounded(local_service, provider, path_suffix):
    service, client, events = local_service
    def no_history(**kwargs):
        raise AssertionError("Realtime-only question must not read history")
    service.data_provider.history = no_history
    with fake_model() as (url, calls):
        base = url + ("/v1" if provider == "openai_compatible" else "")
        save(client, base, provider=provider, key="mock-secret", max_output_tokens=256)
        assert not calls  # Saving configuration never performs inference.
        assert client.post("/api/ai/query", json={"question": "当前真空压力多少"}).status_code == 403
        result = client.post("/api/ai/query", headers=headers(client), json={"question": "当前真空压力多少"})
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["provider"] == provider and data["model"] == "mock-model"
        assert data["query_type"] == "current" and data["evidence_count"] == 1
        assert data["context"]["current_count"] == 1 and data["context"]["history_count"] == 0
        assert data["evidence"]["current_items"][0]["quality"] == "Good"
        assert data["evidence"]["current_items"][0]["timestamp"] == point()["timestamp"]
        assert not data["plc_write_allowed"] and not data["truncated"]
        call = calls[0]
        assert call["path"] == path_suffix and call["authorization"] == "Bearer mock-secret"
        payload = call["body"]
        assert "tools" not in payload and "functions" not in payload and payload["stream"] is False
        evidence = json.loads(payload["messages"][1]["content"])["read_only_evidence"]
        assert evidence["source"] == "opcua" and evidence["connection_id"] == "plant-a"
        assert evidence["current_items"][0]["ai_description"] == "腔体绝对压力"
        assert evidence["current_items"][0]["quality"] == "Good"
        if provider == "ollama":
            assert payload["keep_alive"] == 0 and payload["options"]["num_predict"] == 256
        else:
            assert payload["max_tokens"] == 256


def test_model_history_sends_only_threshold_kept_evidence(local_service):
    service, client, _ = local_service
    with fake_model() as (url, calls):
        save(client, url)
        result = client.post("/api/ai/query", headers=headers(client), json={"question": "最近一周真空压力趋势"})
    assert result.status_code == 200
    data = result.json()
    evidence = json.loads(calls[0]["body"]["messages"][1]["content"])["read_only_evidence"]
    assert [item["value"] for item in evidence["history_items"]] == [1.0, 1.00005, 1.00010]
    assert [item["change_reason"] for item in evidence["history_items"]] == ["baseline", "threshold_change", "threshold_change"]
    assert "summary" not in evidence
    assert data["context"]["history_count"] == 3 and data["context"]["history_raw_total"] == 5
    assert data["query_type"] == "history" and not data["plc_write_allowed"]


def test_candidate_test_is_pin_gated_nonpersisting_and_has_no_process_data(local_service):
    service, client, events = local_service
    before = service.store.path.read_bytes()
    with fake_model() as (url, calls):
        body = {"provider": "ollama", "base_url": url, "model": "mock-model", "api_key": "candidate-only-secret"}
        assert client.post("/api/ai/test", json=body).status_code == 403
        result = client.post("/api/ai/test", json=body, headers={"X-Operator-Pin": "fake-operator-pin"})
        assert result.status_code == 200 and result.json()["success"]
        assert calls[0]["authorization"] == "Bearer candidate-only-secret"
        content = str(calls[0]["body"])
        assert "read_only_evidence" not in content and "真空" not in content and "opcua" not in content
        assert service.store.path.read_bytes() == before and not events
        assert not service.store.secrets_path.exists()


def test_keys_are_private_redacted_and_do_not_follow_changed_target(local_service):
    service, client, _ = local_service
    with fake_model(body={"message": {"content": "mock-private-secret fake-operator-pin"}}) as (url, calls):
        saved = save(client, url, key="mock-private-secret")
        assert saved.json()["api_key_set"] and "mock-private-secret" not in saved.text
        private = list(service.store.secrets_path.glob("key-*.json"))
        assert len(private) == 1
        if os.name != "nt":
            assert stat.S_IMODE(private[0].stat().st_mode) == 0o600
        assert "mock-private-secret" not in service.store.path.read_text()
        restarted = AIConfigStore(service.store.root)
        assert restarted.snapshot()[1] == "mock-private-secret"
        save(client, url)  # Empty API key preserves the same target's key.
        assert service.store.snapshot()[1] == "mock-private-secret"
        result = client.post("/api/ai/query", headers=headers(client), json={"question": "当前真空压力多少"})
        assert "mock-private-secret" not in result.text and "fake-operator-pin" not in result.text
        assert "[REDACTED]" in result.json()["answer"]
        candidate = {"provider": "openai_compatible", "base_url": url + "/v1", "model": "new-model"}
        client.post("/api/ai/test", headers=headers(client), json=candidate)
        assert calls[-1]["authorization"] is None
        changed = save(client, url + "/v1", provider="openai_compatible")
        assert not changed.json()["api_key_set"] and service.store.snapshot()[1] == ""
        save(client, url + "/v1", provider="openai_compatible", key="other-key")
        cleared = client.post("/api/ai/config", headers=headers(client), json={"clear_api_key": True})
        assert cleared.status_code == 200 and not cleared.json()["api_key_set"]
        assert len(list(service.store.secrets_path.glob("key-*.json"))) == 1


@pytest.mark.parametrize("url", ["http://u:p@localhost:11434", "http://localhost:11434?key=secret",
                                 "http://localhost:11434#secret", "ftp://localhost/model", "http://public.example/model",
                                 "http://localhost:11434/api/chat", "https://api.example/v1/chat/completions"])
def test_model_urls_reject_credentials_bad_schemes_and_wrong_endpoint_paths(url):
    with pytest.raises(ValueError):
        AISettings(base_url=url)


def test_local_lan_public_destination_labels():
    assert AIConfigStore.public_settings(AISettings(), "")["deployment"] == "local_rules"
    assert AIConfigStore.public_settings(AISettings(provider="ollama", base_url="http://192.168.1.8:11434"), "")["deployment"] == "local_network"
    assert AIConfigStore.public_settings(AISettings(provider="openai_compatible", base_url="https://api.example/v1"), "")["deployment"] == "remote_server"


@pytest.mark.parametrize("body,status,expected", [(b"not json", 200, 502), ({"message": {"content": ""}}, 200, 502),
                                                  ({"error": "mock-private-secret"}, 200, 502),
                                                  ({"error": "mock-private-secret"}, 401, 502),
                                                  ({"error": "mock-private-secret"}, 429, 429),
                                                  ({"error": "mock-private-secret"}, 302, 502)])
def test_failures_are_explicit_and_never_echo_provider_error_body(local_service, body, status, expected):
    service, client, _ = local_service
    with fake_model(body=body, status=status) as (url, calls):
        save(client, url, key="mock-private-secret")
        result = client.post("/api/ai/query", headers=headers(client), json={"question": "当前真空压力多少"})
    assert result.status_code == expected
    assert "detail" in result.json() and "answer" not in result.json()
    assert "mock-private-secret" not in result.text


def test_timeout_is_absolute_and_sanitized(local_service, monkeypatch):
    service, client, _ = local_service
    original = httpx.AsyncClient.stream
    def fail_stream(*args, **kwargs):
        raise httpx.ReadTimeout("sensitive provider or secret detail")
    monkeypatch.setattr(httpx.AsyncClient, "stream", fail_stream)
    save(client, "http://127.0.0.1:11434")
    result = client.post("/api/ai/query", headers=headers(client), json={"question": "当前真空压力多少"})
    assert result.status_code == 504 and "超时" in result.json()["detail"]
    assert "sensitive" not in result.text
    monkeypatch.setattr(httpx.AsyncClient, "stream", original)


def test_oversized_response_is_rejected(local_service):
    service, client, _ = local_service
    with fake_model(body=b"x" * (1048576 + 1)) as (url, calls):
        save(client, url)
        result = client.post("/api/ai/query", headers=headers(client), json={"question": "当前真空压力多少"})
    assert result.status_code == 502 and "响应过大" in result.json()["detail"]


def test_concurrency_limit_protects_query_and_test(local_service):
    service, client, _ = local_service
    save(client, "http://127.0.0.1:11434")
    service.slot.acquire()
    try:
        result = client.post("/api/ai/query", headers=headers(client), json={"question": "当前真空压力多少"})
        assert result.status_code == 429
        assert client.post("/api/ai/test", headers=headers(client), json={}).status_code == 429
    finally:
        service.slot.release()


def test_context_bounds_truncation_quality_and_connection_separation(local_service):
    service, client, _ = local_service
    points = [point(id=n, name=f"压力{n}", quality="Stale", value=None, ai_description="x"*2000) for n in range(80)]
    service.data_provider.current = lambda device=None: snapshot(points, False)
    context, metadata, truncated = service._context(AIQuery(question="现在全部数据"))
    assert len(context["current_items"]) == 50 and truncated
    assert metadata["current_total"] == 80 and context["warnings"]
    assert all(p["quality"] == "Stale" and p["value"] is None and len(p["ai_description"]) == 256 for p in context["current_items"])
    assert len(json.dumps(context, ensure_ascii=False)) < 40000
    old, _, _ = service._context(AIQuery(question="现在全部数据", connection_id="old-connection"))
    assert old["current_items"] == []


def test_history_context_scan_and_sample_limits_are_disclosed(local_service):
    service, client, _ = local_service
    service.data_provider.history = reader(history_rows([float(x) for x in range(500)]))
    service.CONTEXT_LIMITS = {**service.CONTEXT_LIMITS, "history_samples": 2, "history_scan_samples": 4}
    context, metadata, truncated = service._context(AIQuery(question="历史真空压力趋势"))
    assert len(context["history_items"]) == 2 and metadata["history_raw_total"] == 500
    assert not metadata["history_scan_complete"] and truncated and context["warnings"]


def test_ai_config_secrets_excluded_from_backups_and_plc_unchanged(tmp_path):
    for folder in ("config", "frontend", "data"):
        (tmp_path / folder).mkdir()
    (tmp_path / "config/config.json").write_text(json.dumps({"mode": "simulation", "backup_enabled": False}))
    (tmp_path / "frontend/index.html").write_text("<title>test</title>")
    app = create_app(tmp_path)
    with TestClient(app) as client:
        before = app.state.config_store.snapshot()
        config = client.get("/api/ai/config")
        saved = client.post("/api/ai/config", headers={"X-Operator-Pin": app.state.operator_pin, "If-Match": config.headers["ETag"]},
                            json={"provider": "ollama", "model": "mock-model", "api_key": "private-backup-test-key"})
        assert saved.status_code == 200
        assert app.state.config_store.snapshot() == before
        backup = create_backup(tmp_path)
        for path in backup.rglob("*"):
            if path.is_file():
                assert b"private-backup-test-key" not in path.read_bytes()
        diagnostics = client.get("/api/diagnostics").text
        assert "private-backup-test-key" not in diagnostics
        assert "private-backup-test-key" not in (tmp_path / "data/logs/gateway.log").read_text()


def test_model_query_requires_current_config_version(local_service):
    service, client, _ = local_service
    with fake_model() as (url, calls):
        save(client, url)
        pin_only = {"X-Operator-Pin": "fake-operator-pin"}
        assert client.post("/api/ai/query", headers=pin_only, json={"question": "当前真空压力多少"}).status_code == 428
        stale = headers(client)
        save(client, url, model="new-mock-model")
        rejected = client.post("/api/ai/query", headers=stale, json={"question": "当前真空压力多少"})
        assert rejected.status_code == 412 and not calls


@pytest.mark.parametrize("model,thinking", [("qwen3:4b", False), ("vendor/qwen3:8b", False), ("qwen2.5:7b", None), ("gpt-oss:20b", None)])
def test_qwen3_thinking_control_does_not_change_other_families(local_service, model, thinking):
    service, client, _ = local_service
    with fake_model() as (url, calls):
        save(client, url, model=model)
        result = client.post("/api/ai/query", headers=headers(client), json={"question": "当前真空压力多少"})
        assert result.status_code == 200
        assert calls[0]["body"].get("think") is thinking


def test_thinking_only_response_is_not_a_final_answer(local_service):
    service, client, _ = local_service
    with fake_model(body={"message": {"content": "", "thinking": "unfinished chain"}}) as (url, calls):
        save(client, url)
        result = client.post("/api/ai/query", headers=headers(client), json={"question": "当前真空压力多少"})
        assert result.status_code == 502 and "只返回思考过程" in result.json()["detail"]
        assert "unfinished chain" not in result.text


def test_total_deadline_cancels_wait_before_provider_reply(local_service):
    service, client, _ = local_service
    # Each chunk arrives before the read timeout; only the whole-request
    # deadline prevents an indefinitely slow, continuously active provider.
    with fake_model(chunk_delay=0.02) as (url, calls):
        settings = AISettings(provider="ollama", base_url=url, model="mock-model").model_copy(update={"timeout_seconds": 0.07})
        with pytest.raises(ModelError) as error:
            service._chat(settings, "", [{"role": "user", "content": "connection test"}])
        assert error.value.status_code == 504


def test_failed_atomic_settings_commit_keeps_previous_target_and_secret(local_service, monkeypatch):
    import backend.llm as llm
    service, client, _ = local_service
    save(client, "http://127.0.0.1:11434", key="previous-secret")
    before = service.store.path.read_bytes()
    atomic_json = llm._atomic_json
    def fail_public(path, value, **kwargs):
        if path == service.store.path:
            raise OSError("simulated write failure")
        return atomic_json(path, value, **kwargs)
    monkeypatch.setattr(llm, "_atomic_json", fail_public)
    with pytest.raises(OSError):
        service.store.update(AIConfigRequest(base_url="http://127.0.0.1:11435", api_key="new-secret"), 2)
    assert service.store.path.read_bytes() == before
    assert service.store.snapshot()[1] == "previous-secret"
    assert AIConfigStore(service.store.root).snapshot()[1] == "previous-secret"
    assert not (service.store.secrets_path / "key-3.json").exists()


def test_candidate_validation_does_not_echo_url_credentials(local_service):
    service, client, _ = local_service
    with pytest.raises(ValueError) as error:
        service.store.candidate(AIConfigRequest(base_url="http://user:secret-not-to-echo@localhost:11434"))
    assert "secret-not-to-echo" not in str(error.value)
