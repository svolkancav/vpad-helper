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


def install_driver() -> None:
    """Run the bundled installer (UAC prompt), or open the download page."""
    installer = bundled_vigem_installer()
    if installer and IS_WINDOWS:
        try:
            if installer.lower().endswith(".msi"):
                subprocess.Popen(["msiexec", "/i", installer])
            else:
                subprocess.Popen([installer])
            return
        except Exception as exc:
            print("driver installer failed: %r" % (exc,))
    _open_url(VIGEM_DOWNLOAD_URL)


def _install(state: "HelperState") -> None:
    state.driver_install_started = True
    install_driver()


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
                    "Driver installed? Quit and reopen V-Pad Helper",
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
        items += [pystray.Menu.SEPARATOR,
                  pystray.MenuItem("Quit", on_quit)]
        return pystray.Menu(*items)

    icon = pystray.Icon(APP_NAME, icon_image(False), APP_NAME, menu=menu())
    tray["icon"] = icon

    def refresh() -> None:
        """Repaint title/icon when the session state changes. pystray has
        no push channel, so poll the state the log pump maintains."""
        last = None
        while pump.is_alive():
            now = (state.connected_to, state.backend, state.driver_missing)
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
    print("── %s starting (pid %d) ──" % (APP_NAME, os.getpid()))

    def pump_logs() -> None:
        while True:
            state.absorb(lines.get())

    threading.Thread(target=pump_logs, daemon=True).start()

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
