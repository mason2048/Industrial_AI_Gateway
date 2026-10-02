"""Bounded FIFO history writer; PLC acquisition never performs database I/O."""

from collections import deque
from dataclasses import dataclass
import logging
import threading
import time
import uuid

from .history import iso

log = logging.getLogger("industrial_gateway")


@dataclass(frozen=True)
class SampleBatch:
    tags: tuple
    readings: dict
    source: str
    connection_id: str
    timestamp: float
    monotonic: float
    batch_id: str


class HistoryWriter:
    def __init__(self, history, capacity=120):
        if not 1 <= capacity <= 120:
            raise ValueError("历史缓冲容量必须为1至120批")
        self.history, self.db, self.capacity = history, history.db, capacity
        self._condition = threading.Condition()
        self._queue = deque()
        self._current = None
        self._gaps = {}
        self._closing = False
        self._accepting = True
        self._deadline = None
        self._cleanup_stop = threading.Event()
        self._thread = None
        self._cleanup_thread = None
        self._run_id = uuid.uuid4().hex
        self._initialized = False
        self._allow_clean_shutdown = True
        self._last_cleanup_clock = (time.time(), time.monotonic())
        self._status = {"queue_capacity": capacity, "pending_batches": 0, "queued_batches": 0,
                        "retrying": False, "retry_count": 0, "saved_batches": 0,
                        "saved_samples": 0, "dropped_batches": 0, "dropped_samples": 0,
                        "storage_error": "", "cleanup_error": "", "event_error": "",
                        "last_saved": None, "last_gap": None, "pending_gap_events": 0,
                        "writer_alive": False, "last_clock_event": None, "retention_paused": False}

    def diagnostics(self):
        # Replacing this mapping is atomic; readers do not wait on database or writer locks.
        return dict(self._status)

    def _publish(self, **changes):
        self._status = {**self._status, **changes,
                        "pending_batches": len(self._queue) + int(self._current is not None),
                        "queued_batches": len(self._queue), "pending_gap_events": len(self._gaps)}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="history-writer", daemon=True)
        self._cleanup_thread = threading.Thread(target=self._cleanup, name="history-retention", daemon=True)
        self._thread.start()
        self._cleanup_thread.start()

    def enqueue(self, batch):
        with self._condition:
            if not self._accepting:
                self._record_gap(batch, "shutdown_gap")
                self._publish()
                return False
            if len(self._queue) + int(self._current is not None) >= self.capacity:
                # The in-flight retry is fixed. Evict the oldest not-yet-written batch.
                dropped = self._queue.popleft() if self._queue else batch
                self._record_gap(dropped, "queue_overflow")
                if dropped is batch:
                    self._publish()
                    return False
            self._queue.append(batch)
            self._publish()
            self._condition.notify()
            return True

    def _record_gap(self, batch, kind):
        key = (kind, batch.connection_id)
        gap = self._gaps.get(key)
        if gap is None:
            gap = {"event_id": uuid.uuid4().hex, "kind": kind, "connection_id": batch.connection_id,
                   "start": batch.timestamp, "end": batch.timestamp, "count": 0, "tag_ids": set()}
            self._gaps[key] = gap
        gap["start"] = min(gap["start"], batch.timestamp)
        gap["end"] = max(gap["end"], batch.timestamp)
        gap["count"] += 1
        tag_ids = [tag.id for tag in batch.tags if tag.save]
        gap["tag_ids"].update(tag_ids)
        self._publish(dropped_batches=self._status["dropped_batches"]+1,
                      dropped_samples=self._status["dropped_samples"]+len(tag_ids),
                      last_gap={"kind": kind, "connection_id": batch.connection_id,
                                "start": iso(gap["start"]), "end": iso(gap["end"]), "count": gap["count"]})

    def _initialize(self):
        previous = self.db.get_meta("storage_run", {})
        progress = self.db.get_meta("storage_progress", {})
        if previous and not previous.get("clean_shutdown", False):
            start = progress.get("timestamp", previous.get("started_at", time.time()))
            self.db.record_event("suspect_restart_gap",
                {"reason": "上次运行未完成干净停机，内存缓冲可能丢失", "previous_run_id": previous.get("run_id")},
                connection_id=progress.get("connection_id"), start=start, end=time.time(),
                event_id=f"restart:{previous.get('run_id', 'legacy')}")
        self.db.set_meta("storage_run", {"run_id": self._run_id, "started_at": time.time(), "clean_shutdown": False})
        self._initialized = True

    def _flush_gaps(self):
        with self._condition:
            pending = [(key, {**gap, "tag_ids": sorted(gap["tag_ids"])}) for key, gap in self._gaps.items()]
        for key, gap in pending:
            self.db.record_event(gap["kind"], {"reason": "内存缓冲无法保存该采集批次"},
                                 **{field: gap[field] for field in
                                    ("event_id", "connection_id", "start", "end", "count", "tag_ids")})
            with self._condition:
                if self._gaps.get(key, {}).get("count") == gap["count"]:
                    self._gaps.pop(key, None)
                self._publish(event_error="")

    def _expired(self):
        return self._closing and self._deadline is not None and time.monotonic() >= self._deadline

    def _run(self):
        with self._condition:
            self._publish(writer_alive=True)
        try:
            while not self._expired():
                if not self._initialized:
                    try:
                        self._initialize()
                    except Exception as exc:
                        with self._condition:
                            if not self._status["storage_error"]:
                                log.warning("History writer initialization failed: %s", exc)
                            self._publish(storage_error=f"存储运行记录失败：{exc}")
                            self._condition.wait(.2)
                        continue
                with self._condition:
                    if self._current is None and self._queue:
                        self._current = self._queue.popleft()
                        self._publish()
                    batch = self._current
                    if batch is None and not self._gaps:
                        if self._closing:
                            break
                        self._condition.wait(.2)
                        continue
                if batch is not None:
                    try:
                        count = self.history.save(batch.tags, batch.readings, batch.source,
                                  batch.timestamp, batch.connection_id, batch.monotonic, batch.batch_id)
                    except Exception as exc:
                        with self._condition:
                            if not self._status["retrying"]:
                                log.warning("History storage unavailable; retrying queued batch: %s", exc)
                            self._publish(storage_error=f"历史保存失败：{exc}", retrying=True,
                                          retry_count=self._status["retry_count"]+1)
                            remaining = max(0, self._deadline-time.monotonic()) if self._deadline else 1
                            self._condition.wait(min(1, remaining))
                        continue
                    with self._condition:
                        if self._status["retrying"]:
                            log.info("History storage recovered; draining buffered batches")
                        self._current = None
                        self._publish(storage_error="", retrying=False,
                                      saved_batches=self._status["saved_batches"]+1,
                                      saved_samples=self._status["saved_samples"]+count,
                                      last_saved=iso(batch.timestamp))
                try:
                    self._flush_gaps()
                except Exception as exc:
                    with self._condition:
                        self._publish(event_error=f"缺口事件保存失败：{exc}")
                        if batch is None:
                            self._condition.wait(.2)
            if self._initialized and self._closing and self._allow_clean_shutdown and not self._queue and self._current is None and not self._gaps:
                self.db.set_meta("storage_run", {"run_id": self._run_id, "stopped_at": time.time(), "clean_shutdown": True})
        except Exception as exc:
            log.exception("History writer stopped unexpectedly")
            with self._condition:
                self._publish(storage_error=f"存储任务停止：{exc}")
        finally:
            with self._condition:
                self._publish(writer_alive=False)

    def _cleanup(self):
        while not self._cleanup_stop.wait(60):
            try:
                self.retention_tick()
                with self._condition:
                    if not self._status["retention_paused"]:
                        self._publish(cleanup_error="")
            except Exception as exc:
                with self._condition:
                    self._publish(cleanup_error=f"历史清理失败：{exc}")

    def retention_tick(self, wall=None, monotonic=None):
        if self._status["retention_paused"]:
            return 0
        wall = time.time() if wall is None else wall
        monotonic = time.monotonic() if monotonic is None else monotonic
        old_wall, old_monotonic = self._last_cleanup_clock
        self._last_cleanup_clock = (wall, monotonic)
        jump = (wall-old_wall) - (monotonic-old_monotonic)
        if abs(jump) > 5:
            detail = {"jump_seconds": round(jump, 3), "threshold_seconds": 5,
                      "action": "自动历史清理已暂停；校准系统时间后重启恢复"}
            with self._condition:
                self._publish(last_clock_event={**detail, "timestamp": iso(wall)},
                              retention_paused=True, cleanup_error=detail["action"])
            self.db.record_event("clock_jump", detail, start=min(old_wall, wall), end=max(old_wall, wall))
            log.warning("Wall clock changed by %.3f seconds; retention paused until clock correction and restart", jump)
            return 0
        return self.db.cleanup(wall - self.history.retention*86400)

    def stop(self, timeout=30, clean_shutdown=True):
        deadline = time.monotonic() + max(0, timeout)
        with self._condition:
            self._accepting = False
            self._allow_clean_shutdown = clean_shutdown
            self._closing = True
            self._deadline = deadline
            self._condition.notify_all()
        self._cleanup_stop.set()
        for thread in (self._thread, self._cleanup_thread):
            try:
                if thread is not None and thread.ident is not None:
                    thread.join(max(0, deadline-time.monotonic()))
            except Exception:
                log.exception("History worker join failed; continuing remaining shutdown")
        state = self.diagnostics()
        return {"storage_stopped": not any(thread and thread.is_alive() for thread in (self._thread, self._cleanup_thread)),
                "drained": state["pending_batches"] == 0 and state["pending_gap_events"] == 0,
                "pending_batches": state["pending_batches"], "pending_gap_events": state["pending_gap_events"]}
