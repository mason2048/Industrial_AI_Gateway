"""History scans freeze insertion order even when delayed samples arrive."""

from contextlib import contextmanager

import pytest

from backend.database import Database
from backend.history import History, iso
from backend.models import Tag
from backend.ai_api import LocalDataProvider


@pytest.fixture
def history(tmp_path):
    db = Database(tmp_path / "history.db")
    db.replace_tags([Tag(id=1, address="A1", name="Pressure", type="FLOAT", device="Pump")])
    return History(db)


def add(history, rows):
    history.db.insert_history([(1, timestamp, value, "Good", "opcua", "Pump",
                                "Pressure", "Pa", "FLOAT") for timestamp, value in rows])


def test_follow_up_pages_and_summary_exclude_delayed_earlier_samples(history):
    begin = 1700000000
    add(history, [(begin + index, index) for index in range(12)])
    filters = {"start": iso(begin), "end": iso(begin + 20), "source": "opcua", "limit": 5}
    first = history.query(**filters)
    assert first["total"] == 12
    assert first["snapshot_max_id"] == 12
    assert [row["value"] for row in first["items"]] == list(range(5))
    # Their timestamps are inside the frozen time window and before the next
    # page, but insertion IDs identify them as a later writer commit.
    add(history, [(begin + .1, -100), (begin + .2, 900), (begin + .3, 800)])
    selected = list(first["items"])
    for offset in (5, 10):
        following = history.query(**filters, offset=offset, snapshot_max_id=first["snapshot_max_id"])
        selected.extend(following["items"])
        assert following["total"] == first["total"]
        assert following["summary"] == first["summary"]
        assert following["snapshot_max_id"] == first["snapshot_max_id"]
    assert [row["value"] for row in selected] == list(range(12))
    assert len({row["id"] for row in selected}) == 12
    latest = history.query(**filters)
    assert latest["total"] == 15
    assert latest["snapshot_max_id"] == 15
    assert latest["summary"][0]["minimum"] == -100
    assert latest["summary"][0]["maximum"] == 900


def test_empty_snapshot_stays_empty_after_new_insert(history):
    begin = 1700000000
    filters = {"start": iso(begin), "end": iso(begin + 10)}
    empty = history.query(**filters)
    assert empty["snapshot_max_id"] == 0 and empty["total"] == 0
    add(history, [(begin + 1, 1)])
    fixed = history.query(**filters, snapshot_max_id=empty["snapshot_max_id"])
    assert fixed["items"] == [] and fixed["summary"] == [] and fixed["total"] == 0
    assert fixed["snapshot_max_id"] == 0
    assert history.query(**filters)["total"] == 1


def test_capture_boundary_count_page_and_summary_share_one_wal_snapshot(history, monkeypatch):
    begin = 1700000000
    add(history, [(begin + 1, 1)])
    original = history.db.connect
    injected = False

    class Proxy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            nonlocal injected
            result = self.conn.execute(sql, params)
            if sql.startswith("SELECT COALESCE(MAX(id),0)") and not injected:
                injected = True
                with original() as writer:
                    writer.execute("""INSERT INTO history_data
                        (tag_id,timestamp,value,quality,source,device,name,unit,data_type)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                        (1, begin + 2, 99, "Good", "opcua", "Pump", "Pressure", "Pa", "FLOAT"))
            return result

    @contextmanager
    def wrapped():
        with original() as conn:
            yield Proxy(conn)

    monkeypatch.setattr(history.db, "connect", wrapped)
    result = history.query(start=iso(begin), end=iso(begin + 10))
    assert injected
    assert result["snapshot_max_id"] == 1
    assert result["total"] == 1
    assert [row["value"] for row in result["items"]] == [1]
    assert result["summary"][0]["maximum"] == 1
    with original() as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 2


def test_cleanup_changes_count_under_same_boundary_for_scan_to_detect(history):
    begin = 1700000000
    add(history, [(begin + index, index) for index in range(6)])
    filters = {"start": iso(begin), "end": iso(begin + 10), "limit": 2}
    first = history.query(**filters)
    history.db.cleanup(begin + 2)
    following = history.query(**filters, offset=2, snapshot_max_id=first["snapshot_max_id"])
    assert following["snapshot_max_id"] == first["snapshot_max_id"]
    assert following["total"] == 4 and first["total"] == 6


def provider(history, read):
    return LocalDataProvider(lambda *_: {"items": [{"id": 1, "revision": 1, "threshold": 0,
                                                   "precision": 5, "ai_description": "Pressure"}]}, read)


def test_filtered_ai_scan_keeps_snapshot_when_early_samples_are_committed(history):
    begin = 1700000000
    add(history, [(begin + index, index) for index in range(12000)])
    queries = []

    def read(**filters):
        queries.append(filters)
        result = history.query(**filters)
        if len(queries) == 1:
            add(history, [(begin + .5, -100 - index) for index in range(1000)])
        return result

    result = provider(history, read).history_data(changed_only=True, start=iso(begin),
                                                 end=iso(begin + 12010), limit=20000)
    assert len(queries) == 2
    assert queries[1]["snapshot_max_id"] == 12000
    assert result["filter"]["complete"]
    assert result["filter"]["raw_total"] == result["total"] == 12000
    assert len({row["id"] for row in result["items"]}) == 12000
    assert [row["value"] for row in result["items"]] == list(range(12000))


def test_filtered_ai_scan_does_not_claim_completeness_after_cleanup(history):
    begin = 1700000000
    add(history, [(begin + index, index) for index in range(12000)])
    calls = 0

    def read(**filters):
        nonlocal calls
        calls += 1
        result = history.query(**filters)
        if calls == 1:
            history.db.cleanup(begin + 2500)
        return result

    result = provider(history, read).history_data(changed_only=True, start=iso(begin),
                                                 end=iso(begin + 12010), limit=20000)
    assert calls == 2
    assert not result["filter"]["complete"]
    assert result["filter"]["concurrent_change"]
    assert result["truncated"]


@pytest.mark.parametrize("boundary", [-1, True, 1.5, "1"])
def test_snapshot_boundary_requires_a_nonnegative_integer(history, boundary):
    with pytest.raises(ValueError, match="非负整数"):
        history.query(snapshot_max_id=boundary)
