"""Versioned historical samples and complete-range queries."""

import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from .models import threshold_crossed


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def epoch(value):
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def time_range(start=None, end=None):
    def parse(value):
        if value is None:
            return None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return dt.timestamp()
    stop = parse(end) if end else time.time()
    begin = parse(start) if start else stop - 86400
    if begin >= stop:
        raise ValueError("开始时间必须早于结束时间")
    if stop - begin > 31 * 86400:
        raise ValueError("单次查询范围不能超过31天")
    return begin, stop


class History:
    def __init__(self, db, heartbeat=1800, retention=7):
        self.db, self.heartbeat, self.retention = db, heartbeat, retention
        self.last = {}
        self.last_monotonic = {}
        with db.connect() as conn:
            rows = conn.execute("""SELECT h.* FROM history_data h JOIN
                (SELECT MAX(id) id FROM history_data
                 GROUP BY tag_id,source,connection_id,tag_revision) x ON h.id=x.id""").fetchall()
        for row in rows:
            key = (row["tag_id"], row["source"], row["connection_id"], row["tag_revision"])
            self.last[key] = (row["value"], row["quality"], row["timestamp"])

    def save(self, tags, readings, source, now=None, connection_id=None, monotonic=None, batch_id=None):
        now = time.time() if now is None else now
        connection_id = connection_id or "legacy"
        rows, updates = [], {}
        for tag in tags:
            if not tag.save or tag.id not in readings:
                continue
            reading = readings[tag.id]
            revision = getattr(tag, "revision", 1)
            key = (tag.id, source, connection_id, revision)
            value, quality = reading["value"], reading["quality"]
            received = epoch(reading.get("timestamp"))
            stamp = now if received is None else received
            previous = self.last.get(key)
            interval = getattr(tag, "history_interval_seconds", None) or self.heartbeat
            changed = previous is None
            if previous:
                old, old_quality, old_stamp = previous
                if monotonic is None:
                    heartbeat_due = now - old_stamp >= interval or now < old_stamp
                elif key not in self.last_monotonic:
                    # After restart a fresh baseline avoids relying on a changed wall clock.
                    heartbeat_due = True
                else:
                    heartbeat_due = monotonic - self.last_monotonic[key] >= interval
                changed = quality != old_quality or heartbeat_due
                if getattr(tag, "record_changes", True) and value is not None and old is not None and value != old:
                    changed |= tag.type == "BOOL" or threshold_crossed(value, old, tag.threshold)
                elif getattr(tag, "record_changes", True) and (value is None) != (old is None):
                    changed = True
            if changed:
                sample_key = f"{batch_id}:{tag.id}" if batch_id else None
                rows.append((tag.id, stamp, value, quality, source, tag.device, tag.name, tag.unit, tag.type,
                             connection_id, revision, epoch(reading.get("source_timestamp")),
                             epoch(reading.get("server_timestamp")), sample_key))
                updates[key] = (value, quality, stamp)
        if batch_id:
            self.db.insert_history(rows, progress={"batch_id": batch_id, "timestamp": now,
                                   "connection_id": connection_id, "source": source})
        else:
            self.db.insert_history(rows)
        # Cleanup runs independently, so a cleanup failure cannot cause this batch to retry.
        self.last.update(updates)
        if monotonic is not None:
            self.last_monotonic.update({key: monotonic for key in updates})
        return len(rows)

    @staticmethod
    def _where(begin, stop, device=None, variable=None, tag_id=None, source=None,
               connection_id=None, tag_revision=None):
        where, params = ["timestamp>=?", "timestamp<=?"], [begin, stop]
        for column, value in [("device", device), ("name", variable), ("tag_id", tag_id),
                              ("source", source), ("connection_id", connection_id),
                              ("tag_revision", tag_revision)]:
            if value is not None and value != "":
                where.append(f"{column}=?")
                params.append(value)
        return " AND ".join(where), params

    def query(self, device=None, variable=None, tag_id=None, start=None, end=None,
              source=None, limit=2000, offset=0, connection_id=None, tag_revision=None,
              snapshot_max_id=None):
        if snapshot_max_id is not None and (isinstance(snapshot_max_id, bool)
                                            or not isinstance(snapshot_max_id, int)
                                            or snapshot_max_id < 0):
            raise ValueError("历史快照记录上界必须为非负整数")
        begin, stop = time_range(start, end)
        clause, params = self._where(begin, stop, device, variable, tag_id, source, connection_id, tag_revision)
        with self.db.connect() as conn:
            conn.execute("BEGIN")  # Count, page and summary share one WAL read snapshot.
            if snapshot_max_id is None:
                snapshot_max_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM history_data").fetchone()[0]
            # Follow-up pages use this same insertion boundary. Delayed writer
            # commits with earlier timestamps cannot shift their OFFSET rows.
            clause += " AND history_data.id<=?"
            params.append(snapshot_max_id)
            total = conn.execute(f"SELECT COUNT(*) FROM history_data WHERE {clause}", params).fetchone()[0]
            rows = [dict(x) for x in conn.execute(
                f"""SELECT history_data.*,
                COALESCE((SELECT json_extract(d.definition, '$.precision') FROM tag_definitions d
                          WHERE d.tag_id=history_data.tag_id AND d.revision=history_data.tag_revision), 5) AS precision
                FROM history_data WHERE {clause} ORDER BY timestamp,id LIMIT ? OFFSET ?""",
                [*params, limit, offset])]
            summary = [dict(x) for x in conn.execute(f"""SELECT tag_id,device,name,unit,data_type,source,connection_id,tag_revision,
                COUNT(*) samples, SUM(CASE WHEN quality='Good' THEN 1 ELSE 0 END) good_samples,
                MIN(CASE WHEN quality='Good' THEN value END) minimum,
                MAX(CASE WHEN quality='Good' THEN value END) maximum,
                AVG(CASE WHEN quality='Good' THEN value END) mean
                FROM history_data WHERE {clause}
                GROUP BY tag_id,device,name,unit,data_type,source,connection_id,tag_revision""", params)]
        for row in rows:
            for field in ("timestamp", "source_timestamp", "server_timestamp"):
                row[field] = iso(row[field]) if row[field] is not None else None
            row.pop("sample_key", None)
            if row["data_type"] == "BOOL" and row["value"] is not None:
                row["value"] = bool(row["value"])
        return dict(items=rows, total=total, limit=limit, offset=offset, has_more=offset+len(rows)<total,
                    start=iso(begin), end=iso(stop), summary=summary, snapshot_max_id=snapshot_max_id,
                    note="统计基于全范围已保存样本；变化触发采样的均值不是时间加权均值。")

    def variables(self, device=None, source=None):
        conditions, params = [], []
        for field, value in (("h.device", device), ("h.source", source)):
            if value:
                conditions.append(f"{field}=?")
                params.append(value)
        clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self.db.connect() as conn:
            conn.execute("BEGIN")
            current = {row["id"]: json.loads(row["definition"]).get("revision", 1)
                       for row in conn.execute("SELECT id,definition FROM tag_info WHERE active=1")}
            rows = [dict(row) for row in conn.execute(f"""SELECT h.tag_id,h.tag_revision,h.connection_id,
                h.source,h.device,h.name,h.unit,h.data_type,
                MIN(h.timestamp) first_timestamp,MAX(h.timestamp) last_timestamp,
                COALESCE(t.active,0) active
                FROM history_data h LEFT JOIN tag_info t ON t.id=h.tag_id {clause}
                GROUP BY h.tag_id,h.tag_revision,h.connection_id,h.source,h.device,h.name,h.unit,h.data_type
                ORDER BY h.device,h.name,h.tag_id,h.tag_revision""", params)]
        for row in rows:
            row["first_timestamp"] = iso(row["first_timestamp"])
            row["last_timestamp"] = iso(row["last_timestamp"])
            row["active"] = bool(row["active"]) and current.get(row["tag_id"]) == row["tag_revision"]
        return rows

    def series(self, tag_id, start=None, end=None, source=None, connection_id=None, tag_revision=None, buckets=500):
        if not 1 <= buckets <= 2000:
            raise ValueError("曲线分桶数必须为1至2000")
        begin, stop = time_range(start, end)
        width = (stop - begin) / buckets
        clause, params = self._where(begin, stop, tag_id=tag_id, source=source,
                                     connection_id=connection_id, tag_revision=tag_revision)
        identity = "tag_id,tag_revision,connection_id,source,device,name,unit,data_type"
        with self.db.connect() as conn:
            conn.execute("BEGIN")
            # SQL reduces the full interval before returning rows; LIMIT never clips the tail.
            rows = [dict(row) for row in conn.execute(f"""WITH bucketed AS (
                SELECT *, MIN(?,CAST((timestamp-?)/? AS INTEGER)) bucket
                FROM history_data WHERE {clause}
            ), ranked AS (
                SELECT *,ROW_NUMBER() OVER(PARTITION BY {identity},bucket ORDER BY timestamp,id) first_rank,
                ROW_NUMBER() OVER(PARTITION BY {identity},bucket ORDER BY timestamp DESC,id DESC) last_rank
                FROM bucketed
            )
            SELECT {identity},bucket,COUNT(*) count,
                MAX(CASE WHEN first_rank=1 AND quality='Good' THEN value END) first,
                MAX(CASE WHEN last_rank=1 AND quality='Good' THEN value END) last,
                MIN(CASE WHEN quality='Good' THEN value END) minimum,
                MAX(CASE WHEN quality='Good' THEN value END) maximum,
                SUM(CASE WHEN quality!='Good' THEN 1 ELSE 0 END) bad_count
            FROM ranked GROUP BY {identity},bucket ORDER BY {identity},bucket""",
                [buckets-1, begin, width, *params])]
            gaps = [dict(row) for row in conn.execute(
                "SELECT connection_id,start,end,tag_ids,kind FROM gateway_events WHERE kind IN ('queue_overflow','suspect_restart_gap','shutdown_gap') AND start<=? AND end>=?",
                (stop, begin))]
        grouped = {}
        fields = identity.split(",")
        for row in rows:
            key = tuple(row[field] for field in fields)
            if key not in grouped:
                grouped[key] = {**{field: row[field] for field in fields}, "items": [
                    {"timestamp": iso(begin + index * width), "end": iso(begin + (index + 1) * width),
                     "first": None, "last": None, "minimum": None, "maximum": None,
                     "quality": "NoData", "count": 0} for index in range(buckets)]}
            item = grouped[key]["items"][row["bucket"]]
            item.update({field: row[field] for field in ("first", "last", "minimum", "maximum", "count")})
            item["quality"] = "BadData" if row["bad_count"] else "Good"
        for group in grouped.values():
            for gap in gaps:
                if gap["connection_id"] not in (None, group["connection_id"]):
                    continue
                tag_ids = json.loads(gap["tag_ids"])
                if tag_ids and tag_id not in tag_ids:
                    continue
                left = max(0, min(buckets-1, int((gap["start"]-begin)/width)))
                right = max(0, min(buckets-1, int((gap["end"]-begin)/width)))
                for index in range(left, right+1):
                    group["items"][index]["quality"] = "Gap"
        groups = list(grouped.values())
        return {"tag_id": tag_id, "start": iso(begin), "end": iso(stop), "buckets": buckets,
                "series": groups, "items": groups[0]["items"] if len(groups) == 1 else []}

    @staticmethod
    def sample_trend(items):
        values = [row["value"] for row in items if row["quality"] == "Good" and row.get("value") is not None]
        return float(values[-1]) - float(values[0]) if len(values) >= 2 else None
