"""SQLite storage, versioned point definitions and recoverable schema upgrades."""

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


SCHEMA_VERSION = 2
IDENTITY_FIELDS = ("device", "address", "type", "node_id")


class RevisionConflict(ValueError):
    """The caller edited an obsolete copy of the point table."""

    def __init__(self, expected, actual):
        self.expected, self.actual = expected, actual
        super().__init__(f"点表版本冲突：提交版本 {expected}，当前版本 {actual}，请刷新后重试")


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migration_backup = None
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            existing = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tag_info'").fetchone()
            if version > SCHEMA_VERSION:
                raise ValueError(f"数据库版本 {version} 高于本程序支持的 {SCHEMA_VERSION}")
            if existing and version < SCHEMA_VERSION:
                target = self.path.with_name(f"{self.path.stem}.pre-v{SCHEMA_VERSION}-{time.time_ns()}.sqlite3")
                self.backup(target)
                self.migration_backup = str(target)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            self._migrate(db)

    @staticmethod
    def _migrate(db):
        # executescript would commit an active migration, so keep individual statements.
        statements = [
            "CREATE TABLE IF NOT EXISTS tag_info (id INTEGER PRIMARY KEY, definition TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)",
            "CREATE TABLE IF NOT EXISTS history_data (id INTEGER PRIMARY KEY AUTOINCREMENT, tag_id INTEGER NOT NULL REFERENCES tag_info(id), timestamp REAL NOT NULL, value REAL, quality TEXT NOT NULL, source TEXT NOT NULL, device TEXT NOT NULL, name TEXT NOT NULL, unit TEXT NOT NULL, data_type TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS gateway_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tag_identity (tag_id INTEGER PRIMARY KEY REFERENCES tag_info(id), identity TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS tag_definitions (tag_id INTEGER NOT NULL REFERENCES tag_info(id), revision INTEGER NOT NULL, definition TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(tag_id, revision))",
            "CREATE TABLE IF NOT EXISTS gateway_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE, timestamp REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, connection_id TEXT, start REAL, end REAL, count INTEGER NOT NULL DEFAULT 1, tag_ids TEXT NOT NULL DEFAULT '[]')",
            "CREATE TABLE IF NOT EXISTS write_audit (id INTEGER PRIMARY KEY, timestamp REAL, tag_id INTEGER, mode TEXT, value TEXT, outcome TEXT)",
        ]
        for statement in statements:
            db.execute(statement)
        columns = {row[1] for row in db.execute("PRAGMA table_info(history_data)")}
        for name, definition in {
            "connection_id": "TEXT NOT NULL DEFAULT 'legacy'",
            "tag_revision": "INTEGER NOT NULL DEFAULT 1",
            "source_timestamp": "REAL",
            "server_timestamp": "REAL",
            "sample_key": "TEXT",
        }.items():
            if name not in columns:
                db.execute(f"ALTER TABLE history_data ADD COLUMN {name} {definition}")
        for statement in [
            "CREATE INDEX IF NOT EXISTS idx_history_tag_time ON history_data(tag_id,timestamp,id)",
            "CREATE INDEX IF NOT EXISTS idx_history_time ON history_data(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_history_filter ON history_data(device,name,timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_history_identity ON history_data(tag_id,connection_id,tag_revision,timestamp,id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_history_sample_key ON history_data(sample_key) WHERE sample_key IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_events_time ON gateway_events(start,end,connection_id)",
        ]:
            db.execute(statement)
        for row in db.execute("SELECT id,definition FROM tag_info").fetchall():
            definition = json.loads(row["definition"])
            definition.setdefault("revision", 1)
            encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True)
            identity = json.dumps({key: definition.get(key, "") for key in IDENTITY_FIELDS}, sort_keys=True)
            db.execute("UPDATE tag_info SET definition=? WHERE id=?", (encoded, row["id"]))
            db.execute("INSERT OR IGNORE INTO tag_identity(tag_id,identity) VALUES(?,?)", (row["id"], identity))
            db.execute("INSERT OR IGNORE INTO tag_definitions VALUES(?,?,?,?)", (row["id"], definition["revision"], encoded, time.time()))
        initial_revision = 1 if db.execute("SELECT 1 FROM tag_info LIMIT 1").fetchone() else 0
        db.execute("INSERT OR IGNORE INTO gateway_meta VALUES('tags_revision',?)", (str(initial_revision),))
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def backup(self, destination, timeout=30):
        target = Path(destination)
        if target.resolve() == self.path.resolve() or target.exists():
            raise ValueError("备份必须使用尚不存在的新文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise TimeoutError("数据库一致性备份超过30秒，请在低负载时重试")

        with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(target)) as output:
            source.backup(output, pages=256, progress=progress, sleep=.05)
            if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份完整性校验失败")
        return str(target)

    def tags(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT definition FROM tag_info WHERE active=1 ORDER BY id")]

    def tag_definition(self, tag_id, revision):
        with self.connect() as db:
            row = db.execute("SELECT definition FROM tag_definitions WHERE tag_id=? AND revision=?",
                             (tag_id, revision)).fetchone()
            return json.loads(row[0]) if row else None

    def tags_revision(self):
        with self.connect() as db:
            return int(db.execute("SELECT value FROM gateway_meta WHERE key='tags_revision'").fetchone()[0])

    def validate_replacement(self, tags):
        """Validate all permanent identities, including retired IDs, without writing."""
        seen_ids = set()
        with self.connect() as db:
            db.execute("BEGIN")
            for tag in tags:
                definition = tag.model_dump() if hasattr(tag, "model_dump") else dict(tag)
                tag_id = definition["id"]
                if tag_id in seen_ids:
                    raise ValueError("点位ID不能重复")
                seen_ids.add(tag_id)
                identity = json.dumps({key: definition.get(key, "") for key in IDENTITY_FIELDS}, sort_keys=True)
                old = db.execute("SELECT identity FROM tag_identity WHERE tag_id=?", (tag_id,)).fetchone()
                if old and old[0] != identity:
                    raise ValueError(f"ID {tag_id}永久绑定原设备/地址/类型/NodeId，改变身份请使用新ID")
        return {"valid": True, "count": len(seen_ids)}

    def replace_tags(self, tags, expected_revision=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            actual = int(db.execute("SELECT value FROM gateway_meta WHERE key='tags_revision'").fetchone()[0])
            if expected_revision is not None and expected_revision != actual:
                raise RevisionConflict(expected_revision, actual)
            definitions = []
            seen_ids = set()
            for tag in tags:
                definition = tag.model_dump() if hasattr(tag, "model_dump") else dict(tag)
                definition.pop("revision", None)
                tag_id = definition["id"]
                if tag_id in seen_ids:
                    raise ValueError("点位ID不能重复")
                seen_ids.add(tag_id)
                identity = json.dumps({key: definition.get(key, "") for key in IDENTITY_FIELDS}, sort_keys=True)
                old_identity = db.execute("SELECT identity FROM tag_identity WHERE tag_id=?", (tag_id,)).fetchone()
                if old_identity and old_identity[0] != identity:
                    raise ValueError(f"ID {tag_id}永久绑定原设备/地址/类型/NodeId，改变身份请使用新ID")
                old = db.execute("SELECT definition FROM tag_info WHERE id=?", (tag_id,)).fetchone()
                revision = 1
                if old:
                    previous = json.loads(old[0])
                    revision = previous.pop("revision", 1)
                    if previous != definition:
                        revision += 1
                definition["revision"] = revision
                definitions.append((tag_id, json.dumps(definition, ensure_ascii=False, sort_keys=True), identity, revision))
            db.execute("UPDATE tag_info SET active=0")
            for tag_id, encoded, identity, revision in definitions:
                db.execute("INSERT INTO tag_info(id,definition,active) VALUES(?,?,1) ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,active=1", (tag_id, encoded))
                db.execute("INSERT OR IGNORE INTO tag_identity VALUES(?,?)", (tag_id, identity))
                db.execute("INSERT OR IGNORE INTO tag_definitions VALUES(?,?,?,?)", (tag_id, revision, encoded, time.time()))
            new_revision = actual + 1
            db.execute("UPDATE gateway_meta SET value=? WHERE key='tags_revision'", (str(new_revision),))
            return new_revision

    def insert_history(self, rows, progress=None):
        if not rows and progress is None:
            return
        enriched = [tuple(row) + ("legacy", 1, None, None, None) if len(row) == 9 else tuple(row) for row in rows]
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO history_data(tag_id,timestamp,value,quality,source,device,name,unit,data_type,connection_id,tag_revision,source_timestamp,server_timestamp,sample_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", enriched)
            if progress is not None:
                db.execute("INSERT INTO gateway_meta VALUES('storage_progress',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(progress),))

    def cleanup(self, cutoff):
        with self.connect() as db:
            return db.execute("DELETE FROM history_data WHERE timestamp < ?", (cutoff,)).rowcount

    def get_meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM gateway_meta WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO gateway_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def record_event(self, kind, detail, connection_id=None, start=None, end=None, count=1, tag_ids=None, event_id=None):
        event_id = event_id or uuid.uuid4().hex
        encoded = json.dumps(detail, ensure_ascii=False)
        with self.connect() as db:
            db.execute("INSERT INTO gateway_events(event_id,timestamp,kind,detail,connection_id,start,end,count,tag_ids) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET detail=excluded.detail,end=excluded.end,count=excluded.count,tag_ids=excluded.tag_ids", (event_id, time.time(), kind, encoded, connection_id, start, end, count, json.dumps(tag_ids or [])))
        return event_id

    def events(self, limit=100):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM gateway_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),))]
        for row in rows:
            row["detail"] = json.loads(row["detail"])
            row["tag_ids"] = json.loads(row["tag_ids"])
        return rows
