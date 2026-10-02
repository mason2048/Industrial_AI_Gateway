# Build on Windows x64 with Python 3.12 using scripts/build_windows.py.
# Official references: https://pyinstaller.org/en/stable/spec-files.html
# https://pyinstaller.org/en/stable/hooks.html
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPEC).resolve().parents[1]
ASSETS = Path(os.environ.get("INDUSTRIAL_GATEWAY_BUILD_ASSETS", ROOT / "build/windows-assets"))
required_assets = ("config/config.default.json", "LICENSE", "THIRD_PARTY_NOTICES.md", "licenses", "third_party_sources", "LICENSE_MANIFEST.json")
for name in required_assets:
    if not (ASSETS / name).exists():
        raise RuntimeError("Missing clean build resource: " + name + "; run scripts/build_windows.py")

# Deliberately enumerate public assets; never include the source config or data directories.
datas = [(str(ASSETS / "frontend"), "frontend"),
         (str(ASSETS / "config/config.default.json"), "config"),
         (str(ASSETS / "THIRD_PARTY_NOTICES.md"), "."),
         (str(ASSETS / "LICENSE"), "."),
         (str(ASSETS / "LICENSE_MANIFEST.json"), "."),
         (str(ASSETS / "licenses"), "licenses"),
         (str(ASSETS / "third_party_sources"), "third_party_sources")]
binaries = []
hiddenimports = ["_cffi_backend", "anyio._backends._asyncio", "python_multipart", "multipart"]
# OPC UA, timezone and CA data use dynamic imports/resources. Default package hooks
# retain cryptography/pydantic/lxml native extensions; collect_all fills package data.
for package in ("opcua", "tzdata", "certifi"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_imports)
hiddenimports.extend(collect_submodules("uvicorn"))
for package in ("fastapi", "uvicorn", "opcua", "openpyxl", "pydantic", "pydantic_core", "python-multipart", "anyio"):
    datas.extend(copy_metadata(package))

a = Analysis(
    [str(ROOT / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=sorted(set(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "pandas", "numpy", "Pygments", "IPython"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="IndustrialAIGateway",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    uac_admin=False,
    version=str(ASSETS / "version_info.txt"),
)
