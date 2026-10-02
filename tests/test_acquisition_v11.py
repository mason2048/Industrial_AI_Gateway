"""V1.1 tests use only temporary databases and deterministic local driver doubles."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from opcua import ua

from backend.database import Database, RevisionConflict, SCHEMA_VERSION
from backend.history import History, iso
from backend.models import Tag
from backend.opcua_client import Gateway, OPCUADriver
from backend.storage import HistoryWriter, SampleBatch


def tag(number=1, **changes):
    values = dict(id=number, address=f"A{number}", name=f"Point{number}",
                  type="WORD", node_id=f"ns=2;s=Point{number}", device="Pump")
    values.update(changes)
    return Tag(**values)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "history.db")
    database.replace_tags([tag()])
    return database


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "condition did not become true"


def batch(number, tags=None, connection_id="plc-one"):
    tags = tuple(tags or [tag()])
    stamp = 1700000000 + number
    return SampleBatch(tags, {point.id: {"value": number, "quality": "Good", "timestamp": iso(stamp)}
                             for point in tags}, "opcua", connection_id, stamp, float(number), f"batch-{number}")


def legacy_database(path):
    point = tag().model_dump()
    point.pop("revision")
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE tag_info(id INTEGER PRIMARY KEY,definition TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE history_data(id INTEGER PRIMARY KEY AUTOINCREMENT,tag_id INTEGER NOT NULL REFERENCES tag_info(id),
                timestamp REAL NOT NULL,value REAL,quality TEXT NOT NULL,source TEXT NOT NULL,
                device TEXT NOT NULL,name TEXT NOT NULL,unit TEXT NOT NULL,data_type TEXT NOT NULL);
            CREATE TABLE write_audit(id INTEGER PRIMARY KEY,timestamp REAL,tag_id INTEGER,mode TEXT,value TEXT,outcome TEXT);
        """)
        conn.execute("INSERT INTO tag_info VALUES(1,?,1)", (json.dumps(point),))
        conn.execute("INSERT INTO history_data VALUES(1,1,1700000000,12,'Good','opcua','Pump','Point1','-','WORD')")
        conn.execute("INSERT INTO write_audit VALUES(1,1700000000,1,'opcua','12','success')")


def test_legacy_migration_consistent_backup_and_audit(tmp_path):
    path = tmp_path / "old.db"
    legacy_database(path)
    database = Database(path)
    assert database.migration_backup
    with sqlite3.connect(database.migration_backup) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "connection_id" not in {row[1] for row in backup.execute("PRAGMA table_info(history_data)")}
        assert backup.execute("SELECT COUNT(*) FROM write_audit").fetchone()[0] == 1
    with database.connect() as conn:
        row = conn.execute("SELECT * FROM history_data").fetchone()
        assert (row["connection_id"], row["tag_revision"], row["source_timestamp"]) == ("legacy", 1, None)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert database.tags()[0]["revision"] == 1
    assert Database(path).migration_backup is None


