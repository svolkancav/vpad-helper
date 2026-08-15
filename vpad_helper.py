#!/usr/bin/env python3
"""
V-Pad Helper — the tray app end users install on their computer.

Same engine as `vpad_daemon.py` (imported, not duplicated), wrapped so a
non-technical user never sees a terminal: double-click, an icon appears in
the tray/menu bar, the phone finds the computer by itself.

Why this exists: iOS forbids third-party apps from acting as a Bluetooth
gamepad (CoreBluetooth rejects the HID service UUID 0x1812 — it is
reserved for the system), so the phone reaches the computer over Wi-Fi and
something on this end has to turn those messages into gamepad input. Every
iOS app in this category ships such a helper. The only thing that makes it
feel heavy is the install, so this file's whole job is to make the install
a double-click and then disappear.

Tray menu
    • status line (waiting / connected to <phone>)
    • injection backend in use
    • "Install gamepad driver" — only when ViGEmBus is missing (Windows)
    • "Start with Windows" — registry Run key toggle
    • Quit

Without `pystray` installed it degrades to console mode and keeps working,
so a developer running it from a checkout gets the same engine and logs.

Build the Windows one-file .exe from CI (see
`.github/workflows/build.yml`), or locally on a Windows box:

    pip install -r requirements.txt pyinstaller
    pyinstaller vpad-helper.spec
"""
from __future__ import annotations

import io
import os
import queue
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_daemon as engine  # noqa: E402 — path must be set first

APP_NAME = "V-Pad Helper"

# Single source of truth for the version. `vpad-helper.spec` parses it into
# the .exe's Windows version resource, CI passes it to `installer.iss` for
# the Add/Remove Programs entry, and the tray menu shows it.
#
# On a `v*` tag build CI asserts that this equals the tag and fails the
# build otherwise — bumping the tag alone would ship an .exe whose
# properties still claimed the previous release.
__version__ = "0.2.2"

PUBLISHER = "V-Pad"
HOMEPAGE = "https://vpadcontroller.com/"
VIGEM_DOWNLOAD_URL = "https://github.com/nefarius/ViGEmBus/releases/latest"

IS_WINDOWS = sys.platform == "win32"


# ── Status plumbing ─────────────────────────────────────────────────
#
# The engine is console-first: it prints. Rather than rewire it into a
# callback API (and risk drifting from the CLI path the protocol was
# verified against), we tee its stdout through a queue and let the tray
# read the last meaningful line. One writer, one reader, no locks.

