"""V1.1 composition root. Production startup never seeds demonstration history."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import secrets
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from scripts.runtime import ProcessLock

from .ai_api import LocalDataProvider, router as ai_router
from .configuration import ConfigStore
from .database import Database
from .history import History
from .llm import AIConfigStore, ModelService
from .observability import Operations, Redactor, configure_logging
from .opcua_client import Gateway
from .routes import make_router
from .tag_manager import import_excel
from .version import APP_VERSION

ROOT = Path(__file__).resolve().parents[1]
_active_apps = set()
_app_guard = threading.RLock()


def create_app(root=ROOT):
    root = Path(root).resolve()
    with ProcessLock(root):
        with _app_guard:
            if root in _active_apps:
                raise RuntimeError("该目录已有运行中的应用或仍在完成停机")
        config = ConfigStore(root)
        db = Database(root / "data/history.db")
        if not db.tags() and db.tags_revision() == 0 and (root / "data/tags.xlsx").exists():
            db.replace_tags(import_excel((root / "data/tags.xlsx").read_bytes()))
        values = config.snapshot()
        history = History(db, values["heartbeat_seconds"], values["retention_days"])
        gateway = Gateway(db, history, dict(values))
        pin_path = root / "data/operator_pin.txt"
        if not pin_path.exists():
            fd = os.open(pin_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(24))
        operator_pin = pin_path.read_text(encoding="utf-8").strip()
        if not operator_pin:
            raise ValueError("管理口令文件为空，请修复data/operator_pin.txt后启动")
        redactor = Redactor([operator_pin, values["username"], os.environ.get(values["password_env"], "")])
        ai_config = AIConfigStore(root)
        redactor.update([*redactor.secrets, ai_config.snapshot()[1]])
        logger, log_handler = configure_logging(root, redactor)
        operations = Operations(root, gateway, db, config, redactor, logger)
        with db.connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS write_audit (
                id INTEGER PRIMARY KEY, timestamp REAL, tag_id INTEGER,
                mode TEXT, value TEXT, outcome TEXT)""")

    @asynccontextmanager
    async def lifespan(app):
        deferred_close = False
        claimed = False

        def release_application():
            logger.removeHandler(log_handler)
            log_handler.close()
            if claimed:
                with _app_guard:
                    _active_apps.discard(root)

        try:
            with ProcessLock(root):
                with _app_guard:
                    if root in _active_apps:
                        raise RuntimeError("该目录只允许一个运行中的应用")
                    _active_apps.add(root)
                    claimed = True
                try:
                    gateway.start()
                    operations.start()
                    yield
                finally:
                    deadline = time.monotonic() + 30
                    try:
                        await asyncio.to_thread(operations.stop, timeout=1)
                    except Exception as exc:
                        logger.error("Operational shutdown failed: %s", redactor.text(exc))
                    try:
                        result = await asyncio.to_thread(gateway.stop, timeout=max(0, deadline-time.monotonic()))
                    except Exception as exc:
                        logger.error("Acquisition shutdown failed: %s", redactor.text(exc))
                        result = {"collector_stopped": False, "storage_stopped": False, "drained": False}
                    workers = [gateway.thread, gateway.writer._thread, gateway.writer._cleanup_thread,
                               operations.thread, operations.backup_thread]
                    result["operations_stopped"] = not any(t and t.is_alive() for t in workers[3:])
                    operations.record("shutdown", result, persist=False)
                    app.state.shutdown_result = result
                    if any(t and t.is_alive() for t in workers):
                        # Return on the deadline but retain ownership while any worker can still
                        # touch SQLite. Offline recovery must not race a late commit.
                        lease = ProcessLock(root)
                        lease.__enter__()
                        deferred_close = True

                        def reap():
                            try:
                                for worker in workers:
                                    if worker and worker.ident is not None:
                                        worker.join()
                            finally:
                                release_application()
                                lease.__exit__(None, None, None)

                        threading.Thread(target=reap, name="gateway-shutdown-reaper", daemon=True).start()
        finally:
            if not deferred_close:
                release_application()

    app = FastAPI(title="Industrial AI Gateway", version=APP_VERSION, lifespan=lifespan)
    state = SimpleNamespace(root=root, gateway=gateway, db=db, history=history, config_store=config,
                            operator_pin=operator_pin, redactor=redactor, operations=operations, ai_config_store=ai_config)

    def refresh_redaction():
        now = config.snapshot()
        # Delayed exceptions can still refer to a previous session's credentials.
        redactor.update([*redactor.secrets, operator_pin, now["username"], os.environ.get(now["password_env"], "")])

    state.refresh_redaction = refresh_redaction
    for key, value in vars(state).items():
        setattr(app.state, key, value)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.url.hostname not in ("localhost", "127.0.0.1", "::1", "testserver"):
            return JSONResponse({"detail": "仅允许本机访问"}, 403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "禁止跨站访问"}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": redactor.text(exc)}, 400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Default validation errors include raw input; never echo credential fields.
        details = [{"loc": error["loc"], "msg": redactor.text(error["msg"]), "type": error["type"]}
                   for error in exc.errors()]
        return JSONResponse({"detail": details}, 422)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        logger.error("Unhandled request error: %s", redactor.text(exc))
        return JSONResponse({"detail": "请求处理失败，请查看本机诊断日志"}, 500)

    app.include_router(make_router(state))
    provider = LocalDataProvider(lambda device=None: redactor.clean(gateway.snapshot(device)), history.query,
                                 db.tag_definition)
    model_service = ModelService(ai_config, provider, redactor)
    app.state.model_service = model_service
    app.include_router(ai_router(provider, model_service, state))
    app.mount("/", StaticFiles(directory=root / "frontend", html=True), name="frontend")
    return app
