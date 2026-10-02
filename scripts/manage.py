"""Offline maintenance and cooperative shutdown. Run with python -m scripts.manage."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stop", help="Request graceful shutdown; never kills a PID")
    commands.add_parser("backup", help="Create and validate a consistent backup")
    restore = commands.add_parser("restore", help="Restore a verified backup while gateway is stopped")
    restore.add_argument("backup_path", type=Path, help="Exact backup directory containing manifest.json")
    verify = commands.add_parser("verify-backup", help="Verify checksums and SQLite integrity without restoring")
    verify.add_argument("backup_path", type=Path)
    args = parser.parse_args(argv)
    from launch import check_python
    from backend.maintenance import create_backup, restore_backup, validate_backup
    from scripts.runtime import request_stop
    try:
        check_python()
        if args.command == "stop":
            result = request_stop(args.root)
            print(result["message"])
            return 0 if result["stopped"] else 1
        if args.command == "backup":
            result = {"backup": str(create_backup(args.root))}
        elif args.command == "restore":
            result = restore_backup(args.root, args.backup_path)
        else:
            result = validate_backup(args.backup_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"Maintenance failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
