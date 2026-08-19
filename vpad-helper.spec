# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the V-Pad Helper tray app.

No console window — the whole point is that a person who wants to play a
game never meets Python.

Two layouts, chosen by the VPAD_ONEDIR environment variable:

    unset   one file, one double-click. The standalone download.
    set     a folder. What `installer.iss` packages.

The folder build exists because of antivirus, not tidiness. A one-file
build is a self-extracting archive: every launch unpacks ~20 MB into
%TEMP% and runs it from there, which is both the slowest way to start and
the behaviour heuristic scanners are built to catch. Once an installer is
in the picture there is nothing left to gain from it — the files can just
sit in Program Files. See the AV note in .github/workflows/build.yml.

Build (Windows, from the repo root):
    pip install -r requirements.txt pyinstaller
    pyinstaller vpad-helper.spec

CI does exactly that: .github/workflows/build.yml
"""
import ast
import os
import re
import sys

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(SPECPATH)  # noqa: F821 — PyInstaller global

ONEDIR = bool(os.environ.get("VPAD_ONEDIR"))

# The macOS build drives this same spec (make-dmg.sh takes the .app it
# produces). Everything below that is a Windows resource — the .ico, the
# version block, the folder layout — has to stay off that path: macOS
# wants .icns, has no version resource, and ships a .app bundle.
IS_WINDOWS = sys.platform == "win32"


def _read_metadata():
    """Pull __version__/PUBLISHER/HOMEPAGE out of vpad_helper.py.

    Parsed, not imported: importing would run the module's top-level code,
    which inserts into sys.path and pulls in zeroconf — on a build host
    that is both pointless and a way for a packaging run to fail for a
    runtime reason.
    """
    src = open(os.path.join(ROOT, "vpad_helper.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            name = getattr(node.targets[0], "id", None)
            if name in ("__version__", "PUBLISHER", "HOMEPAGE", "APP_NAME"):
                out[name] = ast.literal_eval(node.value)
    missing = {"__version__", "PUBLISHER", "HOMEPAGE", "APP_NAME"} - out.keys()
    if missing:
        raise SystemExit("vpad_helper.py is missing %s" % ", ".join(sorted(missing)))
    return out


META = _read_metadata()

# Windows wants a 4-part numeric version in the resource even though we
# show a 3-part one. Only the leading dotted-numeric run is used, so a
# pre-release suffix is dropped whole: "1.0.0-rc2" must become (1,0,0,0),
# not (1,0,0,2) — the digit in "rc2" is not a build number.
_lead = re.match(r"\d+(?:\.\d+)*", META["__version__"])
if not _lead:
    raise SystemExit("__version__ %r does not start with a number"
                     % META["__version__"])
_parts = [int(p) for p in _lead.group(0).split(".")][:4]
FILEVERS = tuple((_parts + [0, 0, 0, 0])[:4])

def _write_version_resource():
    """Emit the Windows version resource and return its path.

    Without this, every field in the file's Properties tab is blank —
    which is exactly what an unattributed binary looks like, both to a
    suspicious user and to a reputation scanner.
    """
    path = os.path.join(ROOT, "build", "version_info.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = (
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(filevers={vers}, prodvers={vers}, mask=0x3f,\n"
        "    flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),\n"
        "  kids=[\n"
        "    StringFileInfo([StringTable('040904B0', [\n"
        "      StringStruct('CompanyName', {pub!r}),\n"
        "      StringStruct('FileDescription', {desc!r}),\n"
        "      StringStruct('FileVersion', {ver!r}),\n"
        "      StringStruct('InternalName', 'vpad_helper'),\n"
        "      StringStruct('LegalCopyright', {copy!r}),\n"
        "      StringStruct('OriginalFilename', {orig!r}),\n"
        "      StringStruct('ProductName', {name!r}),\n"
        "      StringStruct('ProductVersion', {ver!r}),\n"
        "    ])]),\n"
        # 1033 = en-US, 1200 = UTF-16. Must agree with the '040904B0'
        # above, or Explorer shows the fields blank despite them being
        # present in the binary.
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])]),\n"
        "  ]\n"
        ")\n"
    ).format(
        vers=FILEVERS,
        pub=META["PUBLISHER"],
        desc="Companion app that lets V-Pad on your phone act as a gamepad",
        ver=META["__version__"],
        copy="Copyright (C) V-Pad. MIT licensed. %s" % META["HOMEPAGE"],
        orig="%s.exe" % META["APP_NAME"],
        name=META["APP_NAME"],
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


VERSION_RC = _write_version_resource() if IS_WINDOWS else None

ICON = os.path.join(ROOT, "vpad-helper.ico")
if IS_WINDOWS and not os.path.exists(ICON):
    raise SystemExit("vpad-helper.ico is missing — run `python make-icon.py`")

# The engine lives in a subfolder that `vpad_helper` puts on sys.path at
# runtime. Static analysis cannot follow that, so the folder is named here
# too — otherwise `import vpad_host` resolves during the build to nothing
# and the .exe dies on its first line.
ENGINE_DIR = os.path.join(ROOT, "android-vpad-helper", "host")
if not os.path.exists(os.path.join(ENGINE_DIR, "vpad_host.py")):
    raise SystemExit("engine missing: %s" % ENGINE_DIR)

# vpad_host imports its siblings normally, so listing the entry point is
# strictly enough. They are spelled out anyway because leaving one behind
# fails at runtime, in a tray app with no console, on the user's machine.
#
# `qrcode` is imported lazily inside two functions (the terminal renderer
# and the tray's pairing card). PyInstaller does walk function bodies, but
# this one is load-bearing: without it the tray has no way to show a QR,
# which is the only way a phone can pair.
hidden = ["vpad_host", "vpad_devices", "vpad_pairing", "vpad_slots",
          "qrcode", "qrcode.image.pil",
          # The pairing window. Imported inside the function that opens it,
          # so it is spelled out here rather than trusted to the scan.
          "tkinter"]

# vgamepad ships the ViGEmBus installer as package data. Carrying it means
# the tray's "Install gamepad driver…" item can run a local installer
# instead of sending the user to a GitHub releases page. Absent on
# non-Windows build hosts — degrade quietly, the helper falls back to the
# download URL at runtime.
# The brand badge. The tray builds its icon from it at runtime, so it has
# to be in the bundle, not just baked into the .ico.
#
# The .ico ships too, and for the same reason. It was previously used ONLY
# as the exe's Windows resource (`ICON` above), which brands the taskbar
# and Explorer — but the pairing window asks for the file at RUNTIME
# (`_bundled("vpad-helper.ico")` → `root.iconbitmap(...)`). Measured on an
# installed 0.3.x build: no .ico anywhere under the install directory, so
# that call raised, the caller swallowed it, and the window kept Tk's
# default feather. Baking an icon into the exe does not put a readable
# file next to it.
datas = [(os.path.join(ROOT, "brand", "icongamepad.png"), "."),
         (ICON, ".")]
try:
    datas += collect_data_files("vgamepad", include_py_files=False)
except Exception as exc:  # pragma: no cover — build-host dependent
    print("spec: vgamepad data not collected (%s)" % exc)

a = Analysis(  # noqa: F821 — PyInstaller global
    [os.path.join(ROOT, "vpad_helper.py")],
    pathex=[ROOT, ENGINE_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    # tkinter is NOT excluded: the pairing window is a real Tk window.
    # Tcl/Tk is in the bundle for the splash either way, so the marginal
    # cost is _tkinter.pyd and the tkinter package.
    excludes=["unittest", "pydoc_data"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821 — PyInstaller global

# Splash screen. Windows only — PyInstaller does not support it on macOS,
# and this same spec drives make-dmg.sh.
#
# It buys the two seconds between the double-click and the tray icon. A
# tray app opens no window, Windows 11 hides new tray icons behind the
# overflow arrow, and the first field report about this app was that it
# "doesn't open". The cost is ~7 MB of Tcl/Tk in the bundle.
#
# `vpad_helper` MUST close it (see `_close_splash`): the splash lives
# until it is told to go, and a tray app never exits on its own — an
# uncancelled splash would sit always-on-top for the whole session.
SPLASH_PNG = os.path.join(ROOT, "vpad-splash.png")
splash = None
if IS_WINDOWS:
    if not os.path.exists(SPLASH_PNG):
        raise SystemExit("vpad-splash.png is missing — run `python make-splash.py`")
    splash = Splash(  # noqa: F821 — PyInstaller global
        SPLASH_PNG,
        binaries=a.binaries,
        datas=a.datas,
        # Lands inside the darker band at the bottom of the artwork, which
        # is left empty by make-splash.py for exactly this.
        text_pos=(24, 214),
        text_size=10,
        text_color="#a8bad6",
        text_default="Starting…",
        always_on_top=True,
    )

# One EXE() call for both layouts. The only difference is where the
# payload goes: inside the executable (one-file) or beside it (one-dir,
# where `exclude_binaries` hands it to COLLECT instead).
_payload = [] if ONEDIR else [a.binaries, a.datas]

# The splash's own Tcl/Tk binaries follow the same rule as the rest of the
# payload: inside the .exe for one-file, handed to COLLECT for one-dir.
_splash = ([] if splash is None
           else [splash] + ([] if ONEDIR else [splash.binaries]))

exe = EXE(  # noqa: F821 — PyInstaller global
    pyz,
    a.scripts,
    *_splash,
    *_payload,
    [],
    exclude_binaries=ONEDIR,
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
    icon=ICON if IS_WINDOWS else None,
    version=VERSION_RC,
)

if ONEDIR:
    coll = COLLECT(  # noqa: F821 — PyInstaller global
        exe,
        *([splash.binaries] if splash is not None else []),
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name="V-Pad Helper",
    )
