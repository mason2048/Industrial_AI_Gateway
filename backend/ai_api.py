"""On-demand, read-only process data for local questions and future AI adapters."""
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, Query, Request, Response
from typing import Literal
from .models import AIQuery, threshold_crossed


class LocalDataProvider:
    """An AI adapter receives data callbacks only, never a PLC driver."""

    HISTORY_SCAN_LIMIT = 500000

    def __init__(self, snapshot, query_history, definition=None, default_interval=None):
        self.current = snapshot
        self.history = query_history
        self.definition = definition
        self.default_interval = default_interval or (lambda: 1800)

    @staticmethod
    def _elapsed_seconds(previous, current):
        try:
            start = datetime.fromisoformat(str(previous["timestamp"]).replace("Z", "+00:00"))
            stop = datetime.fromisoformat(str(current["timestamp"]).replace("Z", "+00:00"))
            if start.tzinfo is None or stop.tzinfo is None:
                return None
            return (stop - start).total_seconds()
        except (KeyError, ValueError, TypeError, OverflowError):
            return None

    @staticmethod
    def _history_policy(tag_id, definition, active, use_point_settings, default_interval):
        policy = active or definition or {}
        source = "current_point" if active is not None else "historical_definition" if definition else "default"
        mode = policy.get("ai_history_mode", "changes") if use_point_settings else "changes"
        if not use_point_settings:
            source = "legacy_changes"
        if mode not in ("changes", "interval"):
            raise ValueError(f"点位{tag_id}的AI取数方式无效")
        interval = None
        interval_source = "not_used"
        if mode == "interval":
            choices = ((policy.get("ai_history_interval_seconds"), "ai_history_interval"),
                       (policy.get("history_interval_seconds"), "history_interval"),
                       (default_interval, "global_default"))
            interval, interval_source = next((value, origin) for value, origin in choices if value is not None)
            try:
                interval = float(interval)
            except (ValueError, TypeError):
                raise ValueError(f"点位{tag_id}的AI取数间隔无效") from None
            if not math.isfinite(interval) or not 0 < interval <= 604800:
                raise ValueError(f"点位{tag_id}的AI取数间隔必须大于0且最多604800秒")
        return {"tag_id": tag_id, "ai_history_mode": mode, "policy_source": source,
                "policy_revision": policy.get("revision", 1),
                "interval_seconds": interval, "interval_source": interval_source}

    def history_data(self, *, changed_only=False, scan_limit=None, use_point_settings=False, **filters):
        """Filter an ordered range before pagination, retaining each series' baseline."""
        if not changed_only:
            return self.history(**filters)
        limit = filters.pop("limit", 2000)
        offset = filters.pop("offset", 0)
        scan_limit = min(scan_limit or self.HISTORY_SCAN_LIMIT, self.HISTORY_SCAN_LIMIT)
        page_size = min(10000, scan_limit)
        first = self.history(**filters, limit=page_size, offset=0)
        # Freeze the default time window so each read uses the same boundaries.
        filters.update(start=first["start"], end=first["end"])
        if "snapshot_max_id" in first:
            filters["snapshot_max_id"] = first["snapshot_max_id"]
        initial_total = total = first["total"]
        concurrent_change = False
        current = self.current()
        active_points = {t["id"]: t for t in current["items"]}
        definitions = {(t["id"], t.get("revision", 1)): t for t in current["items"]}
        # Freeze point preferences and the inherited global interval per request.
        default_interval = self.default_interval()
        policies, policy_summary = {}, {}
        baselines, unknown, invalid_times, selected = {}, set(), set(), []
        scanned, kept = 0, 0
        batch = first["items"]
        while batch and scanned < scan_limit:
            for raw in batch:
                if scanned >= scan_limit:
                    break
                scanned += 1
                key = (raw["tag_id"], raw.get("source"), raw.get("connection_id"), raw.get("tag_revision", 1))
                definition_key = (key[0], key[3])
                if definition_key not in definitions:
                    definitions[definition_key] = self.definition(*definition_key) if self.definition else None
                definition = definitions[definition_key]
                if definition_key not in policies:
                    policy = self._history_policy(key[0], definition, active_points.get(key[0]),
                                                  use_point_settings, default_interval)
                    policies[definition_key] = policy
                    policy_key = (policy["tag_id"], policy["policy_source"], policy["policy_revision"],
                                  policy["ai_history_mode"], policy["interval_seconds"])
                    policy_summary[policy_key] = policy
                policy = policies[definition_key]
                previous = baselines.get(key)
                reason = None
                if previous is None:
                    reason = "baseline"
                elif raw["quality"] != previous["quality"] or ((raw["value"] is None) != (previous["value"] is None)):
                    reason = "quality_change"
                elif definition is None:
                    # Unknown definitions must never silently discard historical data.
                    reason = "definition_unavailable"
                elif policy["ai_history_mode"] == "interval":
                    elapsed = self._elapsed_seconds(previous, raw)
                    if elapsed is None:
                        reason = "timestamp_unavailable"
                        invalid_times.add(definition_key)
                    elif elapsed >= policy["interval_seconds"]:
                        reason = "interval_sample"
                elif raw["value"] is not None and raw["value"] != previous["value"]:
                    if raw.get("data_type") == "BOOL":
                        reason = "state_change"
                    else:
                        threshold = definition.get("threshold", 0)
                        if threshold_crossed(raw["value"], previous["value"], threshold):
                            reason = "threshold_change"
                if definition is None:
                    unknown.add(definition_key)
                if reason:
                    row = {**raw, "change_reason": reason,
                           "threshold": definition.get("threshold", 0) if definition else None,
                           "ai_description": definition.get("ai_description", "") if definition else "",
                           "precision": definition.get("precision", 5) if definition else 5,
                           "ai_history_mode": policy["ai_history_mode"],
                           "ai_history_interval_seconds": policy["interval_seconds"],
                           "ai_history_policy_source": policy["policy_source"],
                           "ai_history_policy_revision": policy["policy_revision"],
                           "ai_history_interval_source": policy["interval_source"]}
                    baselines[key] = raw
                    if offset <= kept < offset + limit:
                        selected.append(row)
                    kept += 1
            if scanned >= total or scanned >= scan_limit:
                break
            next_page = self.history(**filters, limit=page_size, offset=scanned)
            batch = next_page["items"]
            if next_page["total"] != initial_total:
                concurrent_change = True
            total = max(total, next_page["total"])
        truncated = scanned < total or concurrent_change
        has_more = offset + len(selected) < kept
        if use_point_settings:
            policy_note = " AI历史取数按点位当前偏好：变化模式以原定义版本的阈值比较上次保留基准；时间模式按上次选中样本的时间间隔抽取已有保存样本。两种模式均保留区间首样本和质量变化，不补造采样；退役点位使用历史定义偏好。"
        else:
            policy_note = " 变化数据按每个点位及定义版本的阈值比较上次保留基准；包含区间首样本、BOOL翻转和质量变化。"
        note = first.get("note", "") + policy_note
        if truncated:
            note += f" 仅检查前{scanned}条原始记录，结果不完整；请缩短时间范围或选择单个变量。"
        if concurrent_change:
            note += " 查询期间历史记录发生变化，分页结果可能不完整；请重新查询。"
        if unknown:
            note += " 部分历史定义不可用，其记录全部保留。"
        if invalid_times:
            note += " 部分历史采集时间无效，保留相关记录，无法按时间间隔筛选；请检查数据。"
        policy_items = sorted(policy_summary.values(), key=lambda row: (row["tag_id"], row["policy_source"], row["policy_revision"]))
        return {**first, "items": selected, "total": kept, "limit": limit, "offset": offset,
                "has_more": has_more, "next_offset": offset + len(selected) if has_more else None,
                "changed_only": True, "use_point_settings": use_point_settings, "truncated": truncated, "note": note,
                "filter": {"raw_total": total, "scanned": scanned, "filtered_total": kept,
                           "complete": not truncated, "scan_limit": scan_limit, "concurrent_change": concurrent_change,
                           "point_settings": use_point_settings,
                           "point_policies": policy_items[:1000], "point_policy_count": len(policy_items),
                           "point_policies_truncated": len(policy_items) > 1000,
                           "invalid_timestamps": [{"tag_id": tag_id, "tag_revision": revision}
                                                  for tag_id, revision in sorted(invalid_times)],
                           "unknown_definitions": [{"tag_id": tag_id, "tag_revision": revision}
                                                   for tag_id, revision in sorted(unknown)]},
                "summary_scope": "all_saved_samples_in_range"}

    @staticmethod
    def _current_answer(current, variable):
        items = [x for x in current["items"] if not variable or x["name"] == variable]
        lines = []
        for item in items[:10]:
            if item["quality"] == "Good" and item.get("value") is not None:
                value = item["value"]
                if isinstance(value, bool):
                    formatted = "开" if value else "关"
                elif item["type"] in ("WORD", "DWORD"):
                    formatted = str(int(value))
                else:
                    formatted = f"{value:.{item.get('precision', 5)}f}"
                lines.append(f"{item['device']}/{item['name']}：{formatted} {item['unit']}；采集时间 {item.get('timestamp')}。")
            else:
                lines.append(f"{item['device']}/{item['name']}：当前数据不可用（{item['quality']}），请检查通信。")
        if len(items) > 10:
            lines.append(f"另有{len(items)-10}个点位，完整实时数据见items。")
        return {"provider": "local_rules", "query_type": "current",
                "answer": "\n".join(lines) if lines else "未找到匹配的当前点位，请检查变量名称。",
                "source": current["mode"], "connection_id": current.get("connection_id"),
                "connected": current["connected"], "items": items, "evidence_count": len(items),
                "variable": variable, "plc_write_allowed": False}

    def status(self, device=None):
        data = self.current(device)
        issues = [{"id":x["id"],"name":x["name"],"quality":x["quality"]} for x in data["items"] if x["quality"]!="Good"]
        return {"device":device,"mode":data["mode"],
                "health":"unknown", "health_label":"设备健康尚未评估",
                "data_status":"正常" if data["connected"] and data["items"] and not issues else "异常或无数据",
                "good":data["good"],"total":data["total"],"issues":issues,
                "storage_error":data["storage_error"],
                "explanation":"只评估采集质量。尚未配置设备工况和诊断阈值，不将通讯正常等同于设备健康。",
                "plc_write_allowed":False}

    def selection(self, request):
        current = self.current(request.device)
        variable = request.variable
        candidates = [x for x in current["items"] if x["name"] in request.question]
        if not variable and not candidates:
            candidates = [x for x in current["items"] if any(k in request.question and k in x["name"] for k in ("真空","压力","温度","电流"))]
        if not variable and len(candidates) == 1: variable = candidates[0]["name"]
        historical = bool(request.start or request.end or any(k in request.question for k in
                          ("今天", "昨天", "历史", "过去", "最近", "一周", "7天", "七天", "趋势")))
        realtime = not historical and any(k in request.question for k in ("当前", "实时", "现在", "此刻", "最新"))
        matches = [x for x in current["items"] if x["name"] == variable]
        query_type = "current" if realtime or (not historical and matches and all(not x.get("save", True) for x in matches)) else "history"
        start, end = request.start, request.end
        if not start and not end:
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            today = now.replace(hour=0,minute=0,second=0,microsecond=0)
            if "昨天" in request.question: start,end = (today-timedelta(days=1)).isoformat(),today.isoformat()
            elif "今天" in request.question: start = today.isoformat()
            elif any(k in request.question for k in ("一周", "7天", "七天")):
                start,end = (now-timedelta(days=7)).isoformat(),now.isoformat()
        connection_id = request.connection_id or current.get("connection_id")
        return {"current": current, "variable": variable, "query_type": query_type,
                "start": start, "end": end, "connection_id": connection_id}

    def analyze(self, request):
        selected = self.selection(request)
        current, variable = selected["current"], selected["variable"]
        if selected["query_type"] == "current":
            if request.connection_id and request.connection_id != current.get("connection_id"):
                return {"provider": "local_rules", "answer": "所选连接不是当前连接，请查询该连接的历史数据。",
                        "plc_write_allowed": False}
            return self._current_answer(current, variable)
        if not variable:
            return {"provider":"local_rules","answer":"请选择变量，或在问题中输入完整变量名称。支持当前值、今天、昨天、最近一周及所选时间范围。","plc_write_allowed":False}
        start, end, connection_id = selected["start"], selected["end"], selected["connection_id"]
        data = self.history(device=request.device, variable=variable,start=start,end=end,source=current["mode"],limit=2000,
                            connection_id=connection_id, tag_revision=request.tag_revision)
        summaries = [s for s in data["summary"] if s["good_samples"]]
        lines = []
        for s in summaries:
            lines.append(f"{s['device']}/{s['name']}（定义版本{s.get('tag_revision', 1)}）：保存{s['samples']}条，其中有效{s['good_samples']}条；最小{s['minimum']:.6g}、最大{s['maximum']:.6g}、保存样本均值{s['mean']:.6g} {s['unit']}。")
        if not lines: lines = ["所选范围没有有效历史数据。请检查保存开关、采集质量和时间范围。"]
        return {"provider":"local_rules","query_type":"history","answer":"\n".join(lines),"device":request.device,"variable":variable,
                "source":current["mode"],"connection_id":connection_id,"start":data["start"],"end":data["end"],"summary":data["summary"],
                "evidence_count":data["total"],"note":data["note"],"plc_write_allowed":False}


