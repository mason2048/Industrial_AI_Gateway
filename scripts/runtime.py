"""Cross-platform process ownership and cooperative shutdown; never kill a PID."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import threading
import time


class AlreadyRunning(RuntimeError):
    pass


_guard = threading.RLock()
_owners: dict[str, dict] = {}


def _lock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, value):
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ProcessLock:
    """OS-owned lock shared by nested launcher/application contexts in one process.

    ``reentrant=False`` is mandatory for offline operations: they must reject even
    an application currently running in this same Python process.
    """

    def __init__(self, root: Path, *, reentrant: bool = True, purpose: str = "gateway"):
        self.root = Path(root).resolve()
        self.key = str(self.root)
        self.reentrant = reentrant
        self.purpose = purpose
        self.entry = None

    def __enter__(self):
        with _guard:
            if self.key in _owners:
                if not self.reentrant or self.purpose != "gateway" or _owners[self.key]["purpose"] != "gateway":
                    raise AlreadyRunning("Gateway is running; stop it before this operation.")
                self.entry = _owners[self.key]
                self.entry["references"] += 1
                return self
            data = self.root / "data"
            data.mkdir(parents=True, exist_ok=True)
            path = data / "gateway.lock"
            handle = path.open("a+b")
            if path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            try:
                _lock(handle)
            except OSError as exc:
                handle.close()
                raise AlreadyRunning("Another gateway or offline maintenance process owns this installation.") from exc
            entry = {"handle": handle, "references": 1, "token": secrets.token_urlsafe(32),
                     "pid": os.getpid(), "purpose": self.purpose, "root": self.key,
                     "started_at": time.time()}
            try:
                _atomic_json(data / "gateway.pid", {k: v for k, v in entry.items() if k not in ("handle", "references")})
                (data / "gateway.stop").unlink(missing_ok=True)
            except BaseException:
                _unlock(handle)
                handle.close()
                raise
            _owners[self.key] = self.entry = entry
            return self

    @property
    def token(self):
        return self.entry["token"] if self.entry else None

    def stop_requested(self) -> bool:
        try:
            message = json.loads((self.root / "data/gateway.stop").read_text(encoding="utf-8"))
            return secrets.compare_digest(str(message.get("token", "")), self.token or "")
        except (OSError, ValueError, TypeError):
            return False

    def __exit__(self, *exc):
        with _guard:
            if self.entry is None:
                return
            self.entry["references"] -= 1
            if self.entry["references"]:
                self.entry = None
                return
            try:
                for name in ("gateway.pid", "gateway.stop"):
                    (self.root / "data" / name).unlink(missing_ok=True)
            finally:
                _unlock(self.entry["handle"])
                self.entry["handle"].close()
                del _owners[self.key]
                self.entry = None


def is_running(root: Path) -> bool:
    root = Path(root).resolve()
    with _guard:
        if str(root) in _owners:
            return True
    path = root / "data/gateway.lock"
    if not path.exists():
        return False
    with path.open("r+b") as handle:
        try:
            _lock(handle)
        except OSError:
            return True
        _unlock(handle)
    return False


def request_stop(root: Path, timeout: float = 30.0) -> dict:
    root = Path(root).resolve()
    if not is_running(root):
        return {"stopped": True, "message": "Gateway is already stopped."}
    try:
        owner = json.loads((root / "data/gateway.pid").read_text(encoding="utf-8"))
        if owner.get("root") != str(root) or owner.get("purpose") != "gateway" or not owner.get("token"):
            raise ValueError("Owner metadata does not identify this gateway.")
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("Cannot verify gateway ownership; refusing to signal any process.") from exc
    _atomic_json(root / "data/gateway.stop", {"token": owner["token"], "requested_at": time.time()})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(root):
            return {"stopped": True, "message": "Gateway stopped gracefully."}
        time.sleep(0.1)
    return {"stopped": False, "message": f"Graceful stop exceeded {timeout:g} seconds; no process was killed. Check diagnostics and logs."}
