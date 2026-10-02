"""Explicit demo initialization; existing databases are never opened or changed."""
import math
import json
import time
from pathlib import Path
from backend.models import Tag
from backend.tag_manager import export_excel, import_excel
from backend.database import Database

ROOT = Path(__file__).resolve().parent


def demo_tags():
    specs = [
        ("DB1.DBD20","真空压力","FLOAT","Pa",1e-5,True,"真空泵入口压力"),
        ("DB1.DBD30","泵体温度","FLOAT","°C",.5,True,"泵体温度"),
        ("DB1.DBD40","电机电流","FLOAT","A",.2,True,"主电机电流"),
        ("M100.0","启动状态","BOOL","-",0,False,"设备运行状态"),
        ("DB1.DBW10","电机转速","WORD","rpm",10,True,"电机转速"),
        ("DB1.DBD50","累计运行秒","DWORD","s",60,True,"累计运行计数"),
    ]
    return [Tag(id=i,address=a,name=n,type=k,unit=u,threshold=t,save=s,ai_description=d,node_id=f"ns=2;s=Demo.Tag{i}") for i,(a,n,k,u,t,s,d) in enumerate(specs,1)]


def seed(root=ROOT):
    root = Path(root).resolve()
    config_path = root / "config/config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if config.get("mode") != "simulation":
            raise ValueError("Demo initialization is only allowed for a simulation installation.")
    else:
        if (root / "data/history.db").exists():
            raise ValueError("An existing database has no configuration; configure it explicitly before demo initialization.")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"mode":"simulation", "endpoint":"opc.tcp://127.0.0.1:4840",
            "poll_interval":1.0, "batch_size":100, "heartbeat_seconds":1800, "retention_days":7,
            "simulation_write_enabled":False}, indent=2), encoding="utf-8")
    data = root/"data"
    data.mkdir(parents=True,exist_ok=True)
    tags = demo_tags()
    if not (data/"tags.xlsx").exists(): (data/"tags.xlsx").write_bytes(export_excel(tags))
    if not (data/"tags_1000.xlsx").exists():
        bulk = tags + [Tag(id=i,address=f"SIM.{i}",name=f"测试点{i:04}",type=["BOOL","WORD","DWORD","FLOAT"][i%4],threshold=1,node_id=f"ns=2;s=Demo.Tag{i}") for i in range(7,1001)]
        (data/"tags_1000.xlsx").write_bytes(export_excel(bulk))
    if (data / "history.db").exists():
        print("Missing templates created; existing database left unchanged.")
        return
    from backend.configuration import ConfigStore
    connection_id = ConfigStore(root).snapshot()["connection_id"]
    db = Database(data/"history.db")
    if not db.tags(): db.replace_tags(import_excel((data/"tags.xlsx").read_bytes()))
    with db.connect() as conn:
        empty = conn.execute("SELECT COUNT(*) FROM history_data").fetchone()[0] == 0
    # Demonstration history only for the exact bundled sample definitions.
    actual = db.tags()
    if empty and actual == [t.model_dump() for t in tags]:
        now, rows = time.time(), []
        for minute in range(1440,0,-1):
            stamp = now-minute*60
            for tag in tags:
                if not tag.save: continue
                phase = stamp/900+tag.id
                v = {1:5e-5+2e-5*math.sin(phase),2:35.6+2.3*math.sin(phase),3:12.5+.8*math.sin(phase),5:int(1450+30*math.sin(phase)),6:int(stamp)%4294967296}[tag.id]
                rows.append((tag.id,stamp,v,"Good","simulation",tag.device,tag.name,tag.unit,tag.type,
                             connection_id,tag.revision,None,None,None))
        db.insert_history(rows)
    print("Templates and simulation history ready (existing data preserved).")


if __name__ == "__main__":
    import argparse
    from scripts.runtime import ProcessLock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-init", action="store_true", required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    with ProcessLock(args.root, reentrant=False, purpose="demo-init"):
        seed(args.root)
