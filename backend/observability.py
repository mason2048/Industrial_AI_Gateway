"""Bounded diagnostic state and redacted rotating logs for local operations."""
from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import shutil
import threading
import time


class Redactor:
    def __init__(self, values):
        self.update(values)

    def update(self, values):
        self.secrets = tuple(dict.fromkeys(str(v) for v in values if v))
        self.pattern = re.compile("|".join(re.escape(secret) for secret in sorted(self.secrets, key=len, reverse=True))) if self.secrets else None

    def text(self, value):
        result = str(value)
        if self.pattern:
            result = self.pattern.sub("[REDACTED]", result)
        return re.sub(r"(?i)(password|operator.pin|authorization)\s*[:=]\s*[^\s,;]+",
                      r"\1=[REDACTED]", result)

    def clean(self, value, field=None):
        # Structured process data is not a credential echo. Substring redaction of
        # e.g. username="opcua" must not corrupt mode, identities or query labels.
        data_fields = {"mode", "source", "connection_id", "data_type", "type", "permission",
                       "timestamp", "source_timestamp", "server_timestamp", "last_scan", "last_success",
                       "last_saved", "start", "end", "endpoint", "name", "device", "address", "unit", "node_id"}
        states = {"waiting", "connecting", "simulation", "connected", "degraded", "backoff",
                  "stopped", "stopping", "ready", "not_ready", "running"}
        if isinstance(value, str) and (field in data_fields or (field == "state" and value in states)
                                      or (field == "quality" and (value in {"Good", "Waiting", "Stale", "Gap", "NoData", "NotChecked"}
                                                                 or value.startswith(("Bad", "Uncertain"))))):
            return value
        if isinstance(value, dict):
            return {key: self.clean(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [self.clean(item, field) for item in value]
        return self.text(value) if isinstance(value, str) else value


class RedactedFormatter(logging.Formatter):
    def __init__(self, redactor):
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self.redactor = redactor

    def format(self, record):
        return self.redactor.text(super().format(record))


def configure_logging(root, redactor):
    directory = root / "data/logs"
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("industrial_gateway")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(directory / "gateway.log", maxBytes=10*1024*1024,
                                  backupCount=9, encoding="utf-8")
    handler.setFormatter(RedactedFormatter(redactor))
    # The app owns its handler; tests can create independent app roots safely.
    logger.addHandler(handler)
    for library in ("opcua",):
        logging.getLogger(library).setLevel(logging.ERROR)
    return logger, handler


class Operations:
    def __init__(self, root, gateway, db, config, redactor, logger):
        self.root, self.gateway, self.db = root, gateway, db
        self.config, self.redactor, self.logger = config, redactor, logger
        self.events = deque(maxlen=100)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.backup_thread = None
        self.last_backup = None
        self.backup_error = ""
        free = shutil.disk_usage(root).free
        minimum = config.snapshot()["disk_min_free_mb"] * 1024 * 1024
        self.disk = {"free_bytes": free, "minimum_bytes": minimum, "ok": free >= minimum}
        self._last_states = {}
        self._monitor_error = ""
        self.persisted_events = []
        with db.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS management_audit (
                id INTEGER PRIMARY KEY, timestamp REAL NOT NULL,
                kind TEXT NOT NULL, detail TEXT NOT NULL)""")

    def record(self, kind, detail, persist=True):
        detail = self.redactor.clean(detail)
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": kind, "detail": detail}
        with self.lock:
            self.events.append(event)
        self.logger.info("%s %s", kind, json.dumps(detail, ensure_ascii=False))
        if persist:
            try:
                with self.db.connect() as conn:
                    conn.execute("INSERT INTO management_audit(timestamp,kind,detail) VALUES(?,?,?)",
                                 (time.time(), kind, json.dumps(detail, ensure_ascii=False)))
            except Exception:
                # Logging survives a temporarily unavailable SQLite writer; never hide the cause.
                self.logger.error("Audit persistence unavailable; event retained in rotating log")

    def start(self):
        self.thread = threading.Thread(target=self._run, name="gateway-operations", daemon=True)
        self.thread.start()
        if self.config.snapshot()["backup_enabled"]:
            self.backup_thread = threading.Thread(target=self._backups, name="gateway-backup", daemon=True)
            self.backup_thread.start()

    def stop(self, timeout=2):
        self.stop_event.set()
        deadline = time.monotonic() + timeout
        for thread in (self.thread, self.backup_thread):
            if thread and thread.ident is not None:
                thread.join(max(0, deadline-time.monotonic()))

    def _run(self):
        while not self.stop_event.is_set():
            try:
                free = shutil.disk_usage(self.root).free
                limit = self.config.snapshot()["disk_min_free_mb"] * 1024*1024
                with self.lock:
                    self.disk = {"free_bytes": free, "minimum_bytes": limit, "ok": free >= limit}
                snap = self.gateway.snapshot()
                diag = self.gateway.diagnostics()
                states = {"plc": snap.get("state", "online" if snap["connected"] else "offline"),
                          "storage": snap.get("storage_error", ""), "disk": free >= limit,
                          "collector_alive": bool(getattr(self.gateway, "thread", None) and self.gateway.thread.is_alive()),
                          "queue_overflow": bool(diag.get("dropped_batches", 0)),
                          "cleanup": diag.get("cleanup_error", ""),
                          "writer_alive": diag.get("writer_alive", False)}
                for name, state in states.items():
                    if self._last_states.get(name) != state:
                        self.record("state_changed", {"component": name, "state": state})
                self._last_states = states
                persisted_events = self.redactor.clean(self.db.events(limit=100))
                with self.lock:
                    self.persisted_events = persisted_events
                if self._monitor_error:
                    self.logger.info("Operational checks recovered")
                    self._monitor_error = ""
            except Exception as exc:
                error = self.redactor.text(exc)
                if error != self._monitor_error:
                    self.logger.error("Operational check failed: %s", error)
                    self._monitor_error = error
            self.stop_event.wait(1)

    def _backups(self):
        from .maintenance import create_backup, validate_backup
        # Verify existing backups once, newest first. Subsequent scheduling uses
        # monotonic time, so clock corrections cannot postpone/accelerate a run.
        initialized = False
        next_action = time.monotonic()
        while not self.stop_event.is_set():
            remaining = next_action - time.monotonic()
            if remaining > 0:
                self.stop_event.wait(min(60, remaining))
                continue
            try:
                if not initialized:
                    files = []
                    for manifest in (self.root / "data/backups").glob("*/manifest.json"):
                        if manifest.parent.name.startswith("."):
                            continue
                        try:
                            files.append((manifest.stat().st_mtime, manifest))
                        except OSError:
                            continue
                    latest = None
                    for modified, manifest in sorted(files, key=lambda item: item[0], reverse=True):
                        if self.stop_event.is_set():
                            return
                        try:
                            validate_backup(manifest.parent)
                        except (OSError, ValueError):
                            continue
                        latest = (modified, manifest.parent)
                        break
                    if latest:
                        with self.lock:
                            self.last_backup = str(latest[1])
                        # A future timestamp after a clock correction still waits
                        # at most one day. Wall time is consulted only at startup.
                        age = max(0, time.time() - latest[0])
                        next_action = time.monotonic() + max(0, 86400 - age)
                    initialized = True
                    continue
                if self.stop_event.is_set():
                    return
                path = create_backup(self.root)
                with self.lock:
                    recovered = bool(self.backup_error)
                    self.last_backup, self.backup_error = str(path), ""
                next_action = time.monotonic() + 86400
                if recovered:
                    self.record("backup_recovered", {"backup": path.name})
                self.record("backup_complete", {"backup": path.name})
            except Exception as exc:
                error = self.redactor.text(exc) or type(exc).__name__
                with self.lock:
                    changed = error != self.backup_error
                    self.backup_error = error
                if changed:
                    self.record("backup_failed", {"message": error}, persist=False)
                next_action = time.monotonic() + 300

    def diagnostics(self):
        with self.lock:
            extra = {"disk": dict(self.disk), "events": list(self.events),
                     "gap_events": list(self.persisted_events),
                     "last_backup": self.last_backup, "backup_error": self.backup_error}
        return self.redactor.clean({**self.gateway.diagnostics(), **extra})

    def readiness(self):
        current = self.gateway.snapshot()
        diag = self.diagnostics()
        collector = getattr(self.gateway, "thread", None)
        checks = {
            "collector": {"ok": bool(collector and collector.is_alive())},
            "plc": {"ok": bool(current["connected"] and current["total"] and current["good"] == current["total"]),
                    "state": current.get("state"), "good": current["good"], "total": current["total"]},
            "storage": {"ok": bool(diag.get("writer_alive")) and not bool(current.get("storage_error")),
                        "error": current.get("storage_error", "")},
            "queue": {"ok": diag.get("pending_batches", 0) < diag.get("queue_capacity", 120),
                      "pending_batches": diag.get("pending_batches", 0),
                      "capacity": diag.get("queue_capacity", 120)},
            "disk": diag["disk"],
        }
        ready = all(item["ok"] for item in checks.values())
        return self.redactor.clean({"ready": ready, "status": "ready" if ready else "not_ready", "checks": checks})