def router(provider, model_service=None, state=None):
    api = APIRouter(prefix="/api/ai",tags=["AI只读数据服务"])

    if model_service is not None:
        from .llm import AIConfigRequest, AIConflict, ModelError
        from .routes import etag, expected_revision
        import secrets

        def require_admin(request):
            supplied = request.headers.get("X-Operator-Pin", "").encode("utf-8")
            if not secrets.compare_digest(supplied, state.operator_pin.encode("utf-8")):
                raise HTTPException(403, "模型管理及推理操作需要本机data/operator_pin.txt中的口令")

        def invoke(operation):
            try:
                return operation()
            except ModelError as exc:
                raise HTTPException(exc.status_code, str(exc)) from exc

        @api.get("/config")
        def get_ai_config(response: Response):
            values = model_service.store.public()
            response.headers["ETag"] = etag("ai", values["revision"])
            return values

        @api.post("/config")
        def set_ai_config(body: AIConfigRequest, request: Request, response: Response):
            require_admin(request)
            expected = expected_revision(request, "ai")
            try:
                values = model_service.store.update(body, expected)
            except AIConflict as exc:
                raise HTTPException(412, str(exc)) from exc
            model_service._remember_key(model_service.store.snapshot()[1])
            state.operations.record("ai_configuration_changed", {"provider": values["provider"], "revision": values["revision"]})
            response.headers["ETag"] = etag("ai", values["revision"])
            return values

        @api.post("/test")
        def test_ai_config(body: AIConfigRequest, request: Request):
            require_admin(request)
            return invoke(lambda: model_service.test(body))

    @api.get("/current")
    def current(device: str | None = None): return provider.current(device)

    @api.get("/history")
    def history(device: str | None=None, variable: str | None=None, start: str | None=None,
                end: str | None=None, source: Literal['simulation','opcua'] | None=None,
                limit: int=Query(2000,ge=1,le=10000),offset: int=Query(0,ge=0),
                connection_id: str | None=None, tag_revision: int | None=Query(None,ge=1),
                changed_only: bool=True, use_point_settings: bool=True):
        current = provider.current()
        selected_source = source or current['mode']
        if connection_id is None and selected_source == current['mode']:
            connection_id = current.get('connection_id')
        return provider.history_data(device=device,variable=variable,start=start,end=end,source=selected_source,
                                     limit=limit,offset=offset,connection_id=connection_id,
                                     tag_revision=tag_revision,changed_only=changed_only,
                                     use_point_settings=use_point_settings)

    @api.get("/status")
    def status(device: str | None=None): return provider.status(device)

    @api.post("/query")
    def query(body: AIQuery, request: Request):
        if model_service is None:
            return provider.analyze(body)
        selected_config = model_service.store.snapshot()
        if selected_config[0].provider != "local_rules":
            require_admin(request)
            if expected_revision(request, "ai") != selected_config[0].revision:
                raise HTTPException(412, "模型配置已变化，请重新加载并确认模型服务后提问")
        return invoke(lambda: model_service.analyze(body, config_snapshot=selected_config))

    return api
