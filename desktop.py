"""Standalone desktop entry point; PyInstaller bundles its Python runtime."""
from multiprocessing import freeze_support

from scripts.desktop_runtime import main


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
