"""On-demand, read-only process data for local questions and future AI adapters."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Query
from typing import Literal
from .models import AIQuery, threshold_crossed


class LocalDataProvider:
    """An AI adapter receives data callbacks only, never a PLC driver."""

    HISTORY_SCAN_LIMIT = 500000

    def __init__(self, snapshot, query_history, definition=None):
        self.current = snapshot
        self.history = query_history
        self.definition = definition

    def history_data(self, *, changed_only=False, **filters):
        """Filter an ordered range before pagination, retaining each series' baseline."""
        if not changed_only:
            return self.history(**filters)
        limit = filters.pop("limit", 2000)
        offset = filters.pop("offset", 0)
        page_size = min(10000, self.HISTORY_SCAN_LIMIT)
        first = self.history(**filters, limit=page_size, offset=0)
        # Freeze the default time window so each read uses the same boundaries.
        filters.update(start=first["start"], end=first["end"])
        total = first["total"]
        current = self.current()
        definitions = {(t["id"], t.get("revision", 1)): t for t in current["items"]}
        baselines, unknown, selected = {}, set(), []
        scanned, kept = 0, 0
        batch = first["items"]
        while batch and scanned < self.HISTORY_SCAN_LIMIT:
            for raw in batch:
                if scanned >= self.HISTORY_SCAN_LIMIT:
                    break
                scanned += 1
                key = (raw["tag_id"], raw.get("source"), raw.get("connection_id"), raw.get("tag_revision", 1))
                definition_key = (key[0], key[3])
                if definition_key not in definitions:
                    definitions[definition_key] = self.definition(*definition_key) if self.definition else None
                definition = definitions[definition_key]
                previous = baselines.get(key)
                reason = None
                if previous is None:
                    reason = "baseline"
                elif raw["quality"] != previous["quality"] or ((raw["value"] is None) != (previous["value"] is None)):
                    reason = "quality_change"
                elif definition is None:
                    # Unknown definitions must never silently discard historical data.
                    reason = "definition_unavailable"
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
                           "precision": definition.get("precision", 5) if definition else 5}
                    baselines[key] = raw
                    if offset <= kept < offset + limit:
                        selected.append(row)
                    kept += 1
            if scanned >= total or scanned >= self.HISTORY_SCAN_LIMIT:
                break
            next_page = self.history(**filters, limit=page_size, offset=scanned)
            batch = next_page["items"]
            total = max(total, next_page["total"])
        truncated = scanned < total
        has_more = offset + len(selected) < kept
        note = first.get("note", "") + " 变化数据按每个点位及定义版本的阈值比较上次保留基准；包含区间首样本、BOOL翻转和质量变化。"
        if truncated:
            note += f" 仅检查前{scanned}条原始记录，结果不完整；请缩短时间范围或选择单个变量。"
        if unknown:
            note += " 部分历史定义不可用，其记录全部保留。"
        return {**first, "items": selected, "total": kept, "limit": limit, "offset": offset,
                "has_more": has_more, "next_offset": offset + len(selected) if has_more else None,
                "changed_only": True, "truncated": truncated, "note": note,
                "filter": {"raw_total": total, "scanned": scanned, "filtered_total": kept,
                           "complete": not truncated, "scan_limit": self.HISTORY_SCAN_LIMIT,
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

    def analyze(self, request):
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
        if realtime or (not historical and matches and all(not x.get("save", True) for x in matches)):
            if request.connection_id and request.connection_id != current.get("connection_id"):
                return {"provider": "local_rules", "answer": "所选连接不是当前连接，请查询该连接的历史数据。",
                        "plc_write_allowed": False}
            return self._current_answer(current, variable)
        if not variable:
            return {"provider":"local_rules","answer":"请选择变量，或在问题中输入完整变量名称。支持当前值、今天、昨天、最近一周及所选时间范围。","plc_write_allowed":False}
        start, end = request.start, request.end
        if not start and not end:
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            today = now.replace(hour=0,minute=0,second=0,microsecond=0)
            if "昨天" in request.question: start,end = (today-timedelta(days=1)).isoformat(),today.isoformat()
            elif "今天" in request.question: start = today.isoformat()
            elif any(k in request.question for k in ("一周", "7天", "七天")):
                start,end = (now-timedelta(days=7)).isoformat(),now.isoformat()
        connection_id = request.connection_id or current.get("connection_id")
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


def router(provider):
    api = APIRouter(prefix="/api/ai",tags=["AI只读数据服务"])

    @api.get("/current")
    def current(device: str | None = None): return provider.current(device)

    @api.get("/history")
    def history(device: str | None=None, variable: str | None=None, start: str | None=None,
                end: str | None=None, source: Literal['simulation','opcua'] | None=None,
                limit: int=Query(2000,ge=1,le=10000),offset: int=Query(0,ge=0),
                connection_id: str | None=None, tag_revision: int | None=Query(None,ge=1),
                changed_only: bool=True):
        current = provider.current()
        selected_source = source or current['mode']
        if connection_id is None and selected_source == current['mode']:
            connection_id = current.get('connection_id')
        return provider.history_data(device=device,variable=variable,start=start,end=end,source=selected_source,
                                     limit=limit,offset=offset,connection_id=connection_id,
                                     tag_revision=tag_revision,changed_only=changed_only)

    @api.get("/status")
    def status(device: str | None=None): return provider.status(device)

    @api.post("/query")
    def query(request: AIQuery): return provider.analyze(request)

    return api
