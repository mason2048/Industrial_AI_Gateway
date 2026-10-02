"""HTTP adapters for independently owned collection, configuration and persistence."""
import io
import json
import os
import re
import secrets
import threading
import time
from typing import Literal
from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from .configuration import ConfigConflict
from .database import RevisionConflict
from .models import ConnectionConfig, Tag, typed_value
from .preflight import connection_test, validate_nodes
from .tag_manager import export_excel, import_excel, validate_tags
from .version import APP_VERSION
from scripts.runtime import request_stop, is_running


class ValidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Tag] = Field(min_length=1, max_length=1000)
    connection: ConnectionConfig | None = None


class WriteProposal(BaseModel):
    tag_id: int
    value: bool | int | float


class WriteConfirmation(BaseModel):
    request_id: str
    confirmation: str


def etag(kind, revision):
    return f'"{kind}-{revision}"'


def expected_revision(request, kind):
    value = request.headers.get("If-Match")
    if not value:
        raise HTTPException(428, "修改需要If-Match版本号，请重新加载后提交")
    match = re.fullmatch(r'"' + kind + r'-(\d+)"', value)
    if not match:
        raise HTTPException(412, "版本号无效或已过期，请重新加载")
    return int(match.group(1))


def tag_diff(before, after):
    previous = {row["id"]: row for row in before}
    incoming = {t.id: t.model_dump(exclude={"revision"}) for t in after}
    small = lambda value: {"id": value["id"], "name": value["name"]}
    return {"added": [small(incoming[k]) for k in incoming.keys() - previous.keys()],
            "removed": [small(previous[k]) for k in previous.keys() - incoming.keys()],
            "changed": [small(incoming[k]) for k in incoming.keys() & previous.keys()
                        if incoming[k] != {key: value for key, value in previous[k].items() if key != "revision"}]}


