"""Build a clean, self-contained Windows x64 EXE and portable distribution.

Run on Windows/Python 3.12. ``--prepare-only`` validates public resources on
other platforms without attempting to cross-compile or copying field data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_FILES = (
    "index.html", "app.js", "style.css", "history.js", "api-client.js",
    "realtime.js", "vendor/vue.global.prod.js", "vendor/VUE-LICENSE.txt",
)
CONFIG_KEYS = {
    "mode", "endpoint", "poll_interval", "batch_size", "heartbeat_seconds",
    "retention_days", "queue_capacity", "simulation_write_enabled",
    "disk_min_free_mb", "backup_enabled", "security_string", "username",
    "password_env", "ai_provider",
}
PACKAGE_DIRECTORY = "Industrial_AI_Gateway"


def version_tuple(version: str) -> tuple[int, int, int, int]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Version must be a numeric major.minor.patch value.")
    components = tuple(map(int, version.split(".")))
    if any(value > 65535 for value in components):
        raise ValueError("Each Windows version component must be at most 65535.")
    return (*components, 0)


def clean_configuration(root: Path) -> dict:
    """Only the public example is eligible; the installed config is never read."""
    value = json.loads((root / "config/config.example.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != CONFIG_KEYS:
        raise ValueError("Public example configuration has unexpected or missing fields.")
    expected = {"mode": "simulation", "endpoint": "opc.tcp://127.0.0.1:4840",
                "simulation_write_enabled": False, "username": "", "security_string": "",
                "password_env": "PLC_OPCUA_PASSWORD", "ai_provider": "local_rules"}
    if any(value[name] != required for name, required in expected.items()):
        raise ValueError("Public example must be local, simulated, read-only and contain no credentials.")
    from backend.configuration import validate_runtime
    validate_runtime(value)
    return value


def _public_file(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.is_file():
        raise ValueError("Missing public build resource: " + relative)
    current = path
    while current != root:
        if current.is_symlink():
            raise ValueError("Refusing linked build resource: " + relative)
        current = current.parent
    return path


def windows_version_info(version: str) -> str:
    numbers = version_tuple(version)
    return f"""# UTF-8 Windows executable version resource
VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'mason2048'),
    StringStruct('FileDescription', 'Industrial AI Gateway local application'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', 'IndustrialAIGateway'),
    StringStruct('LegalCopyright', 'Copyright 2026 mason2048; third-party licenses apply'),
    StringStruct('OriginalFilename', 'IndustrialAIGateway.exe'),
    StringStruct('ProductName', 'Industrial AI Gateway'),
    StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""


def prepare_assets(root: Path, assets: Path, version: str, *, license_collector=None) -> dict:
    version_tuple(version)
    root = root.resolve()
    if assets.is_symlink():
        raise ValueError("Build assets cannot replace a symbolic link.")
    assets = assets.resolve()
    if assets == root or root.is_relative_to(assets):
        raise ValueError("Build assets cannot replace the project or an ancestor.")
    if assets.is_relative_to(root) and not assets.is_relative_to(root / "build"):
        raise ValueError("Generated assets inside the project must remain under build/.")
    # Validate before creating/replacing an output directory.
    configuration = clean_configuration(root)
    frontend = [(name, _public_file(root, "frontend/" + name)) for name in FRONTEND_FILES]
    _public_file(root, "LICENSE")
    if license_collector is None:
        from scripts.collect_licenses import collect
        license_collector = collect
    assets.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gateway-assets-", dir=assets.parent) as temporary:
        staged = Path(temporary)
        for name, source in frontend:
            target = staged / "frontend" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        (staged / "config").mkdir()
        (staged / "config/config.default.json").write_text(
            json.dumps(configuration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        license_collector(root, staged)
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "LICENSE_MANIFEST.json",
                     "third_party_sources/opcua-0.98.13.tar.gz"):
            _public_file(staged, name)
        if not (staged / "licenses").is_dir():
            raise ValueError("License collector did not generate full license texts.")
        (staged / "version_info.txt").write_text(windows_version_info(version), encoding="utf-8")
        selected = {}
        for path in sorted(staged.rglob("*")):
            if path.is_symlink():
                raise ValueError("Refusing linked generated resource.")
            if path.is_file():
                content = path.read_bytes()
                selected[path.relative_to(staged).as_posix()] = {
                    "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        manifest = {"version": version, "contains_field_data": False, "files": selected}
        (staged / "BUILD_ASSET_MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if assets.exists():
            if assets.is_symlink():
                raise ValueError("Build assets cannot replace a symbolic link.")
            shutil.rmtree(assets)
        shutil.copytree(staged, assets)
    return manifest


def inspect_windows_exe(path: Path) -> dict:
    """Reject an accidental native-host build or a non-x64 artifact."""
    with path.open("rb") as handle:
        dos = handle.read(64)
        if len(dos) != 64 or dos[:2] != b"MZ":
            raise ValueError("Output is not a Windows executable.")
        offset = struct.unpack_from("<I", dos, 60)[0]
        if offset > path.stat().st_size - 6:
            raise ValueError("Invalid Windows executable header.")
        handle.seek(offset)
        header = handle.read(6)
        if header[:4] != b"PE\0\0" or struct.unpack_from("<H", header, 4)[0] != 0x8664:
            raise ValueError("Output must be a Windows x64 PE executable.")
    return {"format": "PE", "architecture": "x64", "bytes": path.stat().st_size}


def quick_start(version: str) -> str:
    return f"""Industrial AI Gateway {version} — Windows x64 本地程序

1. 将本文件夹解压到本机可写目录，双击 IndustrialAIGateway.exe。
   无需安装 Python、Node.js 或数据库；程序会自动打开本机浏览器界面。
2. 第一次运行会创建 gateway-data，显示 6 个明确标注的模拟点位。
   模拟数据用于体验界面，连接真实设备前请切换“PLC连接”中的 OPC UA 模式。
3. 管理口令在 gateway-data\\data\\operator_pin.txt。
   点击“打开数据目录.cmd”可定位当前数据目录；不可写安装目录会使用
   %LOCALAPPDATA%\\IndustrialAIGateway，配置和历史都保存在该目录。
4. 在“PLC连接”修改服务器地址、采集周期和批量读取数量。
   在“点位管理”下载 Excel 模板，填写 NodeId、类型、注释和历史策略再导入。
5. 结束运行时点击界面“退出软件”（需管理口令），或双击“停止软件.cmd”。
   仅关闭浏览器页面不会停止采集。重新双击 EXE 会打开已有实例。

当前版本支持最多 1000 点、BOOL/WORD/DWORD/FLOAT、按点位保存间隔与变化阈值、
SQLite 最近 7 天历史及按需数据查询。实际 PLC 连接只读；真实 PLC 写入未开放。
当前查询使用本地规则，尚未接入外部大模型，不会在后台自动发送工业数据。
真空浮点显示支持 5 位小数；实际采集精度以 PLC 与 OPC UA 数据类型为准。

发布包保留第三方许可证与 opcua LGPL 原始源码，见 THIRD_PARTY_NOTICES.md、
licenses 和 third_party_sources。源代码与重建说明：
https://github.com/mason2048/Industrial_AI_Gateway

升级前请正常退出并备份 gateway-data；替换 EXE 即可保留本机配置和历史。
可选命令：IndustrialAIGateway.exe --check / --stop / --open-data
指定安装目录：IndustrialAIGateway.exe --root "D:\\GatewayData"
Windows 自动测试验证了独立 EXE 的启动、采集、历史和退出；真实 PLC 需现场配置验收。
"""


def create_portable(root: Path, assets: Path, executable: Path, output: Path, version: str) -> dict:
    version_tuple(version)
    inspect_windows_exe(executable)
    output.mkdir(parents=True, exist_ok=True)
    archive_path = output / f"Industrial_AI_Gateway-v{version}-windows-x64.zip"
    entries = {"IndustrialAIGateway.exe": executable.read_bytes(),
               "使用说明.txt": quick_start(version).encode("utf-8-sig"),
               "停止软件.cmd": ("@echo off\r\n\"%~dp0IndustrialAIGateway.exe\" --stop\r\n"
                                 "if errorlevel 1 (\r\n  echo Stop failed. Open the local logs for details.\r\n"
                                 "  pause\r\n  exit /b 1\r\n)\r\n").encode("ascii"),
               "打开数据目录.cmd": b'@echo off\r\n"%~dp0IndustrialAIGateway.exe" --open-data\r\n'}
    # Public license resources are listed explicitly. No installation paths are eligible.
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "LICENSE_MANIFEST.json"):
        entries[name] = _public_file(assets, name).read_bytes()
    for directory in ("licenses", "third_party_sources"):
        for path in sorted((assets / directory).rglob("*")):
            if path.is_file():
                name = path.relative_to(assets).as_posix()
                entries[name] = _public_file(assets, name).read_bytes()
    if (root / "docs/EXE_BUILD.md").is_file():
        entries["EXE_BUILD.md"] = _public_file(root, "docs/EXE_BUILD.md").read_bytes()
    manifest = {"version": version, "target": "Windows x64", "requires_python_install": False,
                "contains_field_data": False,
                "files": {name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                          for name, value in sorted(entries.items())}}
    entries["RELEASE_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = archive_path.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, value in sorted(entries.items()):
                archive.writestr(PACKAGE_DIRECTORY + "/" + name, value)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("Portable distribution CRC validation failed.")
        temporary.replace(archive_path)
    finally:
        temporary.unlink(missing_ok=True)
    sums = []
    for path in (executable, archive_path):
        sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    (output / "SHA256SUMS.txt").write_text("".join(sums), encoding="ascii")
    info = {"version": version, "exe": executable.name, "portable_zip": archive_path.name,
            "target": "Windows x64", "python": "3.12", "contains_field_data": False,
            "requires_python_install": False, "checksum_file": "SHA256SUMS.txt"}
    (output / "BUILD_INFO.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return info


def build(root: Path, output: Path, version: str, *, prepare_only=False) -> dict:
    root, output = root.resolve(), output.resolve()
    if not prepare_only and (sys.platform != "win32" or sys.version_info[:2] != (3, 12)
                             or struct.calcsize("P") != 8):
        raise RuntimeError("Build the Windows x64 executable on Windows x64 with Python 3.12.")
    assets = root / "build/windows-assets"
    manifest = prepare_assets(root, assets, version)
    if prepare_only:
        return {"prepared": True, "version": version, "files": len(manifest["files"]), "assets": str(assets)}
    output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, INDUSTRIAL_GATEWAY_BUILD_ASSETS=str(assets))
    subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                    "--distpath", str(output), "--workpath", str(root / "build/pyinstaller-windows"),
                    str(root / "packaging/IndustrialAIGateway.spec")],
                   cwd=root, env=environment, check=True)
    executable = output / "IndustrialAIGateway.exe"
    inspect_windows_exe(executable)
    return create_portable(root, assets, executable, output, version)


def main(argv=None):
    from backend.version import APP_VERSION
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/windows")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(ROOT, args.output, args.version, prepare_only=args.prepare_only), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.CalledProcessError) as exc:
        print(f"Windows build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