def test_failed_migration_rolls_back_schema(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    legacy_database(path)
    original = Database._migrate

    def broken(conn):
        original(conn)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(Database, "_migrate", staticmethod(broken))
    with pytest.raises(RuntimeError, match="injected"):
        Database(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "connection_id" not in {row[1] for row in conn.execute("PRAGMA table_info(history_data)")}
        assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 1
    assert len(list(tmp_path.glob("old.pre-v2-*.sqlite3"))) == 1


def test_revision_conflict_permanent_identity_and_server_versions(db):
    revision = db.tags_revision()
    replacement = tag(name="Renamed", revision=999)
    assert db.replace_tags([replacement], expected_revision=revision) == revision+1
    assert db.tags()[0]["revision"] == 2
    with pytest.raises(RevisionConflict):
        db.replace_tags([tag(name="Lost update")], expected_revision=revision)
    assert db.tags()[0]["name"] == "Renamed"
    db.cleanup(time.time()+1000000)
    db.replace_tags([tag(2)])
    with pytest.raises(ValueError, match="永久"):
        db.replace_tags([tag(address="different")])
    assert db.tags()[0]["id"] == 2


def test_commit_retry_is_idempotent_and_progress_atomic(db, monkeypatch):
    history = History(db)
    original = db.insert_history
    failed = False

    def committed_then_failed(rows, progress=None):
        nonlocal failed
        original(rows, progress)
        if not failed:
            failed = True
            raise OSError("lost acknowledgement after commit")

    monkeypatch.setattr(db, "insert_history", committed_then_failed)
    reading = {1: {"value": 10, "quality": "Good", "source_timestamp": iso(1699999999),
                   "server_timestamp": iso(1699999999.5)}}
    with pytest.raises(OSError):
        history.save([tag()], reading, "opcua", 1700000000, "one", 1, "same-batch")
    assert history.last == {}
    history.save([tag()], reading, "opcua", 1700000000, "one", 1, "same-batch")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 1
    result = history.query(start=iso(1699999990), end=iso(1700000010), connection_id="one")
    assert result["items"][0]["source_timestamp"] == iso(1699999999)
    assert db.get_meta("storage_progress")["batch_id"] == "same-batch"


def test_definition_and_source_changes_force_new_sample(db):
    history = History(db, heartbeat=1800)
    reading = {1: {"value": 10, "quality": "Good"}}
    assert history.save([tag()], reading, "opcua", 1700000000, "first") == 1
    assert history.save([tag()], reading, "opcua", 1700000001, "second") == 1
    db.replace_tags([tag(name="New unit", unit="bar")])
    updated = Tag.model_validate(db.tags()[0])
    assert updated.revision == 2
    assert history.save([updated], reading, "opcua", 1700000002, "first") == 1
    groups = history.series(1, iso(1699999990), iso(1700000010), buckets=2)
    assert len(groups["series"]) == 3
    assert groups["items"] == []


def test_monotonic_heartbeat_survives_wall_clock_jump(db):
    history = History(db, heartbeat=10)
    readings = {1: {"value": 10, "quality": "Good"}}
    assert history.save([tag()], readings, "opcua", 1700000000, "one", 100) == 1
    assert history.save([tag()], readings, "opcua", 1800000000, "one", 105) == 0
    assert history.save([tag()], readings, "opcua", 1600000000, "one", 111) == 1


def test_complete_series_spike_end_bad_quality_and_gap(db):
    begin = 1700000000
    db.insert_history([(1, begin+index, 99999 if index == 2401 else index, "BadData" if index == 2700 else "Good",
                        "opcua", "Pump", "Point1", "-", "WORD") for index in range(3001)])
    db.record_event("queue_overflow", {}, connection_id="legacy", start=begin+20, end=begin+21, tag_ids=[1])
    history = History(db)
    result = history.series(1, iso(begin), iso(begin+3000), buckets=10)
    assert sum(item["count"] for item in result["items"]) == 3001
    assert max(item["maximum"] or 0 for item in result["items"]) == 99999
    assert result["items"][-1]["last"] == 3000
    assert result["items"][0]["quality"] == "Gap"
    assert result["items"][-1]["quality"] == "BadData"
    assert result["start"] == iso(begin) and result["end"] == iso(begin+3000)
    assert history.series(1, iso(begin+5000), iso(begin+6000))["items"] == []
    db.replace_tags([tag(2)])
    assert history.variables()[0]["active"] is False


def test_query_uses_one_snapshot_during_concurrent_insert(db, monkeypatch):
    begin = 1700000000
    row = (1, begin, 1, "Good", "opcua", "Pump", "Point1", "-", "WORD")
    db.insert_history([row])
    history = History(db)
    original = db.connect
    injected = False

    class Proxy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=()):
            nonlocal injected
            result = self.conn.execute(sql, params)
            if sql.startswith("SELECT COUNT") and not injected:
                injected = True
                with original() as writer:
                    writer.execute("INSERT INTO history_data(tag_id,timestamp,value,quality,source,device,name,unit,data_type) VALUES(?,?,?,?,?,?,?,?,?)", row)
            return result

    @contextmanager
    def wrapped():
        with original() as conn:
            yield Proxy(conn)

    monkeypatch.setattr(db, "connect", wrapped)
    result = history.query(start=iso(begin-1), end=iso(begin+1))
    assert result["total"] == len(result["items"]) == result["summary"][0]["samples"] == 1
    with original() as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 2


def test_partial_batches_variant_validation_and_source_time():
    values = [ua.DataValue(ua.Variant(1, ua.VariantType.UInt16)),
              ua.DataValue(ua.Variant(2, ua.VariantType.Int32))]
    values[0].SourceTimestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
    calls = []

    def attributes(nodes, attribute):
        calls.append(nodes)
        if len(calls) == 3:
            raise TimeoutError("batch timeout")
        return [values[len(calls)-1]]

    client = SimpleNamespace(get_node=lambda node: SimpleNamespace(nodeid=node),
                             uaclient=SimpleNamespace(get_attributes=attributes))
    driver = OPCUADriver({"endpoint": "opc.tcp://127.0.0.1:4840", "batch_size": 1})
    driver.client = client
    result = driver.read([tag(index) for index in range(1, 5)])
    assert result[1]["quality"] == "Good"
    assert result[1]["source_timestamp"] == "2024-01-01T00:00:00+00:00"
    assert result[2]["quality"] == "BadTypeMismatch"
    assert result[3]["quality"] == "BadCommunicationError"
    assert result[4]["quality"] == "BadNotRead"
    assert len(calls) == 3
    assert "batch timeout" in driver.communication_error
    with pytest.raises(PermissionError):
        driver.write(tag(), 1)


def test_fifo_retry_evicts_oldest_waiting_and_persists_gap(db, monkeypatch):
    history = History(db)
    writer = HistoryWriter(history, capacity=3)
    original = history.save
    blocked = threading.Event()
    released = threading.Event()
    attempted = []

    def fail_until_released(*args, **kwargs):
        attempted.append(args[-1])
        if not released.is_set():
            blocked.set()
            raise OSError("disk unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(history, "save", fail_until_released)
    writer.start()
    try:
        writer.enqueue(batch(1))
        assert blocked.wait(2)
        writer.enqueue(batch(2))
        writer.enqueue(batch(3))
        writer.enqueue(batch(4))
        state = writer.diagnostics()
        assert state["pending_batches"] == 3
        assert state["dropped_batches"] == 1
        released.set()
        with writer._condition:
            writer._condition.notify_all()
        wait_for(lambda: writer.diagnostics()["pending_batches"] == 0)
    finally:
        result = writer.stop(3)
    assert result["drained"] and result["storage_stopped"]
    with db.connect() as conn:
        values = [row[0] for row in conn.execute("SELECT value FROM history_data ORDER BY id")]
    assert values == [1, 3, 4]
    event = next(event for event in db.events() if event["kind"] == "queue_overflow")
    assert event["start"] == event["end"] == batch(2).timestamp
    assert event["count"] == 1
    assert db.get_meta("storage_run")["clean_shutdown"] is True


def test_suspect_restart_and_bounded_shutdown(db, monkeypatch):
    db.set_meta("storage_run", {"run_id": "crashed-run", "started_at": 1700000000, "clean_shutdown": False})
    history = History(db)
    writer = HistoryWriter(history)
    writer.start()
    wait_for(lambda: writer._initialized)
    assert any(event["kind"] == "suspect_restart_gap" for event in db.events())
    monkeypatch.setattr(history, "save", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    writer.enqueue(batch(1))
    wait_for(lambda: writer.diagnostics()["retrying"])
    started = time.monotonic()
    result = writer.stop(.1)
    assert time.monotonic()-started < .5
    assert result["drained"] is False
    wait_for(lambda: not writer.diagnostics()["writer_alive"])
    assert db.get_meta("storage_run")["clean_shutdown"] is False


def test_slow_driver_and_storage_do_not_block_snapshot(db, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    history = History(db)
    gateway = Gateway(db, history, {"mode": "opcua", "endpoint": "opc.tcp://127.0.0.1:4840",
                                   "poll_interval": .1, "queue_capacity": 120, "connection_id": "one"})

    class SlowDriver:
        communication_error = ""

        def read(self, tags):
            entered.set()
            release.wait(2)
            return {point.id: {"value": 5, "quality": "Good"} for point in tags}

        def disconnect(self):
            pass

    gateway.driver = SlowDriver()
    monkeypatch.setattr(history, "save", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    gateway.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        assert gateway.snapshot()["total"] == 1
        assert gateway.diagnostics()["collector_alive"]
        assert time.monotonic()-started < .1
        release.set()
        wait_for(lambda: gateway.snapshot()["good"] == 1)
        wait_for(lambda: gateway.diagnostics()["retrying"])
        started = time.monotonic()
        assert gateway.snapshot()["good"] == 1
        assert time.monotonic()-started < .1
    finally:
        release.set()
        result = gateway.stop(.2)
    assert result["collector_stopped"]
    assert result["drained"] is False


def test_simulation_write_requires_explicit_feature_flag(db):
    db.replace_tags([tag(permission="WRITE")])
    config = {"mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4840", "poll_interval": .1}
    gateway = Gateway(db, History(db), config)
    with pytest.raises(PermissionError, match="未启用"):
        gateway.manual_write(1, 123)
    gateway.reconfigure({"simulation_write_enabled": True})
    assert gateway.manual_write(1, 123)[1] == 123
    gateway.reconfigure({"mode": "opcua"})
    with pytest.raises(PermissionError, match="只读"):
        gateway.manual_write(1, 123)
    assert gateway.snapshot()["write_enabled"] is False


def test_reconfigure_during_read_keeps_original_history_identity(db):
    entered, released = threading.Event(), threading.Event()
    gateway = Gateway(db, History(db), {"mode": "opcua", "endpoint": "opc.tcp://127.0.0.1:4840",
                                       "poll_interval": .1, "connection_id": "old-plc"})

    class DelayedDriver:
        communication_error = ""

        def read(self, tags):
            entered.set()
            released.wait(2)
            return {point.id: {"value": 321, "quality": "Good"} for point in tags}

        def disconnect(self):
            pass

    gateway.driver = DelayedDriver()
    gateway.start()
    try:
        assert entered.wait(2)
        gateway.reconfigure({"mode": "simulation", "connection_id": "new-simulation"})
        released.set()
        wait_for(lambda: gateway.snapshot()["good"] == 1)
        assert gateway.snapshot()["connection_id"] == "new-simulation"
        assert gateway.snapshot()["items"][0]["value"] != 321
    finally:
        released.set()
        result = gateway.stop(3)
    assert result["drained"]
    with db.connect() as conn:
        sources = {row[0]: row[1] for row in conn.execute("SELECT connection_id,value FROM history_data")}
    assert sources["old-plc"] == 321
    assert "new-simulation" in sources


def test_configuration_change_interrupts_connection_backoff(db):
    gateway = Gateway(db, History(db), {"mode": "opcua", "endpoint": "opc.tcp://127.0.0.1:4840",
                                       "poll_interval": .1, "connection_id": "offline"})

    class OfflineDriver:
        def read(self, tags):
            raise OSError("connection offline")

        def disconnect(self):
            pass

    gateway.driver = OfflineDriver()
    gateway.start()
    try:
        wait_for(lambda: gateway.snapshot()["state"] == "backoff")
        assert gateway.snapshot()["next_retry_seconds"] == 1
        started = time.monotonic()
        gateway.reconfigure({"mode": "simulation", "connection_id": "sim"})
        wait_for(lambda: gateway.snapshot()["good"] == 1, timeout=1)
        assert time.monotonic()-started < 1
        assert gateway.snapshot()["reconnect_attempts"] == 0
    finally:
        assert gateway.stop(3)["drained"]


def test_point_edit_is_nonblocking_and_old_scan_keeps_definition(db):
    history = History(db)
    gateway = Gateway(db, history, {"mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4840",
                                   "poll_interval": .1, "connection_id": "sim"})
    old = tuple(gateway.tags)
    revision = gateway.replace_tags([tag(name="New name", revision=999)], gateway.revision)
    assert gateway.snapshot()["revision"] == revision
    assert gateway.tags[0].revision == 2
    gateway.writer.start()
    gateway.writer.enqueue(batch(1, old, "sim"))
    gateway.writer.enqueue(batch(2, gateway.tags, "sim"))
    assert gateway.writer.stop(3)["drained"]
    versions = history.variables()
    assert {(row["name"], row["tag_revision"], row["active"]) for row in versions} == {
        ("Point1", 1, False), ("New name", 2, True)}


def test_wall_clock_jump_skips_retention_and_records_event(db):
    now = time.time()
    db.insert_history([(1, now, 1, "Good", "opcua", "Pump", "Point1", "-", "WORD")])
    writer = HistoryWriter(History(db, retention=7))
    wall, monotonic = writer._last_cleanup_clock
    assert writer.retention_tick(wall+365*86400, monotonic+60) == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 1
    assert db.events()[0]["kind"] == "clock_jump"
    assert writer.diagnostics()["retention_paused"] is True
    assert "重启恢复" in writer.diagnostics()["last_clock_event"]["action"]
    assert writer.retention_tick(wall+365*86400+60, monotonic+120) == 0
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 1


def test_successful_transport_with_bad_point_reports_degraded(db):
    gateway = Gateway(db, History(db), {"mode": "opcua", "endpoint": "opc.tcp://127.0.0.1:4840",
                                       "poll_interval": .1, "connection_id": "one"})

    class BadPointDriver:
        communication_error = ""

        def read(self, tags):
            return {point.id: {"value": None, "quality": "BadNodeIdUnknown"} for point in tags}

        def disconnect(self):
            pass

    gateway.driver = BadPointDriver()
    gateway.start()
    try:
        wait_for(lambda: gateway.snapshot()["last_scan"] is not None)
        state = gateway.snapshot()
        assert state["connected"] is True
        assert state["state"] == "degraded"
    finally:
        assert gateway.stop(3)["drained"]


def test_dry_run_identity_validation_includes_retired_ids_and_does_not_write(db):
    db.replace_tags([tag(2)])
    revision = db.tags_revision()
    assert db.validate_replacement([tag(1)]) == {"valid": True, "count": 1}
    with pytest.raises(ValueError, match="永久"):
        db.validate_replacement([tag(1, address="replacement")])
    assert db.tags_revision() == revision
    assert db.tags()[0]["id"] == 2


def test_timed_out_write_can_finish_after_stop_reports_incomplete(db, monkeypatch):
    history = History(db)
    writer = HistoryWriter(history)
    entered, release = threading.Event(), threading.Event()
    original = history.save

    def stalled_commit(*args, **kwargs):
        entered.set()
        release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(history, "save", stalled_commit)
    writer.start()
    writer.enqueue(batch(1))
    assert entered.wait(2)
    result = writer.stop(.01)
    assert result["storage_stopped"] is False and result["drained"] is False
    release.set()
    wait_for(lambda: not writer.diagnostics()["writer_alive"])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 1


def test_sixty_seconds_of_samples_recover_in_order_after_database_outage(db, monkeypatch):
    history = History(db)
    writer = HistoryWriter(history, capacity=120)
    failed, recover = threading.Event(), threading.Event()
    original = db.insert_history

    def unavailable(rows, progress=None):
        if not recover.is_set():
            failed.set()
            raise OSError("controlled 60-second storage outage")
        return original(rows, progress)

    monkeypatch.setattr(db, "insert_history", unavailable)
    writer.start()
    try:
        writer.enqueue(batch(1))
        assert failed.wait(2)
        # Each batch carries a one-second-later wall and monotonic sample clock.
        # No real 60-second sleep is necessary to exercise the retention boundary.
        for second in range(2, 61):
            assert writer.enqueue(batch(second))
        assert writer.diagnostics()["pending_batches"] == 60
        assert writer.diagnostics()["dropped_batches"] == 0
        recover.set()
        with writer._condition:
            writer._condition.notify_all()
        wait_for(lambda: writer.diagnostics()["pending_batches"] == 0)
    finally:
        recover.set()
        assert writer.stop(3)["drained"]
    with db.connect() as conn:
        rows = conn.execute("SELECT timestamp,value FROM history_data ORDER BY id").fetchall()
    assert [row["timestamp"] for row in rows] == [batch(second).timestamp for second in range(1, 61)]
    assert [row["value"] for row in rows] == list(range(1, 61))
    assert db.get_meta("storage_progress")["batch_id"] == "batch-60"


def test_thousand_point_snapshot_p95_under_blocked_plc_and_database(tmp_path, monkeypatch):
    database = Database(tmp_path / "thousand.db")
    points = [tag(number) for number in range(1, 1001)]
    database.replace_tags(points)
    history = History(database)
    gateway = Gateway(database, history, {"mode": "opcua", "endpoint": "opc.tcp://127.0.0.1:4840",
                                         "poll_interval": .1, "connection_id": "thousand"})
    plc_blocked, db_blocked, release = threading.Event(), threading.Event(), threading.Event()
    original = database.insert_history

    def blocked_database(rows, progress=None):
        db_blocked.set()
        release.wait(3)
        return original(rows, progress)

    class BlockAfterFirstScan:
        communication_error = ""
        calls = 0

        def read(self, tags):
            self.calls += 1
            if self.calls > 1:
                plc_blocked.set()
                release.wait(3)
            return {point.id: {"value": point.id, "quality": "Good"} for point in tags}

        def disconnect(self):
            pass

    monkeypatch.setattr(database, "insert_history", blocked_database)
    gateway.driver = BlockAfterFirstScan()
    gateway.start()
    try:
        assert db_blocked.wait(2) and plc_blocked.wait(2)
        elapsed = []
        for _ in range(50):
            started = time.perf_counter()
            current = gateway.snapshot()
            elapsed.append(time.perf_counter()-started)
            assert current["total"] == current["good"] == len(current["items"]) == 1000
        p95 = sorted(elapsed)[47]
        assert p95 < .5, f"1000-point snapshot P95 {p95:.3f}s exceeds 500ms"
        print(f"1000-point snapshot 50-read P95 under blocked PLC/DB: {p95*1000:.2f} ms")
    finally:
        release.set()
        result = gateway.stop(3)
    assert result["collector_stopped"] and result["storage_stopped"] and result["drained"]
