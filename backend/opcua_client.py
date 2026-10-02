"""Read-only OPC UA acquisition with short-lived snapshot locks."""

import hashlib
import logging
import math
import os
import threading
import time
import uuid

from opcua import Client, ua
from .history import epoch, iso
from .models import Tag, typed_value
from .storage import HistoryWriter, SampleBatch

log = logging.getLogger("industrial_gateway")
VARIANT_TYPES = {"BOOL": ua.VariantType.Boolean, "WORD": ua.VariantType.UInt16,
                 "DWORD": ua.VariantType.UInt32, "FLOAT": ua.VariantType.Float}


class OPCUADriver:
    def __init__(self, config, stop_event=None):
        self.config = dict(config)
        self.client = None
        self.stop_event = stop_event
        self.communication_error = ""

    def disconnect(self):
        if self.client:
            try:
                self.client.disconnect()
            except Exception:
                pass
            self.client = None

    def connect(self):
        client = Client(self.config["endpoint"], timeout=self.config.get("request_timeout", 4))
        try:
            if self.config.get("security_string"):
                client.set_security_string(self.config["security_string"])
            if self.config.get("username"):
                client.set_user(self.config["username"])
                client.set_password(os.environ.get(self.config.get("password_env", "PLC_OPCUA_PASSWORD"), ""))
            client.connect()
            self.client = client
        except Exception:
            try:
                client.disconnect()
            except Exception:
                pass
            raise

    @staticmethod
    def _bad(quality):
        return {"value": None, "quality": quality, "timestamp": iso(time.time()),
                "source_timestamp": None, "server_timestamp": None}

    def read(self, tags):
        self.communication_error = ""
        if not self.client:
            self.connect()
        result = {tag.id: self._bad("BadNodeIdMissing") for tag in tags if not tag.node_id}
        mapped = [tag for tag in tags if tag.node_id]
        size = self.config.get("batch_size", 100)
        for offset in range(0, len(mapped), size):
            if self.stop_event is not None and self.stop_event.is_set():
                self.communication_error = "采集正在停止"
                result.update({tag.id: self._bad("BadShutdown") for tag in mapped[offset:]})
                break
            batch = mapped[offset:offset+size]
            try:
                nodes = [self.client.get_node(tag.node_id).nodeid for tag in batch]
                values = self.client.uaclient.get_attributes(nodes, ua.AttributeIds.Value)
                if len(values) != len(batch):
                    raise RuntimeError("OPC UA返回点位数量不匹配")
            except Exception as exc:
                self.communication_error = f"{type(exc).__name__}: {exc}"
                # Preserve completed batches. Failed and not-attempted nodes are explicit.
                result.update({tag.id: self._bad("BadCommunicationError") for tag in batch})
                result.update({tag.id: self._bad("BadNotRead") for tag in mapped[offset+size:]})
                break
            received = iso(time.time())
            for tag, value in zip(batch, values):
                timestamps = {"timestamp": received,
                              "source_timestamp": iso(epoch(value.SourceTimestamp)) if value.SourceTimestamp else None,
                              "server_timestamp": iso(epoch(value.ServerTimestamp)) if value.ServerTimestamp else None}
                if not value.StatusCode.is_good():
                    result[tag.id] = {"value": None, "quality": value.StatusCode.name, **timestamps}
                    continue
                try:
                    if value.Value is None or value.Value.VariantType != VARIANT_TYPES[tag.type]:
                        raise ValueError("OPC UA Variant类型与点表不一致")
                    result[tag.id] = {"value": typed_value(value.Value.Value, tag.type),
                                      "quality": "Good", **timestamps}
                except (ValueError, TypeError, OverflowError):
                    result[tag.id] = {"value": None, "quality": "BadTypeMismatch", **timestamps}
        return result

    def write(self, tag, value):
        raise PermissionError("V1.1真实PLC连接为只读，禁止写入")


