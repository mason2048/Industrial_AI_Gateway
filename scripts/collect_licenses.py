"""Prepare offline, auditable third-party notices for a frozen distribution.

Only named installed distributions and checked-in legal/source resources are read;
the collector never traverses the application's configuration or history folders.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import sys

from packaging.requirements import Requirement

RUNTIME_ROOTS = (
    "fastapi", "uvicorn", "opcua", "cryptography", "openpyxl",
    "python-multipart", "httpx", "tzdata",
)
LICENSE_NAMES = re.compile(r"^(?:licen[cs]e|copying|notice|authors)(?:[._-].*)?$", re.I)
KNOWN_LICENSES = {
    "colorama": "BSD-3-Clause", "opcua": "LGPL-3.0-or-later",
    "python-dateutil": "Apache-2.0 OR BSD-3-Clause", "pyinstaller":
    "GPL-2.0-or-later WITH PyInstaller-exception AND Apache-2.0",
}


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_distributions(roots=RUNTIME_ROOTS) -> list:
    """Evaluate dependency markers for the current target interpreter/platform."""
    pending = list(roots)
    found = {}
    while pending:
        name = pending.pop()
        key = normalize_name(name)
        if key in found:
            continue
        dist = metadata.distribution(name)
        found[key] = dist
        for dependency in dist.requires or ():
            requirement = Requirement(dependency)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            pending.append(requirement.name)
    return [found[key] for key in sorted(found)]


def _copy(source: Path, target: Path) -> dict:
    if not source.is_file() or source.stat().st_size < 20:
        raise RuntimeError(f"Missing or empty legal resource: {source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {"path": target.as_posix(), "sha256": sha256(target)}


def _license_label(dist) -> str:
    name = normalize_name(dist.metadata["Name"])
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression
    if name in KNOWN_LICENSES:
        return KNOWN_LICENSES[name]
    text = dist.metadata.get("License", "").strip()
    return text.splitlines()[0][:160] if text else "See full license text"


def _source_url(dist) -> str:
    for entry in dist.metadata.get_all("Project-URL", ()):
        label, _, url = entry.partition(",")
        if label.lower().strip() in {"source", "source code", "repository", "homepage"}:
            return url.strip()
    return dist.metadata.get("Home-page") or (
        "https://pypi.org/project/" + dist.metadata["Name"] + "/" + dist.version + "/"
    )


def _verified_resources(root: Path) -> dict:
    legal_root = root / "packaging/licenses"
    manifest = json.loads((legal_root / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    for item in manifest["resources"]:
        resource = PurePosixPath(item["path"])
        if resource.is_absolute() or ".." in resource.parts:
            raise RuntimeError("Invalid legal resource path")
        path = legal_root.joinpath(*resource.parts)
        if not path.is_file() or sha256(path) != item["sha256"]:
            raise RuntimeError("Legal resource checksum mismatch: " + item["path"])
    sources = [manifest["opcua_source"], *manifest.get("additional_sources", ())]
    for item in sources:
        resource = PurePosixPath(item["path"])
        if resource.is_absolute() or ".." in resource.parts:
            raise RuntimeError("Invalid third-party source path")
        path = root.joinpath(*resource.parts)
        if not path.is_file() or sha256(path) != item["sha256"]:
            raise RuntimeError("Third-party source checksum mismatch: " + path.name)
    return manifest


def collect(root: Path, out: Path) -> dict:
    """Write licenses, notices, source materials, and manifest into ``out``.

    The output may share a stage directory with frontend/config assets. Legal
    subdirectories are refreshed so a rebuilt release cannot retain stale texts.
    Network access is deliberately unnecessary at build time.
    """
    root, out = Path(root).resolve(), Path(out).resolve()
    if out == root or out in (root / "packaging", root / "packaging/licenses"):
        raise ValueError("License output must be a separate build directory")
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Frozen release license resources require Python 3.12")
    resources = _verified_resources(root)
    out.mkdir(parents=True, exist_ok=True)
    for folder in ("licenses", "third_party_sources"):
        if (out / folder).exists():
            shutil.rmtree(out / folder)
        (out / folder).mkdir()
    records = []

    def add_file(source: Path, relative: str, origin: str) -> dict:
        item = _copy(source, out / relative)
        item["path"] = relative
        item["source"] = origin
        return item

    fallbacks = {
        normalize_name(name): value for name, value in resources["package_fallbacks"].items()
    }
    distributions = runtime_distributions()
    # PyInstaller's exception permits generated executables; retaining its full
    # COPYING is useful provenance for the bootloader bundled in the EXE.
    try:
        distributions.append(metadata.distribution("pyinstaller"))
    except metadata.PackageNotFoundError:
        pass
    for dist in distributions:
        name = normalize_name(dist.metadata["Name"])
        files = []
        for resource in sorted(dist.files or (), key=str):
            parts = PurePosixPath(str(resource)).parts
            if not any(part.endswith((".dist-info", ".egg-info")) for part in parts):
                continue
            if not LICENSE_NAMES.match(parts[-1]):
                continue
            info_index = next(i for i, part in enumerate(parts) if part.endswith((".dist-info", ".egg-info")))
            relative = PurePosixPath(*parts[info_index + 1:]).as_posix()
            files.append(add_file(Path(dist.locate_file(resource)),
                f"licenses/packages/{name}/{relative}", f"installed-distribution:{name}=={dist.version}"))
        fallback = fallbacks.get(name)
        if fallback:
            if fallback["version"] != dist.version:
                raise RuntimeError("Checked-in license version mismatch: " + name)
            need_fallback = not files or name == "opcua"
            for relative in fallback["files"]:
                # opcua's sdist lacks legal files: always attach both full GPL
                # and LGPL texts, even if a future installed wheel adds one.
                if need_fallback:
                    files.append(add_file(root / "packaging/licenses" / relative,
                        f"licenses/packages/{name}/" + Path(relative).name, fallback["source"]))
        if not files:
            raise RuntimeError("No complete license text found for installed package: " + name)
        records.append({"name": name, "version": dist.version, "license": _license_label(dist),
                        "source": _source_url(dist), "files": files})

    runtime_files = []
    lookup = {item["path"]: item for item in resources["resources"]}
    for name, paths in resources["runtime_fallbacks"].items():
        files = [add_file(root / "packaging/licenses" / path,
                    f"licenses/runtime/{name}/" + Path(path).name, lookup[path]["source"])
                 for path in paths]
        runtime_files.append({"name": name, "files": files})
    vue = root / "frontend/vendor/VUE-LICENSE.txt"
    vue_script = (root / "frontend/vendor/vue.global.prod.js").read_text(encoding="utf-8")[:300]
    match = re.search(r"Vue(?:\.js)?\s+v([\d.]+)", vue_script)
    runtime_files.append({"name": "Vue", "version": match.group(1) if match else "See vendor header",
        "license": "MIT", "files": [add_file(vue, "licenses/frontend/VUE-LICENSE.txt",
                                             "https://github.com/vuejs/core/blob/main/LICENSE")]})
    project_license = add_file(root / "LICENSE", "LICENSE", "project:Industrial_AI_Gateway")
    source_records = []
    for source in [resources["opcua_source"], *resources.get("additional_sources", ())]:
        file = add_file(root / source["path"], "third_party_sources/" + Path(source["path"]).name,
                        source["source"])
        source_records.append({"name": source.get("name", "opcua"), "version": source["version"],
                               "file": file})
    add_file(root / "packaging/third_party_sources/REBUILD.md", "third_party_sources/REBUILD.md",
             "project:third-party-rebuild-instructions")
    add_file(root / "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md", "project:third-party-notices")
    manifest = {"format": 1, "python_version": sys.version.split()[0],
                "openssl_version": ssl.OPENSSL_VERSION, "project_license": project_license,
                "runtime_root_dependencies": list(RUNTIME_ROOTS), "packages": records,
                "runtime_resources": runtime_files, "source_archives": source_records}
    (out / "LICENSE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                              encoding="utf-8")
    table = ["", "## 此构建的依赖清单", "", "完整文本位于以下文件；校验值见 `LICENSE_MANIFEST.json`。", "",
             "| 组件 | 版本 | 上游声明的许可 |", "| --- | --- | --- |"]
    table.extend(f"| {r['name']} | {r['version']} | {r['license'].replace('|', '/')} |" for r in records)
    with (out / "THIRD_PARTY_NOTICES.md").open("a", encoding="utf-8") as handle:
        handle.write("\n".join(table) + "\n")
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    manifest = collect(arguments.root, arguments.out)
    print(json.dumps({"packages": len(manifest["packages"]), "sources": len(manifest["source_archives"]),
                      "manifest": str(arguments.out / "LICENSE_MANIFEST.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
