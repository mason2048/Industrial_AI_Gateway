"""Business behavior for on-demand current questions and filtered AI evidence."""
from datetime import datetime
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.ai_api import LocalDataProvider, router
from backend.models import AIQuery


def snapshot(items, connected=True):
    return {"items": items, "connected": connected, "mode": "opcua", "connection_id": "plant-a",
            "good": len(items), "total": len(items), "storage_error": ""}


def point(**changes):
    return {"id": 1, "revision": 1, "name": "真空压力", "device": "真空泵", "type": "FLOAT",
            "unit": "Pa", "save": False, "threshold": .00005, "precision": 5,
            "ai_description": "腔体绝对压力", "value": .00006, "quality": "Good",
            "timestamp": "2026-10-02T01:00:00+00:00", **changes}


def history_rows(values, *, tag_id=1, revision=1, kind="FLOAT", quality="Good"):
    return [{"id": i + 1, "tag_id": tag_id, "tag_revision": revision,
             "source": "opcua", "connection_id": "plant-a", "data_type": kind,
             "device": "真空泵", "name": "真空压力", "unit": "Pa",
             "value": value, "quality": quality,
             "timestamp": f"2026-10-02T01:00:{i:02d}+00:00"}
            for i, value in enumerate(values)]


def reader(rows, calls=None):
    def query(**filters):
        if calls is not None:
            calls.append(filters)
        limit, offset = filters.get("limit", 2000), filters.get("offset", 0)
        return {"items": rows[offset:offset + limit], "total": len(rows), "has_more": offset + limit < len(rows),
                "limit": limit, "offset": offset, "start": filters.get("start") or "2026-10-01T00:00:00+00:00",
                "end": filters.get("end") or "2026-10-02T01:00:00+00:00", "summary": [], "note": "全范围统计"}
    return query


def test_unsaved_point_current_question_never_reads_history():
    def unexpected_history(**filters):
        raise AssertionError("Current questions must not depend on history storage")
    provider = LocalDataProvider(lambda device=None: snapshot([point()]), unexpected_history)
    for question in ("当前真空压力多少", "真空压力多少"):
        result = provider.analyze(AIQuery(question=question))
        assert result["query_type"] == "current"
        assert "0.00006" in result["answer"]
        assert result["items"][0]["timestamp"] == point()["timestamp"]
        assert result["items"][0]["ai_description"] == "腔体绝对压力"
        assert not result["plc_write_allowed"]


def test_bad_quality_current_is_unavailable_and_does_not_claim_a_value():
    provider = LocalDataProvider(lambda device=None: snapshot([point(quality="Stale", value=None)], False), reader([]))
    result = provider.analyze(AIQuery(question="现在真空压力多少"))
    assert not result["connected"]
    assert "Stale" in result["answer"] and "0.00006" not in result["answer"]


def test_recent_week_uses_seven_days_instead_of_default_day():
    calls = []
    provider = LocalDataProvider(lambda device=None: snapshot([point()]), reader([], calls))
    result = provider.analyze(AIQuery(question="最近一周真空压力变化"))
    assert result["query_type"] == "history"
    filters = calls[0]
    delta = datetime.fromisoformat(filters["end"]) - datetime.fromisoformat(filters["start"])
    assert delta.total_seconds() == 7 * 86400


def test_threshold_filter_compares_retained_baseline_and_paginates_after_filter():
    rows = history_rows([1.0, 1.00002, 1.00004, 1.00005, 1.00006, 1.00010])
    calls = []
    provider = LocalDataProvider(lambda device=None: snapshot([point()]), reader(rows, calls))
    result = provider.history_data(changed_only=True, limit=1, offset=1)
    assert [r["value"] for r in result["items"]] == [1.00005]
    assert result["items"][0]["change_reason"] == "threshold_change"
    assert result["items"][0]["ai_description"] == "腔体绝对压力"
    assert result["total"] == 3 and result["has_more"] and result["next_offset"] == 2
    assert result["filter"]["raw_total"] == 6 and result["filter"]["complete"]
    assert result["summary_scope"] == "all_saved_samples_in_range"


def test_bool_quality_changes_and_old_definition_threshold_are_preserved():
    rows = history_rows([False, False, True, True], revision=2, kind="BOOL")
    rows[3].update(value=None, quality="BadCommunicationError")
    provider = LocalDataProvider(lambda device=None: snapshot([point()]), reader(rows),
                                 lambda tag_id, revision: point(type="BOOL", threshold=999, ai_description="旧版开关"))
    result = provider.history_data(changed_only=True)
    assert [r["change_reason"] for r in result["items"]] == ["baseline", "state_change", "quality_change"]
    assert result["items"][1]["ai_description"] == "旧版开关"
    assert not result["filter"]["unknown_definitions"]


def test_filter_scan_cap_and_unknown_definition_cannot_claim_complete_interval():
    rows = history_rows([1.0] * 5, tag_id=99)
    provider = LocalDataProvider(lambda device=None: snapshot([point()]), reader(rows))
    provider.HISTORY_SCAN_LIMIT = 3
    result = provider.history_data(changed_only=True)
    assert result["truncated"] and not result["filter"]["complete"]
    assert result["filter"]["scanned"] == 3 and result["filter"]["raw_total"] == 5
    assert result["total"] == 3
    assert result["filter"]["unknown_definitions"] == [{"tag_id": 99, "tag_revision": 1}]
    assert "结果不完整" in result["note"] and "全部保留" in result["note"]


def test_filtered_ai_http_endpoint_keeps_default_raw_query_compatible():
    rows = history_rows([1.0, 1.00001, 1.00005])
    provider = LocalDataProvider(lambda device=None: snapshot([point()]), reader(rows))
    app = FastAPI()
    app.include_router(router(provider))
    with TestClient(app) as client:
        raw = client.get("/api/ai/history?changed_only=false").json()
        filtered = client.get("/api/ai/history?changed_only=true&limit=1&offset=1").json()
        assert raw["total"] == 3
        assert filtered["total"] == 2 and filtered["items"][0]["value"] == 1.00005
        assert filtered["items"][0]["quality"] == "Good"
        assert client.post("/api/ai/write", json={"tag_id": 1, "value": 1}).status_code == 404
