"""Clearing an installation must remain empty after restart, without losing history."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.models import Tag
from backend.tag_manager import export_excel, import_excel, validate_tags


@pytest.fixture
def installation(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "frontend/index.html").write_text("empty table audit", encoding="utf-8")
    (tmp_path / "config/config.json").write_text(json.dumps({
        "mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4840",
        "poll_interval": .1, "backup_enabled": False,
    }), encoding="utf-8")
    point = Tag(id=1, address="A1", name="Original point", type="FLOAT", save=False)
    (tmp_path / "data/tags.xlsx").write_bytes(export_excel([point]))
    return tmp_path


def management(client, revision=None):
    headers = {"X-Operator-Pin": client.app.state.operator_pin}
    if revision is not None:
        headers["If-Match"] = revision
    return headers


def test_empty_excel_round_trip_and_capacity_limit():
    assert validate_tags([]) == []
    assert import_excel(export_excel([])) == []
    points = [Tag(id=i, address=f"A{i}", name=f"Point {i}", type="WORD")
              for i in range(1, 1002)]
    assert len(validate_tags(points[:1000])) == 1000
    with pytest.raises(ValueError, match="0至1000"):
        validate_tags(points)


def test_delete_last_point_preserves_history_and_id_identity(installation):
    with TestClient(create_app(installation)) as client:
        before = client.get("/api/tags")
        assert len(before.json()) == 1
        db = client.app.state.db
        db.insert_history([(1, time.time(), 3.5, "Good", "simulation", "真空泵01",
                            "Original point", "-", "FLOAT")])
        # Clearing points remains a protected mutation, including its revision.
        assert client.put("/api/tags", json=[]).status_code == 403
        assert client.put("/api/tags", json=[], headers=management(client)).status_code == 428
        deleted = client.put("/api/tags", json=[], headers=management(client, before.headers["ETag"]))
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["count"] == 0
        assert client.get("/api/tags").json() == []
        assert client.get("/api/current").json()["items"] == []
        assert client.get("/api/tags/next-id").json() == {"next_id": 2}
        with db.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 1
            assert conn.execute("SELECT active FROM tag_info WHERE id=1").fetchone()[0] == 0
        stale = client.put("/api/tags", json=[], headers=management(client, before.headers["ETag"]))
        assert stale.status_code == 412


def test_empty_excel_preview_and_confirmation_are_atomic(installation):
    content = export_excel([])
    with TestClient(create_app(installation)) as client:
        before = client.get("/api/tags")
        preview = client.post("/api/tags/import?dry_run=true",
                              files={"file": ("empty.xlsx", content)}, headers=management(client))
        assert preview.status_code == 200, preview.text
        assert preview.json()["count"] == 0
        assert preview.json()["diff"]["removed"] == [{"id": 1, "name": "Original point"}]
        assert preview.headers["ETag"] == before.headers["ETag"]
        assert client.get("/api/tags").json() == before.json()
        applied = client.post("/api/tags/import", files={"file": ("empty.xlsx", content)},
                              headers=management(client, preview.headers["ETag"]))
        assert applied.status_code == 200, applied.text
        assert client.get("/api/tags").json() == []
        exported = client.get("/api/tags/export")
        assert exported.status_code == 200
        assert import_excel(exported.content) == []


@pytest.mark.parametrize("clear_with", ["api", "excel"])
def test_empty_table_survives_restart_without_restoring_old_excel(installation, clear_with):
    original_excel = (installation / "data/tags.xlsx").read_bytes()
    with TestClient(create_app(installation)) as client:
        before = client.get("/api/tags")
        headers = management(client, before.headers["ETag"])
        if clear_with == "api":
            deleted = client.put("/api/tags", json=[], headers=headers)
        else:
            deleted = client.post("/api/tags/import", files={"file": ("empty.xlsx", export_excel([]))},
                                  headers=headers)
        assert deleted.status_code == 200, deleted.text
        saved_revision = client.get("/api/tags").headers["ETag"]
    assert (installation / "data/tags.xlsx").read_bytes() == original_excel
    with TestClient(create_app(installation)) as restarted:
        assert restarted.get("/api/tags").json() == []
        assert restarted.get("/api/tags").headers["ETag"] == saved_revision
        assert restarted.get("/api/tags/next-id").json() == {"next_id": 2}
        assert restarted.get("/api/current").json()["items"] == []


def test_first_installation_still_imports_excel_once(installation):
    with TestClient(create_app(installation)) as client:
        initial = client.get("/api/tags")
        assert initial.status_code == 200
        assert [row["name"] for row in initial.json()] == ["Original point"]
        assert initial.headers["ETag"] == '"tags-1"'
    with TestClient(create_app(installation)) as restarted:
        assert restarted.get("/api/tags").json() == initial.json()
        assert restarted.get("/api/tags").headers["ETag"] == initial.headers["ETag"]
