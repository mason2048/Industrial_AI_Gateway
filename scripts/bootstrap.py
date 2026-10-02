"""Create Python 3.12 venv and install only when the lockfile digest changes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def lock_digest(root: Path, windows_service: bool = False) -> str:
    names = ["requirements-lock.txt"]
    if windows_service:
        names.append("requirements-windows-lock.txt")
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update((root / name).read_bytes())
    digest.update(json.dumps({"python": "3.12", "platform": sys.platform,
                              "service": windows_service}, sort_keys=True).encode())
    return digest.hexdigest()


def ensure_environment(root: Path = ROOT, *, windows_service: bool = False,
                       wheelhouse: Path | None = None, offline: bool = False) -> Path:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Bootstrap requires Python 3.12 (Windows: py -3.12; macOS: python3.12).")
    if windows_service and os.name != "nt":
        raise RuntimeError("The service dependency bundle is Windows-only.")
    environment = root / ".venv"
    interpreter = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    version = subprocess.check_output([str(interpreter), "-c", "import sys; print('%s.%s' % sys.version_info[:2])"], text=True).strip()
    if version != "3.12":
        raise RuntimeError("Existing .venv is not Python 3.12. Rename it and run bootstrap again.")
    # Separate stamps avoid a normal start downgrading/invalidating service setup.
    stamp = environment / ("dependencies-windows.sha256" if windows_service else "dependencies.sha256")
    wanted = lock_digest(root, windows_service)
    previous = stamp.read_text(encoding="ascii").strip() if stamp.exists() else ""
    if wanted != previous:
        requirement = "requirements-windows-lock.txt" if windows_service else "requirements-lock.txt"
        command = [str(interpreter), "-m", "pip", "install"]
        if offline:
            command.append("--no-index")
            wheelhouse = wheelhouse or root / "wheels"
        if wheelhouse is not None:
            wheelhouse = wheelhouse.resolve()
            if not wheelhouse.is_dir():
                raise RuntimeError(f"Wheel directory does not exist: {wheelhouse}")
            command.extend(["--find-links", str(wheelhouse)])
        command.extend(["-r", str(root / requirement)])
        subprocess.run(command, check=True, cwd=root)
        subprocess.run([str(interpreter), "-m", "pip", "check"], check=True, cwd=root)
        temporary = stamp.with_suffix(".tmp")
        temporary.write_text(wanted + "\n", encoding="ascii")
        temporary.replace(stamp)
        if windows_service:
            # The Windows lock includes the complete base lock. A first service
            # installation is also a complete foreground/offline installation.
            base_stamp = environment / "dependencies.sha256"
            temporary = base_stamp.with_suffix(".tmp")
            temporary.write_text(lock_digest(root) + "\n", encoding="ascii")
            temporary.replace(base_stamp)
    return interpreter


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "start", "test", "service-init", "check", "prepare-offline"))
    parser.add_argument("--offline", action="store_true", help="Install without contacting a package server")
    parser.add_argument("--wheelhouse", type=Path, help="Local dependency wheels; defaults to ./wheels with --offline")
    parser.add_argument("--with-service", action="store_true", help="Include optional Windows service dependencies")
    args, remaining = parser.parse_known_args(argv)
    try:
        if args.command == "prepare-offline":
            if os.name != "nt":
                raise RuntimeError("Prepare offline Windows dependencies on a Windows computer using Python 3.12.")
            if sys.version_info[:2] != (3, 12):
                raise RuntimeError("Python 3.12 is required.")
            if args.offline or remaining:
                raise RuntimeError("prepare-offline needs Internet and accepts only --wheelhouse / --with-service.")
            destination = (args.wheelhouse or ROOT / "wheels").resolve()
            destination.mkdir(parents=True, exist_ok=True)
            requirement = "requirements-windows-lock.txt" if args.with_service else "requirements-lock.txt"
            subprocess.run([sys.executable, "-m", "pip", "wheel", "--wheel-dir", str(destination),
                            "-r", str(ROOT / requirement)], check=True, cwd=ROOT)
            print(f"Offline Windows Python 3.12 wheels are ready: {destination}")
            return 0
        if args.with_service and args.command not in ("init", "service-init"):
            raise RuntimeError("--with-service is available only for init or prepare-offline.")
        if args.command in ("init", "service-init") and remaining:
            raise RuntimeError("Unknown installation arguments: " + " ".join(remaining))
        if args.command == "check":
            if args.offline or args.wheelhouse or args.with_service:
                raise RuntimeError("check does not install dependencies; run init first.")
            interpreter = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not interpreter.is_file():
                raise RuntimeError("Python environment is missing. Run install.bat first.")
            return subprocess.call([str(interpreter), str(ROOT / "launch.py"), "--check", *remaining], cwd=ROOT)
        interpreter = ensure_environment(windows_service=args.command == "service-init" or args.with_service,
                                         wheelhouse=args.wheelhouse, offline=args.offline)
        if args.command in ("init", "service-init"):
            print("Python 3.12 environment and locked dependencies are ready.")
            return 0
        if args.command == "start":
            command = [str(interpreter), str(ROOT / "launch.py"), *remaining]
        else:
            command = [str(interpreter), "-m", "pytest", *remaining]
        return subprocess.call(command, cwd=ROOT)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Environment setup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