class Gateway:
    def __init__(self, db, history, config):
        self.db, self.history, self.config = db, history, config
        self.lock = threading.RLock()
        self._configuration_lock = threading.Lock()
        self.stop_event = threading.Event()
        self._wake_event = threading.Event()
        self.tags = [Tag.model_validate(item) for item in db.tags()]
        self.revision = db.tags_revision()
        self.readings, self.overrides = {}, {}
        self._generation = 0
        self._driver_generation = 0
        self.driver = OPCUADriver(config, self.stop_event)
        self.writer = HistoryWriter(history, config.get("queue_capacity", 120))
        self.connected = False
        self.error = ""
        self.last_scan = None
        self.last_success = None
        self._last_scan_monotonic = None
        self.scan_ms = 0
        self.state = "waiting"
        self.reconnect_attempts = 0
        self.next_retry_seconds = 0
        self.thread = None

    @staticmethod
    def _connection_id(config):
        if config.get("connection_id"):
            return config["connection_id"]
        if config["mode"] == "simulation":
            return "simulation"
        return "opcua-" + hashlib.sha256(config["endpoint"].encode()).hexdigest()[:16]

    @property
    def storage_error(self):
        return self.writer.diagnostics()["storage_error"]

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.writer.start()
        self.thread = threading.Thread(target=self.run, name="plc-collector", daemon=True)
        self.thread.start()

    def stop(self, timeout=30):
        deadline = time.monotonic() + max(0, timeout)
        self.stop_event.set()
        self._wake_event.set()
        try:
            if self.thread is not None and self.thread.ident is not None:
                self.thread.join(max(0, deadline-time.monotonic()))
        except Exception:
            # A collector lifecycle error must never bypass closing the history writer.
            log.exception("Collector join failed; continuing storage shutdown")
        collector_stopped = self.thread is None or not self.thread.is_alive()
        result = self.writer.stop(max(0, deadline-time.monotonic()), clean_shutdown=collector_stopped)
        with self.lock:
            self.state = "stopped" if collector_stopped else "stopping"
            self.connected = False
        return {"collector_stopped": collector_stopped, **result}

    def reconfigure(self, new):
        with self.lock:
            previous_endpoint = self.config["endpoint"]
            self.config.update(new)
            if self.config["endpoint"] != previous_endpoint and "connection_id" not in new:
                self.config["connection_id"] = uuid.uuid4().hex
            self._generation += 1
            self.readings = {}
            self.overrides = {}
            self.connected = False
            self.last_scan = None
            self.last_success = None
            self._last_scan_monotonic = None
            self.error = "等待首次采集"
            self.state = "waiting"
            self.reconnect_attempts = 0
            self.next_retry_seconds = 0
            self._wake_event.set()

    def replace_tags(self, tags, expected_revision=None):
        # Serialize table edits, but never hold the snapshot lock during SQLite I/O.
        with self._configuration_lock:
            revision = self.db.replace_tags(tags, expected_revision)
            stored = [Tag.model_validate(item) for item in self.db.tags()]
            with self.lock:
                self.tags = stored
                self.revision = revision
                self._generation += 1
                self.readings = {}
                self.overrides = {}
                self.connected = False
                self.last_scan = None
                self._last_scan_monotonic = None
                self.state = "waiting"
                self._wake_event.set()
            return revision

    def simulate(self, now, tags=None, overrides=None):
        if tags is None:
            with self.lock:
                tags, overrides = tuple(self.tags), dict(self.overrides)
        values = {}
        for tag in tags:
            phase = now / 22 + tag.id / 3
            if tag.type == "BOOL":
                value = True
            elif tag.type == "WORD":
                value = int(1450 + 30 * math.sin(phase))
            elif tag.type == "DWORD":
                value = int(now) % 4294967296
            elif "压力" in tag.name or "真空" in tag.name:
                value = 5e-5 + 2e-5 * math.sin(phase)
            elif "温度" in tag.name:
                value = 35.6 + 2.3 * math.sin(phase)
            elif "电流" in tag.name:
                value = 12.5 + 0.8 * math.sin(phase)
            else:
                value = 50 + 10 * math.sin(phase)
            values[tag.id] = {"value": (overrides or {}).get(tag.id, value), "quality": "Good",
                              "timestamp": iso(now), "source_timestamp": None, "server_timestamp": None}
        return values

    def run(self):
        try:
            while not self.stop_event.is_set():
                self._wake_event.clear()
                started = time.monotonic()
                with self.lock:
                    config, tags = dict(self.config), tuple(self.tags)
                    overrides = dict(self.overrides)
                    generation = self._generation
                    self.state = "connecting" if config["mode"] == "opcua" and not self.connected else self.state
                if self._driver_generation != generation:
                    self.driver.disconnect()
                    self.driver = OPCUADriver(config, self.stop_event)
                    self._driver_generation = generation
                try:
                    readings = (self.simulate(time.time(), tags, overrides) if config["mode"] == "simulation"
                                else self.driver.read(tags))
                    error = "" if config["mode"] == "simulation" else getattr(self.driver, "communication_error", "")
                    connected = not bool(error)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    connected = False
                    readings = {tag.id: OPCUADriver._bad("BadCommunicationError") for tag in tags}
                if not connected:
                    self.driver.disconnect()
                stamp, finished = time.time(), time.monotonic()
                for reading in readings.values():
                    reading.setdefault("timestamp", iso(stamp))
                    reading.setdefault("source_timestamp", None)
                    reading.setdefault("server_timestamp", None)
                with self.lock:
                    obsolete = generation != self._generation
                    if not obsolete:
                        self.readings = readings
                        self.connected, self.error = connected, error
                        self.last_scan, self._last_scan_monotonic = stamp, finished
                        self.scan_ms = round((finished-started)*1000, 2)
                        if any(item["quality"] == "Good" for item in readings.values()):
                            self.last_success = stamp
                        self.reconnect_attempts = 0 if connected else self.reconnect_attempts+1
                        delay = config["poll_interval"] if connected else min(30, 2**min(self.reconnect_attempts-1, 5))
                        self.next_retry_seconds = 0 if connected else delay
                        has_bad = any(item["quality"] != "Good" for item in readings.values())
                        self.state = ("simulation" if config["mode"] == "simulation" else
                                      "degraded" if has_bad else "connected") if connected else (
                            "degraded" if any(item["quality"] == "Good" for item in readings.values()) else "backoff")
                self.writer.enqueue(SampleBatch(tags, readings, config["mode"], self._connection_id(config),
                                                stamp, finished, uuid.uuid4().hex))
                if obsolete:
                    continue  # Persist its original identity, but never publish an obsolete live snapshot.
                self._wake_event.wait(max(.05, delay-(time.monotonic()-started)))
        finally:
            self.driver.disconnect()
            with self.lock:
                self.connected = False
                self.state = "stopped"

    def snapshot(self, device=None):
        with self.lock:
            config, tags, readings = dict(self.config), tuple(self.tags), self.readings
            stale = self._last_scan_monotonic is None or time.monotonic()-self._last_scan_monotonic > max(10, config["poll_interval"]*3)
            items = []
            for tag in tags:
                if device and tag.device != device:
                    continue
                reading = readings.get(tag.id, {"value": None, "quality": "Waiting", "timestamp": None,
                                                "source_timestamp": None, "server_timestamp": None})
                if stale:
                    reading = {**reading, "value": None, "quality": "Stale"}
                items.append({**tag.model_dump(), **reading})
            return {"mode": config["mode"], "endpoint": config["endpoint"],
                    "connection_id": self._connection_id(config), "state": self.state,
                    "write_enabled": config["mode"] == "simulation" and bool(config.get("simulation_write_enabled", False)),
                    "revision": self.revision, "connection_revision": config.get("revision", 1),
                    "connected": self.connected and not stale, "error": self.error,
                    "storage_error": self.storage_error, "capacity": 1000, "total": len(items),
                    "good": sum(item["quality"] == "Good" for item in items),
                    "last_scan": iso(self.last_scan) if self.last_scan is not None else None,
                    "last_success": iso(self.last_success) if self.last_success is not None else None,
                    "scan_ms": self.scan_ms, "reconnect_attempts": self.reconnect_attempts,
                    "next_retry_seconds": self.next_retry_seconds, "items": items}

    def diagnostics(self):
        storage = self.writer.diagnostics()
        with self.lock:
            return {**storage, "collector_alive": bool(self.thread and self.thread.is_alive()),
                    "state": self.state, "connection_id": self._connection_id(self.config),
                    "last_scan": iso(self.last_scan) if self.last_scan is not None else None,
                    "scan_ms": self.scan_ms, "reconnect_attempts": self.reconnect_attempts,
                    "next_retry_seconds": self.next_retry_seconds}

    def manual_write(self, tag_id, value):
        with self.lock:
            if self.config["mode"] != "simulation":
                raise PermissionError("V1.1真实PLC连接为只读，禁止写入")
            if not self.config.get("simulation_write_enabled", False):
                raise PermissionError("模拟写入未启用")
            tag = next((item for item in self.tags if item.id == tag_id), None)
            if tag is None:
                raise ValueError("点位不存在")
            if tag.permission != "WRITE":
                raise PermissionError("READ点位禁止写入")
            value = typed_value(value, tag.type)
            self.overrides[tag.id] = value
            return tag, value
