#!/usr/bin/env python3
"""Build the Lambda deployment artifacts for the Optira MCP server.

Produces two zips under ``infra/packaging/`` (Docker-free, matching the pattern
used by the other Optira stacks):

- ``dependencies.zip`` -- the runtime dependencies laid out as a Lambda layer
  (``python/...``), installed as ARM64 (manylinux aarch64) wheels for the
  Python 3.12 Lambda runtime.
- ``app.zip`` -- the ``optira_mcp`` package (the function code).

Usage:
    python3 bin/package_for_lambda.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile

# Paths
INFRA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_ROOT = os.path.dirname(INFRA_DIR)  # es-optira-mcp/
OPTIRA_PKG = os.path.join(PACKAGE_ROOT, "optira_mcp")
REQUIREMENTS = os.path.join(PACKAGE_ROOT, "requirements.txt")

PACKAGING_DIR = os.path.join(INFRA_DIR, "packaging")
DEPS_BUILD = os.path.join(PACKAGING_DIR, "deps_build")
APP_BUILD = os.path.join(PACKAGING_DIR, "app_build")
DEPS_ZIP = os.path.join(PACKAGING_DIR, "dependencies.zip")
APP_ZIP = os.path.join(PACKAGING_DIR, "app.zip")

# Lambda target: Python 3.12 on ARM64 (see architecture in infra/app.py).
PYTHON_VERSION = "3.12"
PLATFORM = "manylinux2014_aarch64"


def _clean() -> None:
    if os.path.exists(PACKAGING_DIR):
        shutil.rmtree(PACKAGING_DIR)
    os.makedirs(DEPS_BUILD)
    os.makedirs(APP_BUILD)


def _install_dependencies() -> None:
    # Layer layout requires dependencies under a top-level "python/" directory.
    target = os.path.join(DEPS_BUILD, "python")
    os.makedirs(target, exist_ok=True)
    cmd = [
        sys.executable, "-m", "pip", "install",
        "-r", REQUIREMENTS,
        "--platform", PLATFORM,
        "--python-version", PYTHON_VERSION,
        "--implementation", "cp",
        "--only-binary=:all:",
        "--upgrade",
        "--target", target,
    ]
    print("Installing dependencies:\n  " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def _copy_app_code() -> None:
    dest = os.path.join(APP_BUILD, "optira_mcp")
    shutil.copytree(
        OPTIRA_PKG,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def _zip_dir(source_root: str, zip_path: str) -> None:
    """Zip the *contents* of source_root (arcnames relative to it)."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(source_root):
            for name in files:
                full = os.path.join(root, name)
                arc = os.path.relpath(full, source_root)
                zf.write(full, arc)


def main() -> None:
    print("== Optira MCP Lambda packaging ==")
    _clean()
    _install_dependencies()
    _copy_app_code()

    print("Creating dependencies.zip (layer)...")
    _zip_dir(DEPS_BUILD, DEPS_ZIP)
    print("Creating app.zip (function code)...")
    _zip_dir(APP_BUILD, APP_ZIP)

    deps_mb = os.path.getsize(DEPS_ZIP) / (1024 * 1024)
    app_mb = os.path.getsize(APP_ZIP) / (1024 * 1024)
    print(f"  dependencies.zip = {deps_mb:.1f} MB")
    print(f"  app.zip          = {app_mb:.1f} MB")
    print("Done.")


if __name__ == "__main__":
    main()
