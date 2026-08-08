# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the V-Pad Helper tray app.

Produces ONE file, ONE double-click, no console window — the whole point
is that a person who wants to play a game never meets Python.

Build (Windows, from the repo root):
    pip install -r requirements.txt pyinstaller
    pyinstaller vpad-helper.spec

CI does exactly that: .github/workflows/build.yml
"""
import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(SPECPATH)  # noqa: F821 — PyInstaller global

# `vpad_helper` reaches its engine via a sys.path insert, which static
# analysis cannot see — name it explicitly or the engine is left out of
# the bundle and the .exe dies on import.
hidden = ["vpad_daemon"]

# vgamepad ships the ViGEmBus installer as package data. Carrying it means
# the tray's "Install gamepad driver…" item can run a local installer
# instead of sending the user to a GitHub releases page. Absent on
# non-Windows build hosts — degrade quietly, the helper falls back to the
# download URL at runtime.
datas = []
try:
    datas += collect_data_files("vgamepad", include_py_files=False)
except Exception as exc:  # pragma: no cover — build-host dependent
    print("spec: vgamepad data not collected (%s)" % exc)

a = Analysis(  # noqa: F821 — PyInstaller global
    [os.path.join(ROOT, "vpad_helper.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc_data"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821 — PyInstaller global

exe = EXE(  # noqa: F821 — PyInstaller global
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="V-Pad Helper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console: this is a tray app. Errors still reach the tray status
    # line, because the helper tees stdout into it.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
