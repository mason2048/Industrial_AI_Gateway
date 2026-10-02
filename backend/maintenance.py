"""Version-independent SQLite backup/restore. Importing never opens the live DB."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time

from scripts.runtime import ProcessLock, _lock, _unlock

_backup_lock = threading.RLock()
_maintenance_owners: dict[str, object] = {}
_NAME = re.compile(r"^\d{8}T\d{12}Z-[a-f0-9]{8}$")
_FORMAT = "industrial-ai-gateway-backup-v1"
_FILES = ("history.db", "config.json")


@contextmanager
def _maintenance_lock(root: Path):
    """Serialize daily maintenance and CLI backups, including across processes."""
    key = str(root)
    with _backup_lock:
        if key in _maintenance_owners:
            yield
            return
        path = root / "data/maintenance.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            try:
                _lock(handle)
            except OSError as exc:
                raise RuntimeError("Another backup or restore is in progress; retry when it finishes.") from exc
            _maintenance_owners[key] = handle
            try:
                yield
            finally:
                del _maintenance_owners[key]
                _unlock(handle)


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _read_only(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)


def _check_db(path: Path) -> int:
    with closing(_read_only(path)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed.")
        return db.execute("PRAGMA user_version").fetchone()[0]


def _copy_db(source: Path, target: Path):
    deadline = time.monotonic() + 30
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup exceeded 30 seconds.")
    with closing(_read_only(source)) as db, closing(sqlite3.connect(target)) as destination:
        db.backup(destination, pages=256, progress=progress, sleep=0.05)
        # A portable backup must be self-contained, even when its source uses WAL.
        destination.execute("PRAGMA journal_mode=DELETE")
    _check_db(target)


def _public_config(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized.endswith("_env") or normalized.endswith("_path"):
                result[key] = item
            elif any(part in normalized for part in ("password", "passwd", "secret", "token", "operator_pin", "admin_pin", "private_key", "api_key")):
                continue
            else:
                result[key] = _public_config(item)
        return result
    if isinstance(value, list):
        return [_public_config(item) for item in value]
    return value


def _validate_backup(backup_path: Path) -> dict:
    backup = Path(backup_path).resolve(strict=True)
    if not backup.is_dir():
        raise ValueError("Backup must be a directory containing manifest.json.")
    manifest_path = backup / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("Backup manifest cannot be a symbolic link.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != _FORMAT or set(manifest.get("files", {})) != set(_FILES):
        raise ValueError("Unrecognized or incomplete backup manifest.")
    for name in _FILES:
        path = backup / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Backup member missing or unsafe: {name}")
        expected = manifest["files"][name]
        if path.stat().st_size != expected["size"] or _digest(path) != expected["sha256"]:
            raise ValueError(f"Backup checksum mismatch: {name}")
    version = _check_db(backup / "history.db")
    if manifest.get("database_version") != version:
        raise ValueError("Backup database version does not match its manifest.")
    config = json.loads((backup / "config.json").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or _public_config(config) != config:
        raise ValueError("Backup config is invalid or contains secret fields.")
    return manifest


def validate_backup(backup_path: Path) -> dict:
    """Reject malformed manifests/databases consistently for monitor callers."""
    try:
        return _validate_backup(backup_path)
    except (KeyError, TypeError, sqlite3.Error) as exc:
        raise ValueError("Backup manifest or SQLite database is invalid.") from exc


def _retain(backups: Path):
    candidates = []
    for path in backups.iterdir():
        if path.is_symlink() or not path.is_dir() or not _NAME.fullmatch(path.name):
            continue
        try:
            validate_backup(path)
        except (OSError, ValueError, KeyError, sqlite3.Error):
            continue
        candidates.append(path)
    candidates.sort(key=lambda path: path.name, reverse=True)
    keep = set(candidates[:7])
    for path in candidates:
        if path not in keep:
            # Only our exact, verified backup directory may be removed.
            shutil.rmtree(path)


def create_backup(root: Path) -> Path:
    root = Path(root).resolve()
    with _maintenance_lock(root):
        source = root / "data/history.db"
        config_path = root / "config/config.json"
        if not source.is_file() or not config_path.is_file():
            raise FileNotFoundError("A configured installation and history.db are required for backup.")
        backups = root / "data/backups"
        backups.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=backups))
        try:
            for attempt in range(3):
                config_bytes = config_path.read_bytes()
                config = _public_config(json.loads(config_bytes))
                _copy_db(source, staging / "history.db")
                if config_bytes == config_path.read_bytes():
                    break
            else:
                raise RuntimeError("Configuration changed during backup; retry after editing finishes.")
            (staging / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            manifest = {"format": _FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                        "database_version": _check_db(staging / "history.db"),
                        "secrets_excluded": True, "files": {}}
            for name in _FILES:
                path = staging / name
                manifest["files"][name] = {"size": path.stat().st_size, "sha256": _digest(path)}
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            validate_backup(staging)
            name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(4)
            destination = backups / name
            staging.replace(destination)
            _retain(backups)
            return destination
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def restore_backup(root: Path, backup_path: Path) -> dict:
    root = Path(root).resolve()
    backup = Path(backup_path).resolve(strict=True)
    with ProcessLock(root, reentrant=False, purpose="restore"), _maintenance_lock(root):
        manifest = validate_backup(backup)
        # Stage before retention runs, so restoring an oldest valid backup works.
        staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=root / "data"))
        try:
            _copy_db(backup / "history.db", staging / "history.db")
            shutil.copyfile(backup / "config.json", staging / "config.json")
            target = root / "data/history.db"
            config_target = root / "config/config.json"
            before = create_backup(root)
            # Retain the exact pre-restore config locally for rollback, never in a
            # portable backup. Existing secret files are neither read nor copied.
            original_config = config_target.read_bytes()
            mutated = False
            try:
                with closing(sqlite3.connect(target, timeout=5)) as db:
                    checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if checkpoint and checkpoint[0] != 0:
                        raise RuntimeError("Database is busy; close all gateway/database processes before restore.")
                # Connections above are closed. Do not leave stale WAL pages next
                # to a replacement main file; SQLite checkpoint removed content.
                for suffix in ("-wal", "-shm"):
                    target.with_name(target.name + suffix).unlink(missing_ok=True)
                (staging / "history.db").replace(target)
                mutated = True
                (staging / "config.json").replace(config_target)
                _check_db(target)
            except BaseException:
                if mutated:
                    rollback = staging / "rollback.db"
                    _copy_db(before / "history.db", rollback)
                    rollback.replace(target)
                    (staging / "original-config.json").write_bytes(original_config)
                    (staging / "original-config.json").replace(config_target)
                raise
            return {"restored": str(backup), "pre_restore_backup": str(before),
                    "database_version": manifest["database_version"], "migrated": False,
                    "message": "Restored while stopped. Re-provision external secrets/certificates before startup if needed."}
        finally:
            shutil.rmtree(staging)
