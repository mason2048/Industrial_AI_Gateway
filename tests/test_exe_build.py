"""Public builds must exclude installation data and produce verifiable x64 files."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import zipfile

import pytest

from scripts.build_windows import (FRONTEND_FILES, ROOT, clean_configuration,
                                   create_portable, inspect_windows_exe, prepare_assets,
                                   version_tuple)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in FRONTEND_FILES:
        target = root / "frontend" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "frontend" / name, target)
    (root / "config").mkdir()
    shutil.copy2(ROOT / "config/config.example.json", root / "config/config.example.json")
    shutil.copy2(ROOT / "LICENSE", root / "LICENSE")
    (root / "config/config.json").write_text('{"username":"field-private-token"}')
    (root / "data").mkdir()
    (root / "data/history.db").write_bytes(b"field-private-token")
    (root / "frontend/private.txt").write_text("field-private-token")
    return root


def fake_licenses(root, out):
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "LICENSE_MANIFEST.json",
                 "licenses/example/LICENSE", "third_party_sources/opcua-0.98.13.tar.gz",
                 "third_party_sources/REBUILD.md"):
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Public license material for isolated packaging tests.\n")
    return {}


def fake_pe(path, machine=0x8664):
    image = bytearray(512)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 60, 128)
    image[128:132] = b"PE\0\0"
    struct.pack_into("<H", image, 132, machine)
    path.write_bytes(image)
    return path


def test_prepare_uses_only_public_example_and_listed_interface_files(source, tmp_path):
    assets = tmp_path / "assets"
    manifest = prepare_assets(source, assets, "1.2.0", license_collector=fake_licenses)
    config = json.loads((assets / "config/config.default.json").read_text())
    assert config["mode"] == "simulation" and config["username"] == ""
    assert config["endpoint"] == "opc.tcp://127.0.0.1:4840"
    assert not config["simulation_write_enabled"]
    assert not manifest["contains_field_data"]
    assert not (assets / "data").exists()
    assert not (assets / "frontend/private.txt").exists()
    assert not any(b"field-private-token" in path.read_bytes() for path in assets.rglob("*") if path.is_file())
    for name, expected in manifest["files"].items():
        content = (assets / name).read_bytes()
        assert expected == {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    # Rebuild replaces previous generated assets, without reading installation data.
    (assets / "obsolete.txt").write_text("obsolete")
    prepare_assets(source, assets, "1.2.0", license_collector=fake_licenses)
    assert not (assets / "obsolete.txt").exists()
    assert (source / "data/history.db").read_bytes() == b"field-private-token"


@pytest.mark.parametrize("change", [{"username": "private"}, {"security_string": "private"},
                                   {"endpoint": "opc.tcp://192.168.7.12:4840"},
                                   {"mode": "opcua"}, {"password": "private"},
                                   {"simulation_write_enabled": True}])
def test_unsafe_example_is_rejected_before_build_outputs_are_written(source, tmp_path, change):
    path = source / "config/config.example.json"
    value = json.loads(path.read_text())
    value.update(change)
    path.write_text(json.dumps(value))
    assets = tmp_path / "assets"
    with pytest.raises(ValueError):
        prepare_assets(source, assets, "1.2.0", license_collector=fake_licenses)
    assert not assets.exists()


def test_linked_frontend_cannot_bypass_public_asset_boundaries(source, tmp_path):
    target = source / "frontend/index.html"
    target.unlink()
    try:
        target.symlink_to(source / "config/config.json")
    except OSError:
        pytest.skip("This Windows account cannot create symlinks.")
    with pytest.raises(ValueError, match="linked"):
        prepare_assets(source, tmp_path / "assets", "1.2.0", license_collector=fake_licenses)


def test_build_cache_cannot_replace_installation_directory(source):
    with pytest.raises(ValueError, match="build/"):
        prepare_assets(source, source / "data", "1.2.0", license_collector=fake_licenses)
    assert (source / "data/history.db").read_bytes() == b"field-private-token"


def test_portable_contains_exe_legal_resources_and_scripts_without_runtime(source, tmp_path):
    assets, output = tmp_path / "assets", tmp_path / "distribution"
    prepare_assets(source, assets, "1.2.0", license_collector=fake_licenses)
    output.mkdir()
    executable = fake_pe(output / "IndustrialAIGateway.exe")
    # Unexpected generated files cannot leak into a published portable distribution.
    (assets / "data").mkdir()
    (assets / "data/history.db").write_bytes(b"field-private-token")
    info = create_portable(source, assets, executable, output, "1.2.0")
    assert not info["requires_python_install"] and not info["contains_field_data"]
    assert info["portable_zip"] == "Industrial_AI_Gateway-v1.2.0-windows-x64.zip"
    with zipfile.ZipFile(output / info["portable_zip"]) as archive:
        prefix = "Industrial_AI_Gateway/"
        names = [name.removeprefix(prefix) for name in archive.namelist()]
        assert "IndustrialAIGateway.exe" in names
        assert "停止软件.cmd" in names and "打开数据目录.cmd" in names
        assert "third_party_sources/opcua-0.98.13.tar.gz" in names
        assert not any(name.startswith(("data/", "config/", "frontend/")) for name in names)
        assert not any(b"field-private-token" in archive.read(name) for name in archive.namelist())
        assert b"--stop" in archive.read(prefix + "停止软件.cmd")
        manifest = json.loads(archive.read(prefix + "RELEASE_MANIFEST.json"))
        for name, expected in manifest["files"].items():
            content = archive.read(prefix + name)
            assert expected == {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    for line in (output / "SHA256SUMS.txt").read_text().splitlines():
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((output / name).read_bytes()).hexdigest()


@pytest.mark.parametrize("machine", [0x014C, 0xAA64])
def test_wrong_target_binary_is_rejected(tmp_path, machine):
    with pytest.raises(ValueError, match="x64"):
        inspect_windows_exe(fake_pe(tmp_path / "wrong.exe", machine))


def test_non_windows_binary_and_malformed_version_are_rejected(tmp_path):
    path = tmp_path / "native.exe"
    path.write_bytes(b"\x7fELF" + b"\0" * 200)
    with pytest.raises(ValueError, match="Windows executable"):
        inspect_windows_exe(path)
    for version in ("1.2.0; private", "1.2", "65536.2.0", "v1.2.0"):
        with pytest.raises(ValueError):
            version_tuple(version)
