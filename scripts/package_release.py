"""Build a clean Windows installation ZIP; never copy field data or credentials."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIRECTORY = "Industrial_AI_Gateway"
TOP_LEVEL = {"launch.py", "seed_data.py", "README.md", "TEST_REPORT.md",
             "requirements.txt", "requirements-lock.txt", "requirements-windows.txt", "requirements-windows-lock.txt"}
OPTIONAL_PUBLIC = {"desktop.py", "LICENSE", "THIRD_PARTY_NOTICES.md", "requirements-build-lock.txt"}


def clean_config() -> dict:
    # Construct a new configuration, never redact and reuse the source installation.
    return {"mode": "opcua", "endpoint": "opc.tcp://127.0.0.1:4840",
            "poll_interval": 1.0, "batch_size": 100, "heartbeat_seconds": 1800,
            "retention_days": 7, "queue_capacity": 120,
            "simulation_write_enabled": False, "disk_min_free_mb": 100,
            "backup_enabled": True, "security_string": "", "username": "",
            "password_env": "PLC_OPCUA_PASSWORD", "ai_provider": "local_rules"}


def release_files(root: Path):
    """Allow only source files and public documentation, rejecting linked files."""
    selected = []
    for path in root.iterdir():
        if path.name in TOP_LEVEL | OPTIONAL_PUBLIC or path.suffix in (".bat", ".ps1"):
            selected.append(path)
    rules = {"backend": {".py"}, "scripts": {".py", ".bat", ".ps1"},
             "tests": {".py", ".cjs"}, "frontend": {".html", ".js", ".css", ".txt"},
             "docs": {".md"}, "packaging": {".spec", ".json", ".txt", ".rst", ".md", ".LESSER", ".GPL", ".LIB", ".terms", ".python", ".gz"},
             ".github": {".yml", ".yaml"}}
    for directory, extensions in rules.items():
        for path in (root / directory).rglob("*"):
            if path.suffix in extensions and "__pycache__" not in path.parts:
                selected.append(path)
    for path in sorted(set(selected)):
        relative = path.relative_to(root)
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root.parent):
            raise ValueError(f"Refusing to include a symbolic link: {relative}")
        if path.is_file():
            yield relative.as_posix(), path.read_bytes()
    example = root / "config/config.example.json"
    if example.is_file() and not example.is_symlink():
        yield "config/config.example.json", example.read_bytes()


def build_release(output: Path, root: Path = ROOT) -> dict:
    root = root.resolve()
    output = output.resolve()
    if output.suffix.lower() != ".zip":
        raise ValueError("Release output must have a .zip extension.")
    required = [root / name for name in TOP_LEVEL] + [root / "docs/WINDOWS_DEPLOYMENT.md"]
    missing = [str(path.relative_to(root)) for path in required if not path.is_file()]
    if missing:
        raise ValueError("Missing release files: " + ", ".join(missing))
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from backend.tag_manager import export_excel
    entries = dict(release_files(root))
    entries["config/config.json"] = (json.dumps(clean_config(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    entries["templates/PLC点位导入模板.xlsx"] = export_excel([])
    built_at = datetime.now(timezone(timedelta(hours=8)))
    entries["PACKAGE_INFO.txt"] = ("Industrial AI Gateway — Windows 基础功能发行包\n\n"
        f"打包时间：{built_at.strftime('%Y-%m-%d %H:%M:%S')} Asia/Shanghai\n"
        "包含：代码、静态界面、锁定依赖清单、部署脚本、新现场配置、空Excel点位模板和说明。\n"
        "不包含：Python解释器、.venv、现场数据库、历史、口令、证书、私钥、日志或备份。\n"
        "运行要求：Windows x64 和 Python 3.12；第一次依赖安装需联网或事先准备匹配的 wheels。\n"
        "前台入口：install.bat → check.bat → start.bat；停止入口：stop.bat。\n"
        "后台入口：管理员终端 service.bat install/start/stop/status/remove。\n"
        "详细说明：docs/WINDOWS_DEPLOYMENT.md。Windows 和真实 PLC 尚需现场验收。\n").encode("utf-8-sig")
    entries["部署说明.txt"] = ("Industrial AI Gateway Windows 基础版\n\n"
        "1. 安装 Windows 64 位 Python 3.12（包含 Python 启动器）。\n"
        "2. 解压到本机可写目录，运行 install.bat 安装依赖。\n"
        "3. 运行 check.bat 检查，再运行 start.bat 打开界面。\n"
        "4. 读取 data/operator_pin.txt 的管理口令，在连接设置填写实际 OPC UA 地址。\n"
        "5. 填写 templates/PLC点位导入模板.xlsx，导入并确认，然后检查实时数据质量。\n\n"
        "本包为全新现场安装，未包含演示点位、数据库、口令、证书或现场配置。\n"
        "默认 OPC UA 地址是占位值，需要现场修改；未配置点位前就绪检查会提示未就绪。\n"
        "首次依赖安装需要联网。离线部署先在 Windows 准备依赖，详见 docs/WINDOWS_DEPLOYMENT.md。\n"
        "服务安装与控制入口为 service.bat，需要管理员终端。\n"
        "当前服务控制脚本和批处理尚需 Windows 现场验收，真实 PLC 通信需现场验证。\n").encode("utf-8-sig")
    manifest = {"format": 1, "created_at": datetime.now(timezone.utc).isoformat(),
                "target": "Windows x64 / Python 3.12", "contains_field_data": False,
                "files": {name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
                          for name, content in sorted(entries.items())}}
    entries["RELEASE_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(PACKAGE_DIRECTORY + "/data/", "")
            for name, content in sorted(entries.items()):
                archive.writestr(PACKAGE_DIRECTORY + "/" + name, content)
        with zipfile.ZipFile(temporary) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError("Release CRC validation failed: " + bad)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    return {"output": str(output), "checksum": str(checksum), "sha256": digest,
            "files": len(entries), "bytes": output.stat().st_size}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/Industrial_AI_Gateway_Windows.zip")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build_release(args.output), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, ImportError) as exc:
        print(f"Release packaging failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
