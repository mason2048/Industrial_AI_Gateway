"""Python 3.12 foreground/service entry point. Demo initialization is explicit."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import socket
import sys
import threading
import urllib.request
import webbrowser

from scripts.runtime import AlreadyRunning, ProcessLock

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8080"


def check_python():
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Python 3.12 is required. Recreate .venv using Python 3.12.")


def is_gateway(url=URL):
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=1) as response:
            return json.load(response).get("app") == "Industrial AI Gateway"
    except Exception:
        return False


def check_installation(root: Path) -> dict:
    """Validate configuration and imports without initializing or migrating data."""
    check_python()
    path = root / "config/config.json"
    if not path.is_file():
        raise RuntimeError("Missing config/config.json. Configure the field installation first, or use --demo-init for a demo.")
    config = json.loads(path.read_text(encoding="utf-8"))
    from backend.configuration import validate_runtime, validate_credentials
    try:
        config = validate_runtime(config)
    except ValueError as exc:
        # Validation errors can echo raw credential inputs; do not print them.
        raise RuntimeError("Invalid configuration. Review config/config.json field names and values.") from exc
    validate_credentials(config, root)
    return {"python": sys.version.split()[0], "root": str(root), "mode": config["mode"],
            "read_only": True, "database_exists": (root / "data/history.db").is_file(),
            "versions": {name: importlib.metadata.version(name) for name in ("fastapi", "uvicorn", "opcua", "openpyxl")}}


def run_gateway(root: Path = ROOT, *, no_browser: bool = False,
                demo_init: bool = False, stop_event: threading.Event | None = None, port: int = 8080):
    check_python()
    root = Path(root).resolve()
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    url = f"http://127.0.0.1:{port}"
    with ProcessLock(root) as owner:
        if demo_init:
            from seed_data import seed
            seed(root)
        check_installation(root)
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Port {port} is occupied. Stop the other installation/application before starting.")
        import uvicorn
        from backend.main import create_app
        application = create_app(root)
        server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                               workers=1, timeout_graceful_shutdown=30,
                                               log_config=None))
        finished = threading.Event()
        def monitor():
            opened = no_browser
            while not finished.wait(.2):
                if owner.stop_requested() or (stop_event is not None and stop_event.is_set()):
                    server.should_exit = True
                    return
                if not opened and is_gateway(url):
                    webbrowser.open(url)
                    opened = True
        watcher = threading.Thread(target=monitor, name="gateway-control", daemon=True)
        watcher.start()
        try:
            print("Industrial AI Gateway: " + url, flush=True)
            server.run()
            if not server.started and not owner.stop_requested() and not (stop_event and stop_event.is_set()):
                raise RuntimeError("Gateway startup did not complete. Check the local log.")
            result = getattr(application.state, "shutdown_result", None)
            if result is not None:
                incomplete = [name for name in ("collector_stopped", "storage_stopped", "drained", "operations_stopped")
                              if result.get(name) is False]
                if incomplete:
                    raise RuntimeError("Shutdown is incomplete (" + ", ".join(incomplete) +
                                       "); background work may remain. No process was killed. Inspect the local logs.")
        finally:
            finished.set()
            watcher.join(timeout=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Installation directory")
    parser.add_argument("--no-browser", action="store_true", help="Run without opening a browser")
    parser.add_argument("--port", type=int, default=8080, help="Local HTTP port; defaults to 8080")
    parser.add_argument("--demo-init", action="store_true", help="Explicitly create missing demo config, templates and simulation history")
    parser.add_argument("--check", action="store_true", help="Read-only configuration/dependency check; never opens the DB")
    args = parser.parse_args(argv)
    if args.demo_init and args.check:
        parser.error("--check is read-only and cannot be combined with --demo-init")
    try:
        if args.check:
            print(json.dumps(check_installation(args.root.resolve()), ensure_ascii=False, indent=2))
        else:
            run_gateway(args.root, no_browser=args.no_browser, demo_init=args.demo_init, port=args.port)
        return 0
    except (AlreadyRunning, RuntimeError, ValueError, OSError) as exc:
        print(f"Gateway startup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
