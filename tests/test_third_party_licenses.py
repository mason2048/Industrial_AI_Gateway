import hashlib
import json
from pathlib import Path
import shutil
import tarfile

import pytest

from scripts import collect_licenses


ROOT = Path(__file__).resolve().parents[1]


def test_frozen_licenses_are_complete_and_verifiable(tmp_path):
    stage = tmp_path / "assets"
    stage.mkdir()
    (stage / "frontend-placeholder.txt").write_text("preserved")
    manifest = collect_licenses.collect(ROOT, stage)
    assert (stage / "frontend-placeholder.txt").read_text() == "preserved"
    assert json.loads((stage / "LICENSE_MANIFEST.json").read_text()) == manifest
    packages = {item["name"]: item for item in manifest["packages"]}
    assert set(collect_licenses.RUNTIME_ROOTS) <= packages.keys()
    assert not {"pandas", "numpy", "pytest", "pip"} & packages.keys()
    try:
        collect_licenses.metadata.distribution("pyinstaller")
    except collect_licenses.metadata.PackageNotFoundError:
        pass  # Source-only CI does not install the EXE build tool.
    else:
        assert "pyinstaller" in packages
    for package in manifest["packages"]:
        assert package["files"], package["name"]
        for file in package["files"]:
            path = stage / file["path"]
            assert path.stat().st_size > 20
            assert hashlib.sha256(path.read_bytes()).hexdigest() == file["sha256"]
            assert not Path(file["path"]).is_absolute()
    opcua = stage / "licenses/packages/opcua"
    assert "Version 3, 29 June 2007" in (opcua / "COPYING.LESSER").read_text()
    assert "END OF TERMS AND CONDITIONS" in (opcua / "COPYING.GPL").read_text()
    assert "Version 2.1, February 1999" in (stage / "licenses/runtime/lxml-native/COPYING.LIB").read_text()
    assert (stage / "third_party_sources/REBUILD.md").is_file()
    archive = stage / "third_party_sources/opcua-0.98.13.tar.gz"
    with tarfile.open(archive) as contents:
        assert any(member.name.endswith("opcua/client/client.py") for member in contents.getmembers())
    notices = (stage / "THIRD_PARTY_NOTICES.md").read_text()
    assert "LGPL v3" in notices
    if "pyinstaller" in packages:
        assert "pyinstaller" in notices
    assert "Mozilla Public License Version 2.0" in (stage / "licenses/runtime/certifi/MPL-2.0.txt").read_text()
    assert "site-packages" not in (stage / "LICENSE_MANIFEST.json").read_text()


def test_collector_refuses_changed_checked_in_license(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(ROOT / "packaging", source / "packaging")
    with (source / "packaging/licenses/opcua/0.98.13/COPYING.GPL").open("a") as handle:
        handle.write("\nchanged\n")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        collect_licenses.collect(source, tmp_path / "out")


def test_collector_refuses_changed_original_source_archive(tmp_path):
    source = tmp_path / "source"
    shutil.copytree(ROOT / "packaging", source / "packaging")
    with (source / "packaging/third_party_sources/opcua-0.98.13.tar.gz").open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(RuntimeError, match="source checksum mismatch"):
        collect_licenses.collect(source, tmp_path / "out")


def test_collector_does_not_write_into_original_source():
    with pytest.raises(ValueError, match="separate build"):
        collect_licenses.collect(ROOT, ROOT)


def test_dependency_markers_use_target_platform():
    packages = {collect_licenses.normalize_name(dist.metadata["Name"])
                for dist in collect_licenses.runtime_distributions()}
    assert "opcua" in packages
    assert "lxml" in packages
    assert "cryptography" in packages
    # The OPC UA legacy Python-2-only requirements must not leak into Python 3.
    assert not {"enum34", "futures", "trollius"} & packages
