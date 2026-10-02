"""Portable, local-only desktop startup with persistent data outside the EXE.

The browser is the user interface. Closing it does not stop acquisition; use
``IndustrialAIGateway.exe --stop`` for verified, cooperative shutdown.
"""
from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

from scripts.runtime import AlreadyRunning, ProcessLock, is_running, request_stop


APP_NAME = "Industrial AI Gateway"
SAFE_ENDPOINT = "opc.tcp://127.0.0.1:4840"
DEFAULT_CONFIG = {
    "mode": "simulation", "endpoint": SAFE_ENDPOINT, "poll_interval": 1.0,
    "batch_size": 100, "heartbeat_seconds": 1800, "retention_days": 7,
    "simulation_write_enabled": False, "backup_enabled": True,
}


class DesktopError(RuntimeError):
    """A generated, safe message suitable for an end-user dialog or JSON."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _SilentStream(io.TextIOBase):
    @property
    def encoding(self):
        return "utf-8"

    def write(self, value):
        return len(value)

    def flush(self):
        pass


def _connect_parent_output():
    """Windowed EXEs retain redirected pipes for CLI smoke tests when provided.

    PyInstaller sets sys.stdout/stderr to None in windowed builds. Recover the
    inherited OS handle, or attach the calling console for explicit CLI actions.
    The duplicate is owned by Python; the inherited handle is left untouched.
    """
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    for name, identifier in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is not None:
            continue
        handle = kernel.GetStdHandle(identifier & 0xFFFFFFFF)
        if not handle or handle == wintypes.HANDLE(-1).value:
            kernel.AttachConsole(0xFFFFFFFF)  # ATTACH_PARENT_PROCESS
            handle = kernel.GetStdHandle(identifier & 0xFFFFFFFF)
        if not handle or handle == wintypes.HANDLE(-1).value:
            continue
        duplicate = wintypes.HANDLE()
        process = kernel.GetCurrentProcess()
        if kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
            try:
                fd = msvcrt.open_osfhandle(duplicate.value, os.O_WRONLY | os.O_BINARY)
                setattr(sys, name, os.fdopen(fd, "w", encoding="utf-8", errors="replace", buffering=1))
            except OSError:
                kernel.CloseHandle(duplicate)


def _ensure_streams():
    # launch.run_gateway prints one URL; a windowed build must not fail on it.
    if sys.stdout is None:
        sys.stdout = _SilentStream()
    if sys.stderr is None:
        sys.stderr = _SilentStream()


def _notify(message: str, *, error: bool = False):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10 if error else 0x40)
    else:
        stream = sys.stderr if error else sys.stdout
        if stream:
            print(message, file=stream, flush=True)


def bundle_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])).resolve()


def executable_path() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parents[1] / "desktop.py").resolve()


def _has_installation(root: Path) -> bool:
    return any((root / name).exists() for name in
               ("config/config.json", "data/history.db", "data/operator_pin.txt"))


def _writable_directory(root: Path) -> bool:
    probe = root / (".gateway-write-test-" + secrets.token_hex(6))
    try:
        root.mkdir(parents=True, exist_ok=True)
        with probe.open("xb") as handle:
            handle.write(b"write-test")
        return True
    except OSError:
        return False
    finally:
        with suppress(OSError):
            probe.unlink(missing_ok=True)


def resolve_root(explicit: Path | None = None, *, executable: Path | None = None,
                 local_app_data: Path | None = None, write_probe=True) -> Path:
    """Choose a stable installation, including when a portable EXE is upgraded."""
    if explicit is not None:
        root = Path(explicit).expanduser().resolve()
        if write_probe and not _writable_directory(root):
            raise DesktopError("directory_unwritable", "指定的数据目录无法写入。请选择您有写入权限的目录。")
        return root
    portable = (Path(executable or executable_path()).resolve().parent / "gateway-data").resolve()
    local = Path(local_app_data or os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    fallback = (local / "IndustrialAIGateway").resolve()
    # Preserve the existing choice even if permission conditions later change.
    for root in (portable, fallback):
        if _has_installation(root):
            if write_probe and not _writable_directory(root):
                raise DesktopError("directory_unwritable", "现有数据目录无法写入。请恢复该目录权限后启动；配置和历史未被移动或覆盖。")
            return root
    if not write_probe:
        return portable
    if _writable_directory(portable):
        return portable
    if _writable_directory(fallback):
        return fallback
    raise DesktopError("directory_unwritable", "软件目录和本机用户数据目录均无法写入。请用 --root 指定可写目录。")


def _atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with suppress(OSError):
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _default_configuration(resources: Path) -> dict:
    path = resources / "config/config.default.json"
    if not path.is_file():
        return dict(DEFAULT_CONFIG)
    try:
        candidate = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DesktopError("invalid_bundle", "安装包的默认配置无法读取，请重新下载完整安装包。") from exc
    # Only operational defaults can come from a bundle; never carry field data,
    # session identities, account names or certificate paths into a new install.
    if not isinstance(candidate, dict) or candidate.get("mode", "simulation") != "simulation" \
            or candidate.get("endpoint", SAFE_ENDPOINT) != SAFE_ENDPOINT \
            or any(candidate.get(key) for key in ("username", "security_string", "password", "connection_id")):
        raise DesktopError("unsafe_bundle", "安装包包含非默认现场配置，已拒绝初始化。请重新下载官方安装包。")
    safe = dict(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in candidate and key not in ("mode", "endpoint", "simulation_write_enabled"):
            safe[key] = candidate[key]
    return safe


def _refresh_frontend(root: Path, resources: Path):
    source = resources / "frontend"
    target = root / "frontend"
    if not (source / "index.html").is_file() or not (source / "vendor/vue.global.prod.js").is_file():
        raise DesktopError("missing_frontend", "安装包缺少网页资源，请重新下载完整的EXE。")
    if source.resolve() == target.resolve():
        return
    staging = root / (".frontend-new-" + secrets.token_hex(6))
    previous = root / (".frontend-old-" + secrets.token_hex(6))
    try:
        shutil.copytree(source, staging)
        if target.exists():
            target.replace(previous)
        try:
            staging.replace(target)
        except BaseException:
            if previous.exists():
                previous.replace(target)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if previous.exists():
            shutil.rmtree(previous)


def _write_sample_tags(root: Path):
    """Fresh simulation definitions only; do not manufacture historical data."""
    from backend.models import Tag
    from backend.tag_manager import export_excel
    specifications = [
        ("DB1.DBD20", "模拟真空压力", "FLOAT", "Pa", 0.00001, True),
        ("DB1.DBD30", "模拟泵体温度", "FLOAT", "°C", 0.5, True),
        ("DB1.DBD40", "模拟电机电流", "FLOAT", "A", 0.2, True),
        ("M100.0", "模拟启动状态", "BOOL", "-", 0, False),
        ("DB1.DBW10", "模拟电机转速", "WORD", "rpm", 10, True),
        ("DB1.DBD50", "模拟累计运行秒", "DWORD", "s", 60, True),
    ]
    tags = [Tag(id=index, address=address, name=name, type=kind, unit=unit, threshold=threshold,
                save=save, device="模拟设备01", ai_description="仅用于本地模拟体验，请配置实际OPC UA点位。",
                node_id=f"ns=2;s=Demo.Tag{index}", history_interval_seconds=1800, record_changes=False,
                precision=5 if kind == "FLOAT" else 0)
            for index, (address, name, kind, unit, threshold, save) in enumerate(specifications, 1)]
    path = root / "data/tags.xlsx"
    with path.open("xb") as handle:
        handle.write(export_excel(tags))


def initialize_installation(root: Path, resources: Path, *, demo_init=False):
    """Called only with ProcessLock held; refresh resources, preserve all data."""
    config_path = root / "config/config.json"
    new = not config_path.exists()
    if new and (root / "data/history.db").exists():
        raise DesktopError("missing_existing_configuration", "发现已有历史数据库，但缺少连接配置。请恢复原配置后启动，软件不会用默认配置覆盖现场数据。")
    default = _default_configuration(resources) if new else None
    _refresh_frontend(root, resources)
    if new:
        _atomic_json(config_path, default)
    if demo_init:
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DesktopError("invalid_configuration", "连接配置无法读取。请查看或修复config/config.json。") from exc
        if existing.get("mode") != "simulation":
            raise DesktopError("demo_requires_simulation", "模拟点位初始化只能用于模拟模式，现有PLC连接配置未修改。")
    if (new or demo_init) and not (root / "data/history.db").exists() and not (root / "data/tags.xlsx").exists():
        _write_sample_tags(root)


def choose_port(requested: int | None = None) -> int:
    if requested is not None and not 1 <= requested <= 65535:
        raise DesktopError("invalid_port", "端口必须是1至65535之间的整数。")
    candidates = [requested] if requested is not None else range(8080, 8100)
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise DesktopError("port_occupied", "指定端口已被占用。请关闭占用程序，或使用 --port 指定其他端口。" if requested
                       else "8080至8099端口均已被占用。请使用 --port 指定其他本机端口。")


def _gateway_healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as response:
            return json.load(response).get("app") == APP_NAME
    except (OSError, ValueError):
        return False


def open_existing_instance(root: Path, *, no_browser=False, timeout=15) -> dict:
    """Open only the port recorded by this directory's verified process owner."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(root):
            raise DesktopError("instance_stopped", "原运行实例已退出，请重新双击启动软件。")
        try:
            instance = json.loads((root / "data/desktop-instance.json").read_text(encoding="utf-8"))
            owner = json.loads((root / "data/gateway.pid").read_text(encoding="utf-8"))
            valid = owner.get("purpose") == "gateway" and owner.get("root") == str(root) \
                and instance.get("root") == str(root) \
                and instance.get("owner_started_at") == owner.get("started_at")
            port = instance["port"]
            if valid and type(port) is int and 1 <= port <= 65535 and _gateway_healthy(port):
                url = f"http://127.0.0.1:{port}"
                if not no_browser:
                    webbrowser.open(url)
                return {"running": True, "existing_instance": True, "url": url, "root": str(root)}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(0.1)
    raise DesktopError("instance_not_ready", "该数据目录已有运行实例，网页尚未就绪或由维护操作占用。请稍后重试并查看本机日志。")


