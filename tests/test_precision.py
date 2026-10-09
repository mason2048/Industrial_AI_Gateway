"""Vacuum precision survives storage, filtering and on-demand evidence display."""
import io
import time

from openpyxl import load_workbook

from backend.ai_api import LocalDataProvider
from backend.database import Database
from backend.history import History, iso
from backend.llm import AIConfigStore, ModelService
from backend.models import AIQuery, Tag
from backend.observability import Redactor
from backend.tag_manager import export_excel, import_excel


def provider(tag, history, db, value, stamp):
    point = {**tag.model_dump(), "value": value, "quality": "Good", "timestamp": iso(stamp)}
    snapshot = lambda device=None: {"items": [point], "mode": "opcua", "connection_id": "test",
                                   "connected": True, "good": 1, "total": 1, "storage_error": ""}
    return LocalDataProvider(snapshot, history.query, db.tag_definition)


def test_six_decimal_threshold_excel_storage_statistics_and_model_evidence(tmp_path):
    tag = Tag(id=1, address="VD200", name="真空度", type="FLOAT", unit="Pa", device="Test",
              precision=6, threshold=.000001, record_changes=True)
    exported = export_excel([tag])
    assert import_excel(exported) == [tag]
    workbook = load_workbook(io.BytesIO(exported))
    assert workbook.active["M2"].value == 6
    assert workbook.active["I2"].value == .000001

    db = Database(tmp_path / "history.db")
    db.replace_tags([tag])
    history = History(db, heartbeat=1800)
    start = time.time() - 60
    values = [1.123456, 1.1234565, 1.123457]
    saved = [history.save([tag], {1: {"value": value, "quality": "Good"}}, "opcua",
                          now=start + index, connection_id="test") for index, value in enumerate(values)]
    # A subthreshold step is skipped; cumulative change from the saved baseline
    # at exactly 0.000001 is retained without binary subtraction rounding.
    assert saved == [1, 0, 1]
    with db.connect() as connection:
        stored = [row[0] for row in connection.execute("SELECT value FROM history_data ORDER BY id")]
    assert stored == [1.123456, 1.123457]

    filters = {"start": iso(start - 1), "end": iso(start + 10), "source": "opcua", "connection_id": "test"}
    detail = history.query(tag_id=1, **filters)
    assert [row["value"] for row in detail["items"]] == stored
    assert [row["precision"] for row in detail["items"]] == [6, 6]
    assert detail["summary"][0]["precision"] == 6
    assert detail["summary"][0]["minimum"] == stored[0]
    assert detail["summary"][0]["maximum"] == stored[1]
    group = history.series(1, buckets=2, **filters)["series"][0]
    assert group["precision"] == 6
    assert (group["items"][0]["minimum"], group["items"][0]["maximum"]) == tuple(stored)

    data = provider(tag, history, db, values[0], start)
    current = data.analyze(AIQuery(question="当前真空度", variable="真空度"))
    assert "1.123456 Pa" in current["answer"]
    request = AIQuery(question="历史真空度", variable="真空度", start=filters["start"], end=filters["end"])
    statistics = data.analyze(request)
    assert "最小1.123456、最大1.123457" in statistics["answer"]
    service = ModelService(AIConfigStore(tmp_path), data, Redactor([]))
    context, _, _ = service._context(request)
    assert [row["value"] for row in context["history_items"]] == stored
    assert [row["precision"] for row in context["history_items"]] == [6, 6]
    assert context["current_items"][0]["value"] == values[0]


def test_historical_precision_follows_original_definition_and_legacy_default(tmp_path):
    db = Database(tmp_path / "history.db")
    legacy = Tag(id=1, address="A", name="真空", type="FLOAT").model_dump(exclude={"precision"})
    db.replace_tags([legacy])
    history = History(db)
    old_tag = Tag.model_validate(db.tags()[0])
    start = time.time() - 60
    assert history.save([old_tag], {1: {"value": 1.12345, "quality": "Good"}}, "opcua", now=start) == 1

    db.replace_tags([old_tag.model_copy(update={"precision": 6})])
    current_tag = Tag.model_validate(db.tags()[0])
    assert current_tag.revision == 2
    assert history.save([current_tag], {1: {"value": 1.123456, "quality": "Good"}}, "opcua", now=start + 1) == 1
    filters = {"start": iso(start - 1), "end": iso(start + 10)}
    detail = history.query(tag_id=1, **filters)
    assert {(row["tag_revision"], row["precision"]) for row in detail["summary"]} == {(1, 5), (2, 6)}
    groups = history.series(1, buckets=2, **filters)["series"]
    assert {(row["tag_revision"], row["precision"]) for row in groups} == {(1, 5), (2, 6)}
    assert "precision" not in db.tag_definition(1, 1)
    assert [row["value"] for row in detail["items"]] == [1.12345, 1.123456]


def test_nonzero_readings_below_display_precision_do_not_become_zero(tmp_path):
    tag = Tag(id=1, address="A", name="真空", type="FLOAT", precision=6)
    db = Database(tmp_path / "history.db")
    db.replace_tags([tag])
    history = History(db)
    start = time.time() - 60
    assert history.save([tag], {1: {"value": .0000001, "quality": "Good"}}, "opcua", now=start, connection_id="test") == 1
    data = provider(tag, history, db, .0000001, start)
    current = data.analyze(AIQuery(question="当前真空", variable="真空"))
    historical = data.analyze(AIQuery(question="历史真空", variable="真空", start=iso(start - 1), end=iso(start + 1)))
    assert "1.000000e-07" in current["answer"]
    assert "最小1.000000e-07、最大1.000000e-07" in historical["answer"]
    assert "0.000000" not in current["answer"]
