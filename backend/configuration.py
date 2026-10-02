"""Validated, revisioned local configuration; credentials never enter public responses."""
import json
import os
from pathlib import Path
import threading
import uuid

from pydantic import BaseModel, ConfigDict, Field
from .models import ConnectionConfig


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str = "simulation"
    endpoint: str = "opc.tcp://127.0.0.1:4840"
    poll_interval: float = Field(default=1.0, ge=0.1, le=60, allow_inf_nan=False)
    batch_size: int = Field(default=100, ge=1, le=1000)
    heartbeat_seconds: float = Field(default=1800, gt=0, le=604800, allow_inf_nan=False)
    retention_days: float = Field(default=7, gt=0, le=7, allow_inf_nan=False)
    queue_capacity: int = Field(default=120, ge=1, le=120)
    security_string: str = ""
    username: str = ""
    password_env: str = "PLC_OPCUA_PASSWORD"
    ai_provider: str = "local_rules"
    simulation_write_enabled: bool = False
    connection_id: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)
    revision: int = Field(default=1, ge=1)
    disk_min_free_mb: int = Field(default=100, ge=1)
    backup_enabled: bool = True


PUBLIC_FIELDS = ("mode", "endpoint", "poll_interval", "batch_size", "heartbeat_seconds",
                 "retention_days", "queue_capacity", "simulation_write_enabled",
                 "connection_id", "revision")


def validate_runtime(values):
    parsed = RuntimeConfig.model_validate(values).model_dump()
    ConnectionConfig.model_validate({k: parsed[k] for k in
                                    ("mode", "endpoint", "security_string", "username", "password_env")})
    return parsed


def validate_credentials(values, root):
    """Local preflight only. Does not connect or silently downgrade UA security."""
    if values["mode"] != "opcua":
        return
    if values.get("username") and not os.environ.get(values.get("password_env", "PLC_OPCUA_PASSWORD")):
        raise ValueError("PLC密码环境变量未设置；请在启动服务的账户环境中配置")
    security = values.get("security_string", "")
    if security:
        parts = [part.strip() for part in security.split(",")]
        if len(parts) not in (4, 5) or parts[1] not in ("Sign", "SignAndEncrypt"):
            raise ValueError("安全配置格式应为 Policy,Sign或SignAndEncrypt,证书,私钥[,服务器证书]")
        for name in parts[2:]:
            path = Path(name)
            if not path.is_absolute():
                raise ValueError("证书和私钥必须使用绝对路径")
            if not path.is_file():
                raise ValueError("安全配置引用的证书或私钥文件不存在")


class ConfigConflict(ValueError):
    pass


class ConfigStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / "config/config.json"
        self.lock = threading.RLock()
        if not self.path.is_file():
            raise ValueError("缺少 config/config.json。请先配置现场连接；演示数据请显式使用 --demo-init。")
        original = json.loads(self.path.read_text(encoding="utf-8"))
        self._values = validate_runtime(original)
        # Persist the initial connection identity once, so restarts do not create new series.
        if "connection_id" not in original or "revision" not in original:
            self._persist(self._values)

    def _persist(self, values):
        temp = self.path.with_suffix(".tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump(values, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)

    def snapshot(self):
        with self.lock:
            return dict(self._values)

    def public(self):
        values = self.snapshot()
        return {**{k: values[k] for k in PUBLIC_FIELDS},
                "security_configured": bool(values["security_string"]),
                "username_configured": bool(values["username"])}

    def candidate(self, patch):
        with self.lock:
            return validate_runtime({**self._values, **{k: v for k, v in patch.items() if v is not None}})

    def update(self, patch, expected_revision):
        with self.lock:
            if expected_revision != self._values["revision"]:
                raise ConfigConflict("连接配置已被其他页面修改，请重新加载")
            new = self.candidate(patch)
            validate_credentials(new, self.root)
            if (new["mode"], new["endpoint"]) != (self._values["mode"], self._values["endpoint"]):
                new["connection_id"] = uuid.uuid4().hex
            new["revision"] = self._values["revision"] + 1
            self._persist(new)
            self._values = new
            return dict(new)