def _open_directory(root: Path):
    if os.name == "nt":
        os.startfile(str(root))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(root)])


def _record_event(root: Path | None, event: str, *, error_type: str | None = None):
    """Never log exception text, config contents, credentials or environment."""
    if root is None:
        return
    with suppress(OSError):
        path = root / "logs/desktop.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1024 * 1024:
            old = path.with_suffix(".log.1")
            old.unlink(missing_ok=True)
            path.replace(old)
        value = {"time": datetime.now(timezone.utc).isoformat(), "event": event}
        if error_type:
            value["error_type"] = error_type
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def _emit_result(root: Path, result: dict, *, check=False):
    if check:
        _atomic_json(root / "check-result.json", result)
        # Retain the internal diagnostic location for older launch tooling.
        _atomic_json(root / "data/desktop-check.json", result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    cli = any(value in arguments for value in ("--check", "--stop", "--no-browser", "--help", "-h"))
    if cli:
        _connect_parent_output()
    _ensure_streams()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Persistent installation directory; never uses the embedded temporary directory")
    parser.add_argument("--check", action="store_true", help="Initialize missing safe defaults, validate without opening SQLite, and output JSON")
    parser.add_argument("--no-browser", action="store_true", help="Run without opening the local browser")
    parser.add_argument("--port", type=int, help="Explicit HTTP port; otherwise selects a free port from 8080 through 8099")
    parser.add_argument("--stop", action="store_true", help="Stop this installation gracefully; never kills a PID")
    parser.add_argument("--open-data", action="store_true", help="Open the persistent data directory and exit")
    parser.add_argument("--demo-init", action="store_true", help="Create missing simulation sample points; never creates synthetic history")
    args = parser.parse_args(arguments)
    if sum((args.check, args.stop, args.open_data)) > 1:
        parser.error("--check, --stop and --open-data are mutually exclusive")
    root = None
    try:
        root = resolve_root(args.root, write_probe=not args.stop)
        if args.stop:
            result = request_stop(root, timeout=35)
            _record_event(root, "stop_completed" if result["stopped"] else "stop_incomplete")
            _emit_result(root, result)
            return 0 if result["stopped"] else 1
        if args.open_data:
            _open_directory(root)
            return 0
        import launch
        try:
            with ProcessLock(root) as owner:
                initialize_installation(root, bundle_root(), demo_init=args.demo_init)
                checked = launch.check_installation(root)
                if args.check:
                    _emit_result(root, {"ok": True, "desktop": True, "standalone": bool(getattr(sys, "frozen", False)),
                                        **checked}, check=True)
                    return 0
                port = choose_port(args.port)
                metadata = root / "data/desktop-instance.json"
                _atomic_json(metadata, {"root": str(root), "port": port, "owner_started_at": owner.entry["started_at"]})
                _record_event(root, "desktop_started")
                if args.port is None and port != 8080 and not args.no_browser:
                    _notify(f"默认8080端口已被占用，软件将打开 http://127.0.0.1:{port} 。\n数据目录：{root}")
                try:
                    launch.run_gateway(root, no_browser=args.no_browser, port=port)
                finally:
                    metadata.unlink(missing_ok=True)
                _record_event(root, "desktop_stopped")
                return 0
        except AlreadyRunning:
            if args.check:
                checked = launch.check_installation(root)
                _emit_result(root, {"ok": True, "desktop": True, "running": True,
                                    "standalone": bool(getattr(sys, "frozen", False)), **checked}, check=True)
            else:
                _emit_result(root, open_existing_instance(root, no_browser=args.no_browser))
            return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, DesktopError) else "startup_failed"
        _record_event(root, code, error_type=type(exc).__name__)
        message = str(exc) if isinstance(exc, DesktopError) else "软件启动或操作未完成。请检查连接配置、数据目录权限及本机运行日志。"
        result = {"ok": False, "error": code, "message": message}
        if root:
            result["root"] = str(root)
            result["log"] = str(root / "logs/desktop.log")
        if args.check and root:
            with suppress(OSError):
                _atomic_json(root / "check-result.json", result)
                _atomic_json(root / "data/desktop-check.json", result)
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr, flush=True)
        if not cli:
            suffix = f"\n数据目录：{root}\n日志：{root / 'logs/desktop.log'}" if root else ""
            _notify(message + suffix, error=True)
        return 1