def make_router(state):
    api = APIRouter(prefix="/api")
    gateway, db, history = state.gateway, state.db, state.history
    config, operations = state.config_store, state.operations
    admin_lock = threading.RLock()
    preflight_slot = threading.Semaphore(1)
    pending = {}

    def require_admin(request):
        supplied = request.headers.get("X-Operator-Pin", "").encode("utf-8")
        if not secrets.compare_digest(supplied, state.operator_pin.encode("utf-8")):
            raise HTTPException(403, "管理操作需要本机data/operator_pin.txt中的口令")

    def simulation_only():
        values = config.snapshot()
        if values["mode"] != "simulation" or not values["simulation_write_enabled"]:
            raise HTTPException(403, "V1.1现场只读；写入仅可在显式启用的模拟测试中使用")

    @api.post("/shutdown", status_code=202)
    def shutdown(request: Request):
        require_admin(request)
        if not is_running(state.root):
            raise HTTPException(409, "未找到当前运行实例")
        # Submit a verified, cooperative stop; waiting here would hold an HTTP
        # request open while uvicorn waits for that same request to complete.
        request_stop(state.root, timeout=0)
        operations.record("shutdown_requested", {"source": "local_operator"})
        return {"status": "stopping", "message": "正在停止采集并保存剩余历史。退出后可关闭此页面。"}

    def call_preflight(request, operation):
        require_admin(request)
        if not preflight_slot.acquire(blocking=False):
            raise HTTPException(429, "已有预检运行中，请稍后重试")
        try:
            return operation()
        except ValueError:
            raise
        except Exception as exc:
            operations.record("preflight_failed", {"message": state.redactor.text(exc)})
            raise HTTPException(502, state.redactor.text(exc)) from exc
        finally:
            preflight_slot.release()

    def candidate_config(body):
        values = config.candidate(body)
        state.redactor.update([*state.redactor.secrets, values.get("username", ""),
                               os.environ.get(values.get("password_env", "PLC_OPCUA_PASSWORD"), "")])
        return values

    @api.get("/health")
    async def health():
        return {"app": "Industrial AI Gateway", "version": APP_VERSION, "status": "running"}

    @api.get("/ready")
    async def ready():
        result = operations.readiness()
        return JSONResponse(result, status_code=200 if result["ready"] else 503)

    @api.get("/diagnostics")
    async def diagnostics():
        return {"version": APP_VERSION, **operations.diagnostics()}

    @api.get("/current")
    async def current(device: str | None = None):
        return state.redactor.clean(gateway.snapshot(device))

    @api.get("/config")
    def get_config(response: Response):
        values = config.public()
        response.headers["ETag"] = etag("config", values["revision"])
        return values

    @api.post("/connection")
    def connection(body: ConnectionConfig, request: Request, response: Response):
        require_admin(request)
        expected = expected_revision(request, "config")
        with admin_lock:
            try:
                values = config.update(body.model_dump(exclude_unset=True), expected)
            except ConfigConflict as exc:
                raise HTTPException(412, str(exc)) from exc
            gateway.reconfigure(values)
            history.heartbeat = values["heartbeat_seconds"]
            history.retention = values["retention_days"]
            pending.clear()
            state.refresh_redaction()
            operations.record("connection_changed", {"revision": values["revision"], "mode": values["mode"],
                                                       "connection_id": values["connection_id"]})
            response.headers["ETag"] = etag("config", values["revision"])
            return {"message": "配置已保存，采集器将应用新连接", **config.public()}

    @api.post("/connection/test")
    def test_connection(body: ConnectionConfig, request: Request):
        def check():
            result = connection_test(candidate_config(body.model_dump(exclude_unset=True)), state.root)
            operations.record("connection_test", {"success": result["success"]})
            return result
        return call_preflight(request, check)

    @api.get("/tags")
    def tags(response: Response):
        with admin_lock:
            values = db.tags()
            response.headers["ETag"] = etag("tags", db.tags_revision())
            return values

    @api.get("/tags/next-id")
    def next_tag_id(response: Response):
        with admin_lock:
            next_id, revision = db.next_tag_id()
            response.headers["ETag"] = etag("tags", revision)
            return {"next_id": next_id}

    @api.put("/tags")
    def replace(items: list[Tag], request: Request, response: Response):
        require_admin(request)
        expected = expected_revision(request, "tags")
        with admin_lock:
            try:
                gateway.replace_tags(validate_tags(items), expected_revision=expected)
            except RevisionConflict as exc:
                raise HTTPException(412, str(exc)) from exc
            pending.clear()
            revision = db.tags_revision()
            operations.record("tags_changed", {"count": len(items), "revision": revision})
            response.headers["ETag"] = etag("tags", revision)
            return {"count": len(items), "revision": revision, "message": "点位已保存"}

    @api.post("/tags/import")
    def excel_import(request: Request, response: Response, file: UploadFile = File(...), dry_run: bool = False):
        require_admin(request)
        if not file.filename or not file.filename.lower().endswith(".xlsx"):
            raise ValueError("只支持.xlsx文件")
        try:
            items = import_excel(file.file.read(5*1024*1024+1))
        except Exception as exc:
            raise ValueError(f"导入失败，原点表保持不变：{state.redactor.text(exc)}") from exc
        with admin_lock:
            revision = db.tags_revision()
            db.validate_replacement(items)
            diff = tag_diff(db.tags(), items)
            if not dry_run:
                expected = expected_revision(request, "tags")
                try:
                    gateway.replace_tags(items, expected_revision=expected)
                except RevisionConflict as exc:
                    raise HTTPException(412, str(exc)) from exc
                pending.clear()
                revision = db.tags_revision()
                operations.record("tags_imported", {"count": len(items), "revision": revision, "diff": diff})
            response.headers["ETag"] = etag("tags", revision)
            return {"count": len(items), "revision": revision, "diff": diff,
                    "message": "验证通过，请确认差异后应用" if dry_run else "导入成功"}

    @api.get("/tags/export")
    def excel_export():
        content = export_excel(db.tags())
        return StreamingResponse(io.BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": 'attachment; filename="tags.xlsx"'})

    @api.get("/tags/template")
    def excel_template():
        content = export_excel([])
        return StreamingResponse(io.BytesIO(content), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": 'attachment; filename="plc-tags-template.xlsx"'})

    @api.post("/tags/validate")
    def validate(body: ValidationRequest, request: Request):
        def check():
            items = validate_tags(body.items)
            candidate = candidate_config(body.connection.model_dump(exclude_unset=True) if body.connection else {})
            result = validate_nodes(items, candidate, state.root)
            operations.record("tags_validated", {"count": len(result), "good": sum(x["quality"] == "Good" for x in result)})
            return {"items": result}
        return call_preflight(request, check)

    @api.get("/history/variables")
    def variables(device: str | None = None, source: Literal["simulation", "opcua"] | None = None):
        return {"items": history.variables(device=device, source=source)}

    @api.get("/history/series")
    def series(tag_id: int = Query(ge=1), start: str | None = None, end: str | None = None,
               source: Literal["simulation", "opcua"] | None = None, connection_id: str | None = None,
               tag_revision: int | None = Query(None, ge=1), buckets: int = Query(500, ge=1, le=2000)):
        return history.series(tag_id=tag_id, start=start, end=end, source=source or config.snapshot()["mode"],
                              connection_id=connection_id, tag_revision=tag_revision, buckets=buckets)

    @api.get("/history")
    def query_history(device: str | None = None, variable: str | None = None, tag_id: int | None = None,
                      start: str | None = None, end: str | None = None,
                      source: Literal["simulation", "opcua"] | None = None,
                      limit: int = Query(2000, ge=1, le=10000), offset: int = Query(0, ge=0),
                      connection_id: str | None = None, tag_revision: int | None = Query(None, ge=1)):
        return history.query(device, variable, tag_id, start, end, source or config.snapshot()["mode"], limit, offset,
                             connection_id=connection_id, tag_revision=tag_revision)

    @api.post("/operator/write-request")
    def write_request(body: WriteProposal, request: Request):
        require_admin(request)
        simulation_only()
        with admin_lock:
            now = time.monotonic()
            for key in list(pending):
                if pending[key]["expires"] < now:
                    pending.pop(key)
            if len(pending) >= 100:
                raise HTTPException(429, "待确认请求过多")
            tag = next((Tag.model_validate(t) for t in db.tags() if t["id"] == body.tag_id), None)
            if tag is None:
                raise HTTPException(404, "点位不存在")
            if tag.permission != "WRITE":
                raise HTTPException(403, "READ点位禁止写入")
            value = typed_value(body.value, tag.type)
            phrase = f"确认写入 {tag.device}/{tag.name} = {value}"
            key = secrets.token_urlsafe(24)
            pending[key] = {"tag": tag.model_dump(), "value": value, "expires": now+60,
                            "phrase": phrase, "config_revision": config.snapshot()["revision"]}
            return {"request_id": key, "confirmation": phrase, "expires_in": 60, "mode": "simulation"}

    @api.post("/operator/write-confirm")
    def write_confirm(body: WriteConfirmation, request: Request):
        require_admin(request)
        simulation_only()
        with admin_lock:
            proposal = pending.pop(body.request_id, None)
            if not proposal or proposal["expires"] < time.monotonic():
                raise HTTPException(409, "确认已过期或已使用")
            if body.confirmation != proposal["phrase"]:
                raise HTTPException(400, "人工确认文字不匹配")
            current = next((t for t in db.tags() if t["id"] == proposal["tag"]["id"]), None)
            if current != proposal["tag"] or config.snapshot()["revision"] != proposal["config_revision"]:
                raise HTTPException(409, "配置已变更，请重新发起")
            tag, value = gateway.manual_write(current["id"], proposal["value"])
            with db.connect() as conn:
                conn.execute("INSERT INTO write_audit(timestamp,tag_id,mode,value,outcome) VALUES(?,?,?,?,?)",
                             (time.time(), tag.id, "simulation", json.dumps(value), "success"))
            operations.record("simulation_write", {"tag_id": tag.id, "value": value})
            return {"tag_id": tag.id, "value": value, "mode": "simulation", "message": "模拟值已设置，下一扫描刷新"}

    return api
