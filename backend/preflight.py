"""Explicit read-only diagnostics on a separate, short-lived UA session."""
import time
from opcua import ua
from .configuration import validate_credentials
from .opcua_client import OPCUADriver

TYPE_IDS = {"BOOL": 1, "WORD": 5, "DWORD": 7, "FLOAT": 10}


def connection_test(config, root):
    validate_credentials(config, root)
    if config["mode"] == "simulation":
        return {"success": True, "message": "模拟模式，无需外部连接"}
    driver = OPCUADriver(dict(config))
    try:
        driver.connect()
        return {"success": True, "message": "OPC UA连接成功；运行连接未改变"}
    finally:
        driver.disconnect()


def validate_nodes(tags, config, root):
    validate_credentials(config, root)
    if config["mode"] == "simulation":
        return [{"id": t.id, "name": t.name, "quality": "Good", "message": "模拟点表有效"} for t in tags]
    driver = OPCUADriver(dict(config))
    result = []
    deadline = time.monotonic() + 20
    try:
        driver.connect()
        size = config.get("batch_size", 100)
        for offset in range(0, len(tags), size):
            batch = tags[offset:offset+size]
            mapped = [t for t in batch if t.node_id]
            result.extend({"id": t.id, "name": t.name, "quality": "BadNodeIdMissing", "message": "缺少NodeId"}
                          for t in batch if not t.node_id)
            if not mapped:
                continue
            if time.monotonic() >= deadline:
                result.extend({"id": t.id, "name": t.name, "quality": "NotChecked", "message": "预检超时，请分批重试"}
                              for t in mapped)
                continue
            nodes = [driver.client.get_node(t.node_id).nodeid for t in mapped]
            attrs = {}
            for attr in (ua.AttributeIds.DataType, ua.AttributeIds.AccessLevel, ua.AttributeIds.UserAccessLevel):
                attrs[attr] = driver.client.uaclient.get_attributes(nodes, attr)
                if len(attrs[attr]) != len(mapped):
                    raise ValueError("OPC UA返回的预检属性数量不匹配")
            for index, tag in enumerate(mapped):
                values = [attrs[attr][index] for attr in attrs]
                bad = next((v for v in values if not v.StatusCode.is_good()), None)
                quality, message = "Good", "节点存在，类型匹配，当前账号可读取"
                if bad is not None:
                    quality, message = bad.StatusCode.name, "节点属性不可读"
                else:
                    declared = attrs[ua.AttributeIds.DataType][index].Value.Value
                    allowed = all(int(attrs[attr][index].Value.Value) & 1 for attr in
                                  (ua.AttributeIds.AccessLevel, ua.AttributeIds.UserAccessLevel))
                    if declared.NamespaceIndex != 0 or declared.Identifier != TYPE_IDS[tag.type]:
                        quality, message = "BadTypeMismatch", "实际声明类型与点表不一致"
                    elif not allowed:
                        quality, message = "BadUserAccessDenied", "当前账号没有读取权限"
                result.append({"id": tag.id, "name": tag.name, "quality": quality, "message": message})
        return sorted(result, key=lambda row: row["id"])
    finally:
        driver.disconnect()
