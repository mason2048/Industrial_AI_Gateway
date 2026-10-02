"""Deleted point IDs stay bound to their original history; all roots are temporary."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.database import Database
from backend.main import create_app
from backend.models import Tag


def point(tag_id, **changes):
    return Tag(id=tag_id, address=f"A{tag_id}", name=f"Point {tag_id}",
               type="FLOAT", save=False, **changes)


def dump(db):
    with db.connect() as conn:
        return tuple(conn.iterdump())


def test_next_id_in_empty_database_is_read_only_and_does_not_reserve(tmp_path):
    db = Database(tmp_path / "history.db")
    before = dump(db)
    assert db.next_tag_id() == (1, 0)
    assert db.next_tag_id() == (1, 0)
    assert dump(db) == before


@pytest.mark.parametrize("highest", [9, 10])
def test_next_id_includes_deleted_highest_ids_after_cleanup_and_restart(tmp_path, highest):
    path = tmp_path / "history.db"
    db = Database(path)
    original = [point(tag_id) for tag_id in range(1, highest + 1)]
    db.replace_tags(original)
    db.insert_history([(highest, time.time() - 8 * 86400, 12.5, "Good", "simulation",
                        "真空泵01", f"Point {highest}", "-", "FLOAT")])
    db.replace_tags(original[:7])
    assert max(row["id"] for row in db.tags()) == 7
    # The former UI algorithm tries ID 8, which is permanently bound to A8.
    with pytest.raises(ValueError, match="ID 8永久绑定"):
        db.replace_tags([*original[:7], point(8, device="New device")])
    before = dump(db)
    assert db.next_tag_id() == (highest + 1, 2)
    assert dump(db) == before
    assert db.cleanup(time.time() - 7 * 86400) == 1
    reopened = Database(path)
    assert reopened.next_tag_id() == (highest + 1, 2)
    with reopened.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tag_identity").fetchone()[0] == highest


def test_new_id_does_not_rebind_deleted_point_history(tmp_path):
    db = Database(tmp_path / "history.db")
    original = [point(tag_id) for tag_id in range(1, 10)]
    db.replace_tags(original)
    now = time.time()
    db.insert_history([(9, now, 12.5, "Good", "simulation", "真空泵01",
                        "Point 9", "-", "FLOAT")])
    db.replace_tags(original[:7])
    next_id, revision = db.next_tag_id()
    assert next_id == 10
    incoming = Tag(id=next_id, address="New address", name="New point", type="FLOAT", save=False)
    db.replace_tags([*original[:7], incoming], expected_revision=revision)
    db.insert_history([(10, now + 1, 77.5, "Good", "simulation", "真空泵01",
                        "New point", "-", "FLOAT")])
    assert db.tag_definition(9, 1)["address"] == "A9"
    assert db.tag_definition(10, 1)["address"] == "New address"
    with db.connect() as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT tag_id,value,name FROM history_data ORDER BY id")] == [
                (9, 12.5, "Point 9"), (10, 77.5, "New point")]
        assert conn.execute("SELECT active FROM tag_info WHERE id=9").fetchone()[0] == 0


def test_maximum_id_reports_exhaustion_without_writing(tmp_path):
    db = Database(tmp_path / "history.db")
    db.replace_tags([point(2147483647)])
    before = dump(db)
    with pytest.raises(ValueError, match="最大值"):
        db.next_tag_id()
    assert dump(db) == before


@pytest.fixture
def client(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/index.html").write_text("test", encoding="utf-8")
    (tmp_path / "config/config.json").write_text(json.dumps({
        "mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4840",
        "poll_interval": .1, "backup_enabled": False,
    }), encoding="utf-8")
    with TestClient(create_app(tmp_path)) as value:
        yield value


def manage(client, revision):
    return {"X-Operator-Pin": client.app.state.operator_pin, "If-Match": revision}


def test_next_id_api_returns_current_etag_without_management_pin(client):
    empty = client.get("/api/tags/next-id")
    assert empty.status_code == 200
    assert empty.json() == {"next_id": 1}
    assert empty.headers["ETag"] == client.get("/api/tags").headers["ETag"] == '"tags-0"'
    original = [point(tag_id).model_dump() for tag_id in range(1, 10)]
    saved = client.put("/api/tags", json=original, headers=manage(client, empty.headers["ETag"]))
    assert saved.status_code == 200, saved.text
    deleted = client.put("/api/tags", json=original[:7], headers=manage(client, saved.headers["ETag"]))
    assert deleted.status_code == 200, deleted.text
    candidate = client.get("/api/tags/next-id")
    assert candidate.json() == {"next_id": 10}
    assert candidate.headers["ETag"] == deleted.headers["ETag"]
    assert client.get("/api/tags/next-id").json() == candidate.json()
    assert client.get("/api/tags").headers["ETag"] == candidate.headers["ETag"]
    reusing_retired = [*original[:7], point(8, device="Changed device").model_dump()]
    denied = client.put("/api/tags", json=reusing_retired,
                        headers=manage(client, candidate.headers["ETag"]))
    assert denied.status_code == 400
    assert "永久绑定" in denied.json()["detail"]


def test_next_id_is_a_hint_and_stale_saves_still_conflict(client):
    first = client.get("/api/tags/next-id")
    second = client.get("/api/tags/next-id")
    assert first.json() == second.json() == {"next_id": 1}
    candidate = [point(first.json()["next_id"]).model_dump()]
    # Reading the candidate ID does not grant management permission.
    assert client.put("/api/tags", json=candidate,
                      headers={"If-Match": first.headers["ETag"]}).status_code == 403
    assert client.put("/api/tags", json=candidate,
                      headers={"X-Operator-Pin": client.app.state.operator_pin}).status_code == 428
    saved = client.put("/api/tags", json=candidate, headers=manage(client, first.headers["ETag"]))
    assert saved.status_code == 200, saved.text
    stale = client.put("/api/tags", json=[point(1, device="Other device").model_dump()],
                       headers=manage(client, second.headers["ETag"]))
    assert stale.status_code == 412
    assert client.get("/api/tags/next-id").json() == {"next_id": 2}
    assert client.get("/api/tags").json()[0]["device"] == "真空泵01"
    # Configuration keeps its own revision scope after ID queries.
    config = client.get("/api/config")
    body = {"mode": "simulation", "endpoint": config.json()["endpoint"], "poll_interval": .2}
    updated = client.post("/api/connection", json=body, headers=manage(client, config.headers["ETag"]))
    assert updated.status_code == 200, updated.text
    assert client.post("/api/connection", json=body,
                       headers=manage(client, config.headers["ETag"])).status_code == 412
    assert client.post("/api/connection", json=body,
                       headers=manage(client, saved.headers["ETag"])).status_code == 412
