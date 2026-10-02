import math
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: int = Field(ge=1, le=2147483647)
    address: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    type: Literal["BOOL", "WORD", "DWORD", "FLOAT"]
    unit: str = Field(default="-", max_length=30)
    permission: Literal["READ", "WRITE"] = "READ"
    ai_description: str = Field(default="", max_length=500)
    save: bool = True
    threshold: float = Field(default=0, ge=0, allow_inf_nan=False)
    history_interval_seconds: float | None = Field(default=None, gt=0, le=604800, allow_inf_nan=False)
    precision: int = Field(default=5, ge=0, le=10)
    record_changes: bool = True
    device: str = Field(default="真空泵01", min_length=1, max_length=100)
    node_id: str = Field(default="", max_length=500)
    # Assigned by the database. Incoming values never control stored revisions.
    revision: int = Field(default=1, ge=1)

    @field_validator("node_id")
    @classmethod
    def valid_node(cls, value):
        if value:
            from opcua import ua
            try:
                ua.NodeId.from_string(value)
            except Exception as exc:
                raise ValueError("NodeId格式无效，例如 ns=2;s=Pressure") from exc
        return value


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["simulation", "opcua"]
    endpoint: str = Field(min_length=10, max_length=500)
    security_string: str | None = Field(default=None, max_length=2500)
    username: str | None = Field(default=None, max_length=200)
    password_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    poll_interval: float | None = Field(default=None, ge=0.1, le=60, allow_inf_nan=False)
    batch_size: int | None = Field(default=None, ge=1, le=1000)
    heartbeat_seconds: float | None = Field(default=None, gt=0, le=604800, allow_inf_nan=False)
    retention_days: float | None = Field(default=None, gt=0, le=7, allow_inf_nan=False)

    @field_validator("endpoint")
    @classmethod
    def valid_url(cls, value):
        from urllib.parse import urlparse
        p = urlparse(value)
        if p.scheme != "opc.tcp" or not p.hostname or not p.port or p.username or p.password:
            raise ValueError("请输入不带凭据的 opc.tcp://主机:端口")
        return value


class AIQuery(BaseModel):
    question: str = Field(min_length=1, max_length=1000)
    device: str | None = None
    variable: str | None = None
    start: str | None = None
    end: str | None = None
    connection_id: str | None = None
    tag_revision: int | None = Field(default=None, ge=1)


def threshold_crossed(value, baseline, threshold):
    """Compare received numeric values without introducing subtraction rounding error."""
    return abs(Decimal(str(value)) - Decimal(str(baseline))) >= Decimal(str(threshold))


def typed_value(value, kind):
    if kind == "BOOL":
        if not isinstance(value, bool):
            raise ValueError("需要BOOL")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("需要有限数值")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("需要有限数值")
    if kind in ("WORD", "DWORD"):
        maximum = 65535 if kind == "WORD" else 4294967295
        if int(value) != value or not 0 <= value <= maximum:
            raise ValueError(f"{kind}范围为0..{maximum}")
        return int(value)
    if abs(value) > 3.4028235e38:
        raise ValueError("超出Float32范围")
    return float(value)