def log_dir() -> str:
    """Where the log lives. A windowless .exe writes to no console, so
    without this file a user's "it doesn't work" is undiagnosable."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Logs")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    path = os.path.join(base, APP_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        return ""
    return path


def _open_log():
    folder = log_dir()
    if not folder:
        return None
    path = os.path.join(folder, "helper.log")
    try:
        # Truncate a log that grew past ~1 MB rather than rotate: this is a
        # troubleshooting aid, and only the current session matters.
        if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
            os.remove(path)
        return io.open(path, "a", encoding="utf-8", errors="replace")
    except Exception:
        return None


class _Tee:
    """stdout proxy: forwards to the real stream, a queue, and the log."""

    def __init__(self, stream, sink: queue.Queue, log=None):
        self._stream = stream
        self._sink = sink
        self._log = log

    def write(self, text: str) -> int:
        try:
            if self._stream is not None:
                self._stream.write(text)
        except Exception:
            pass
        if self._log is not None:
            try:
                self._log.write(text)
                self._log.flush()
            except Exception:
                pass
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    self._sink.put_nowait(line)
                except queue.Full:
                    pass
        return len(text)

    def flush(self) -> None:
        try:
            if self._stream is not None:
                self._stream.flush()
        except Exception:
            pass


class HelperState:
    """What the tray shows. Derived from the engine's own log lines so
    there is exactly one source of truth about the session."""

    def __init__(self) -> None:
        self.backend = "starting…"
        self.connected_to: str | None = None
        self.addresses: list[str] = []
        self.driver_missing = False
        # The injector is chosen once at startup, so installing the driver
        # mid-session changes nothing until the app is restarted. Saying so
        # is the difference between "it works now" and a silent dead end.
        self.driver_install_started = False
        self.driver_note = ""

    def absorb(self, line: str) -> None:
        if "Injection:" in line:
            self.backend = line.split("Injection:", 1)[1].strip()
        elif "Published IPs:" in line:
            self.addresses = [
                a.strip() for a in
                line.split("Published IPs:", 1)[1].split(",") if a.strip()
            ]
        elif "HELLO proto_ver" in line and "name=" in line:
            # ▸ HELLO proto_ver=1 name='V-Pad' skin=''
            try:
                self.connected_to = line.split("name=", 1)[1].split(
                    "skin=")[0].strip().strip("'\"") or "phone"
            except Exception:
                self.connected_to = "phone"
        elif "session:" in line or "connection dropped" in line \
                or "BYE" in line:
            self.connected_to = None
        elif "vgamepad is not installed" in line \
                or "ViGEmBus backend unavailable" in line:
            self.driver_missing = True

    def summary(self) -> str:
        if self.connected_to:
            return "Connected: %s" % self.connected_to
        if self.addresses:
            return "Waiting for phone (%s)" % self.addresses[0]
        return "Starting…"


# ── Windows autostart (registry Run key) ────────────────────────────

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg  # noqa: PLC0415 — Windows-only
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
        return True
    except Exception:
        return False


def set_autostart(enabled: bool) -> None:
    """Point the Run key at the frozen .exe. Only meaningful once packaged
    — from a source checkout `sys.executable` is the interpreter, so this
    is a no-op there rather than registering a broken command."""
    if not IS_WINDOWS or not getattr(sys, "frozen", False):
        return
    try:
        import winreg  # noqa: PLC0415 — Windows-only
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key, APP_NAME, 0, winreg.REG_SZ, f'"{sys.executable}"')
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
    except Exception as exc:
        print("autostart toggle failed: %r" % (exc,))


# ── ViGEmBus driver install ─────────────────────────────────────────

def bundled_vigem_installer() -> str | None:
    """The ViGEmBus installer that ships inside `vgamepad`'s package data.

    PyInstaller copies it next to the frozen app (see the .spec), so a
    fresh user can get the driver without hunting GitHub. Returns None
    when it isn't there — then the menu falls back to the download page.
    """
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))
    try:
        import vgamepad  # noqa: PLC0415 — Windows-only, optional
        roots.append(os.path.dirname(os.path.abspath(vgamepad.__file__)))
    except Exception:
        pass
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                low = name.lower()
                if low.endswith((".msi", ".exe")) and "vigem" in low:
                    return os.path.join(dirpath, name)
    return None


# ── First-run dialogs (Win32, no toolkit) ───────────────────────────

_MB_OK = 0x0
_MB_YESNO = 0x4
_MB_ICONINFO = 0x40
_MB_ICONWARN = 0x30
_MB_ICONQUESTION = 0x20
_MB_TOPMOST = 0x40000
_IDYES = 6


def _message_box(text: str, flags: int) -> int:
    """MessageBoxW, or a printed line where there is no Win32."""
    if not IS_WINDOWS:
        print(text)
        return 0
    try:
        import ctypes  # noqa: PLC0415 — only needed for the dialog
        return int(ctypes.windll.user32.MessageBoxW(
            None, text, APP_NAME, flags | _MB_TOPMOST))
    except Exception as exc:
        print("dialog failed (%r): %s" % (exc, text))
        return 0


IS_MACOS = sys.platform == "darwin"


def _mac_dialog(text: str, buttons: str, default: str) -> str:
    """Native dialog via osascript — no toolkit, no dependency."""
    script = (
        'display dialog %s with title %s buttons %s default button %s'
        % (_as_str(text), _as_str(APP_NAME), buttons, _as_str(default))
    )
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=600)
        return (done.stdout or "").strip()
    except Exception as exc:
        print("dialog failed (%r): %s" % (exc, text))
        return ""


def _as_str(value: str) -> str:
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def ask_yes_no(text: str) -> bool:
    if IS_MACOS:
        return "button returned:Yes" in _mac_dialog(
            text, '{"Later", "Yes"}', "Yes")
    return _message_box(text, _MB_YESNO | _MB_ICONQUESTION) == _IDYES


def tell(text: str, warn: bool = False) -> None:
    if IS_MACOS:
        _mac_dialog(text, '{"OK"}', "OK")
        return
    _message_box(text, _MB_OK | (_MB_ICONWARN if warn else _MB_ICONINFO))


# ── macOS: Accessibility permission ─────────────────────────────────

ACCESSIBILITY_PANE = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_Accessibility"
)


def mac_accessibility_trusted() -> bool:
    """Is this process allowed to post synthetic input?

    Without it CGEvent posts are accepted and then **silently discarded** —
    the exact same failure shape as a missing gamepad driver on Windows:
    the bridge connects, reports arrive, and nothing moves. Detected with
    `AXIsProcessTrusted` (the non-prompting variant, so we control the
    explanation ourselves).
    """
    if not IS_MACOS:
        return True
    try:
        import ctypes  # noqa: PLC0415
        import ctypes.util  # noqa: PLC0415
        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return False
        lib = ctypes.cdll.LoadLibrary(path)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception as exc:
        print("accessibility probe failed: %r" % (exc,))
        return False


def first_run_accessibility_flow() -> None:
    """macOS counterpart of the driver prompt: ask up front, explain why,
    and open the exact settings pane instead of describing where it is."""
    if not IS_MACOS or mac_accessibility_trusted():
        return
    print("accessibility: not trusted — input will be discarded")
    if ask_yes_no(
        "V-Pad needs Accessibility permission to move the mouse and press "
        "keys on this Mac.\n\nWithout it the phone still connects but "
        "nothing happens.\n\nOpen the setting now?"
    ):
        _open_url(ACCESSIBILITY_PANE)
        tell(
            "In the list that opened, switch on the app running V-Pad "
            "Helper, then quit and reopen it.",
        )
    else:
        tell(
            "Skipped. The phone will connect but no input will reach this "
            "Mac until Accessibility is allowed.",
            warn=True,
        )


def ask_install_driver() -> bool:
    return _message_box(
        "V-Pad needs a one-time gamepad driver (ViGEmBus) so games see a "
        "real controller.\n\nInstall it now? Windows will ask for your "
        "permission.",
        _MB_YESNO | _MB_ICONQUESTION) == _IDYES


def first_run_driver_flow(state: "HelperState") -> None:
    """Offer the driver install on startup, BEFORE the engine starts.

    A tray-only affordance was the wrong shape: nobody double-clicks an exe
    expecting to hunt for an icon near the clock and find a menu item there
    (first user report said exactly that). Asking up front also removes the
    quit-and-reopen step, because the engine picks its injection backend
    once at startup — install first and it simply comes up as a real
    gamepad.
    """
    if not IS_WINDOWS or driver_present():
        return
    state.driver_missing = True
    if not ask_install_driver():
        _message_box(
            "Skipped. Games will not see a controller until the driver is "
            "installed — you can do it later from the tray menu.",
            _MB_OK | _MB_ICONWARN)
        return
    state.driver_install_started = True
    state.driver_note = install_driver()
    print("driver install: %s" % state.driver_note)
    if driver_present():
        state.driver_missing = False
        _message_box(
            "Gamepad driver installed. V-Pad Helper is starting — its icon "
            "lives in the system tray (click the ^ arrow if you don't see "
            "it).",
            _MB_OK | _MB_ICONINFO)
    else:
        _message_box(
            "The driver is still not detected. Windows may need a restart "
            "to finish installing it — reboot, then run V-Pad Helper again.",
            _MB_OK | _MB_ICONWARN)


def driver_present() -> bool:
    """Can we actually open the ViGEm bus right now?

    The authoritative test, not a registry guess: vgamepad raises
    VIGEM_ERROR_BUS_NOT_FOUND when the driver is absent, which is exactly
    the failure a user sees as "Steam shows no controller".
    """
    if not IS_WINDOWS:
        return False
    try:
        import vgamepad as vg  # noqa: PLC0415 — Windows-only, optional
        pad = vg.VX360Gamepad()
        del pad
        return True
    except Exception:
        return False


def install_driver() -> str:
    """Install ViGEmBus from the bundled MSI, elevated, and wait for it.

    Three things here were wrong in 0.1.x and each one alone was enough to
    leave the user with no controller:

    * The MSI lives in PyInstaller's extraction dir (`sys._MEIPASS`), which
      is **deleted when the app exits**. The tray told the user to quit and
      reopen right after starting the install — pulling the installer's own
      source out from under it. It is copied to %TEMP% first now.
    * `msiexec /i` was launched without elevation. Installing a kernel-mode
      driver needs admin, so UAC may never have appeared at all. Launch it
      through ShellExecute's `runas` verb so the prompt is guaranteed.
    * Nothing waited for, or checked, the result. We now block on the
      installer and re-test the bus, so the tray can say what actually
      happened instead of leaving the user to guess.

    Returns a short human-readable outcome for the tray.
    """
    if not IS_WINDOWS:
        _open_url(VIGEM_DOWNLOAD_URL)
        return "Opened the driver download page"

    installer = bundled_vigem_installer()
    if not installer:
        _open_url(VIGEM_DOWNLOAD_URL)
        return "Installer not bundled — opened the download page"

    # Copy out of _MEIPASS: that directory vanishes with the process.
    try:
        import shutil  # noqa: PLC0415 — only needed on this path
        import tempfile  # noqa: PLC0415
        staged = os.path.join(tempfile.gettempdir(), os.path.basename(installer))
        shutil.copy2(installer, staged)
    except Exception as exc:
        print("could not stage the installer: %r" % (exc,))
        staged = installer

    try:
        # -Wait so we know when it finished; -Verb RunAs so UAC is raised
        # even though we are not elevated.
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Start-Process msiexec -ArgumentList '/i','%s' -Verb RunAs -Wait"
             % staged.replace("'", "''")],
            capture_output=True, text=True, timeout=900)
        if completed.returncode != 0:
            detail = (completed.stderr or "").strip().splitlines()
            print("installer returned %d: %s"
                  % (completed.returncode, detail[-1] if detail else "?"))
            return "Driver install was cancelled or failed"
    except Exception as exc:
        print("driver installer failed: %r" % (exc,))
        _open_url(VIGEM_DOWNLOAD_URL)
        return "Could not start the installer — opened the download page"

    if driver_present():
        return "Driver installed — quit and reopen to use it"
    return "Driver still not detected (a reboot may be required)"


def _install(state: "HelperState") -> None:
    state.driver_install_started = True
    state.driver_note = "Installing driver…"
    state.driver_note = install_driver()
    print("driver install: %s" % state.driver_note)


def _open_url(url: str) -> None:
    try:
        if IS_WINDOWS:
            os.startfile(url)  # noqa: S606 — Windows shell open
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception as exc:
        print("could not open %s: %r" % (url, exc))


# ── Tray ────────────────────────────────────────────────────────────

def run_tray(state: HelperState, pump: threading.Thread) -> int:
    """pystray loop. Returns non-zero when the tray can't be created, so
    the caller can fall back to plain console mode."""
    try:
        import pystray  # noqa: PLC0415 — optional
        from PIL import Image, ImageDraw  # noqa: PLC0415 — optional
    except ImportError:
        return 1

    def icon_image(connected: bool):
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        body = (139, 195, 74, 255) if connected else (66, 165, 245, 255)
        # A rounded pad silhouette: readable at 16 px, which is all the
        # tray ever shows.
        draw.rounded_rectangle([6, 20, 58, 48], radius=12, fill=body)
        draw.ellipse([14, 28, 26, 40], fill=(26, 26, 46, 255))
        draw.ellipse([38, 28, 50, 40], fill=(26, 26, 46, 255))
        return img

    tray: dict = {}

    def on_quit(icon, _item):
        icon.visible = False
        icon.stop()

    def menu():
        items = [
            pystray.MenuItem(state.summary(), None, enabled=False),
            pystray.MenuItem("Input: " + state.backend, None, enabled=False),
        ]
        if state.driver_missing:
            items += [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "⚠ No gamepad driver — games see no controller",
                    None, enabled=False),
            ]
            if state.driver_install_started:
                items.append(pystray.MenuItem(
                    state.driver_note or "Installing driver…",
                    None, enabled=False))
            else:
                items.append(pystray.MenuItem(
                    "Install gamepad driver…", lambda _i, _t: _install(state)))
        items.append(pystray.Menu.SEPARATOR)
        if log_dir():
            items.append(pystray.MenuItem(
                "Open log folder", lambda _i, _t: _open_url(log_dir())))
        if IS_WINDOWS and getattr(sys, "frozen", False):
            items.append(pystray.MenuItem(
                "Start with Windows",
                lambda _i, _t: set_autostart(not autostart_enabled()),
                checked=lambda _i: autostart_enabled()))
        # Version in the menu, not only in the log: the first question on
        # any bug report is which build the person is running, and the log
        # folder is two clicks further away than this line.
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("Version %s" % __version__, None,
                                   enabled=False),
                  pystray.MenuItem("Quit", on_quit)]
        return pystray.Menu(*items)

    icon = pystray.Icon(APP_NAME, icon_image(False), APP_NAME, menu=menu())
    tray["icon"] = icon

    def refresh() -> None:
        """Repaint title/icon when the session state changes. pystray has
        no push channel, so poll the state the log pump maintains."""
        last = None
        while pump.is_alive():
            now = (state.connected_to, state.backend,
                   state.driver_missing, state.driver_note)
            if now != last:
                last = now
                try:
                    icon.icon = icon_image(state.connected_to is not None)
                    icon.title = "%s — %s" % (APP_NAME, state.summary())
                    icon.menu = menu()
                    icon.update_menu()
                except Exception:
                    pass
            threading.Event().wait(1.0)

    def on_setup(tray_icon) -> None:
        """Runs once the tray loop is live.

        A double-click that opens no window reads as "nothing happened" —
        the first real-world report of this app was exactly that. Worse on
        Windows 11, which hides new tray icons behind the overflow arrow by
        default, so there is nothing to notice at all. A one-shot
        notification is the cheapest honest answer: it says the app is
        running and where it lives. `notify` is unsupported on some
        backends, so a failure here must never take the tray down with it.
        """
        tray_icon.visible = True
        try:
            tray_icon.notify(
                "Running in the system tray (click the ^ arrow if you don't "
                "see it). Your phone can find this computer now.",
                APP_NAME)
        except Exception:
            pass

    threading.Thread(target=refresh, daemon=True).start()
    icon.run(setup=on_setup)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Helper-only switches must not reach the engine's argparse, which
    # exits the process on an unknown flag.
    tray_disabled = "--no-tray" in argv
    engine_argv = [a for a in argv if a != "--no-tray"]

    state = HelperState()
    lines: queue.Queue = queue.Queue(maxsize=2000)

    # Frozen GUI builds have no stdout at all; _Tee tolerates None. The log
    # file is the only place a packaged run leaves a trace.
    log = _open_log()
    sys.stdout = _Tee(sys.stdout, lines, log)
    sys.stderr = _Tee(sys.stderr, lines, log)
    print("── %s %s starting (pid %d) ──" % (APP_NAME, __version__, os.getpid()))

    def pump_logs() -> None:
        while True:
            state.absorb(lines.get())

    threading.Thread(target=pump_logs, daemon=True).start()

    # Before the engine: it chooses its injection backend once, so a driver
    # installed now is used immediately instead of after a restart.
    first_run_driver_flow(state)
    first_run_accessibility_flow()

    engine_thread = threading.Thread(
        target=lambda: engine.main(engine_argv), daemon=True)
    engine_thread.start()

    if tray_disabled:
        engine_thread.join()
        return 0

    if run_tray(state, engine_thread) != 0:
        print("pystray not available — running in console mode. "
              "Ctrl+C to stop.")
        engine_thread.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
