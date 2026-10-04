"""Freeze Python, dependencies, migrations and frontend into a relocatable runtime."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "desktop"
env = os.environ.copy()
env["PYINSTALLER_CONFIG_DIR"] = str(DESKTOP / "build/pyinstaller-cache")
subprocess.run([
    sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir",
    "--name", "youdekan-backend",
    "--distpath", str(DESKTOP / "src-tauri/runtime"),
    "--workpath", str(DESKTOP / "build/pyinstaller"),
    "--specpath", str(DESKTOP / "build"),
    "--paths", str(ROOT),
    "--add-data", f"{ROOT / 'web'}{os.pathsep}web",
    "--add-data", f"{ROOT / 'configs'}{os.pathsep}configs",
    "--add-data", f"{ROOT / 'alembic.ini'}{os.pathsep}.",
    "--add-data", f"{ROOT / 'server/database/migrations'}{os.pathsep}server/database/migrations",
    "--collect-submodules", "uvicorn",
    "--hidden-import", "sqlalchemy.dialects.sqlite",
    str(DESKTOP / "scripts/backend_entry.py"),
], cwd=ROOT, env=env, check=True)
