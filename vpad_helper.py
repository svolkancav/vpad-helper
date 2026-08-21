#!/usr/bin/env python3
"""
V-Pad Helper — the tray app end users install on their computer.

Wraps the engine (`android-vpad-helper/host/vpad_host.py`, imported, not
duplicated) so a non-technical user never sees a terminal: double-click,
an icon appears in the tray/menu bar, the phone finds the computer by
itself and pairs by scanning a code from the tray menu.

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
    • "Show pairing code…" — opens the code in a window
    • "New pairing code" — burns the open ticket, mints another
    • "Start with Windows" — registry Run key toggle
    • Quit

On a first run — nothing paired yet — the code is shown without being
asked for. A new user cannot be expected to find a menu behind Windows
11's tray overflow arrow.

The pairing items are the reason this file cannot be a thin wrapper: the
engine prints its QR as ANSI art to a console that a windowed .exe does
not have. Without a window there is no way for a phone to pair at all,
which is the single defect that made the published 0.2.1 build unusable.

Without `pystray` installed it degrades to console mode and keeps working,
so a developer running it from a checkout gets the same engine and logs.

Build the Windows one-file .exe from CI (see
`.github/workflows/build.yml`), or locally on a Windows box:

    pip install -r requirements.txt pyinstaller
    pyinstaller vpad-helper.spec
"""
from __future__ import annotations

import gc
import io
import os
import queue
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
# Motor bir alt klasörde yaşıyor. Donmuş pakette PyInstaller onu köke
# düzleştiriyor (bkz. .spec `pathex`), kaynaktan koşarken bu satır gerekli.
sys.path.insert(0, os.path.join(_ROOT, "android-vpad-helper", "host"))

# MOTOR: `vpad_host`, `vpad_daemon` DEĞİL.
#
# İkisi de aynı teli konuşuyor ama `vpad_daemon` eşleşme bilmiyor —
# `--pair` yok, CHALLENGE yok, cihaz defteri yok. Telefondaki 2.x sürümü
# eşleşmeli host bekliyor; eşleşmesiz daemon'la paketlenmiş bir companion
# **hiçbir telefonla bağlanamaz.** `vpad_host` üstkümesi: eşleşme, çoklu
# oyuncu ve oturum sürekliliği doğuştan.
import vpad_host as engine  # noqa: E402 — path must be set first

APP_NAME = "V-Pad Helper"

# Single source of truth for the version. `vpad-helper.spec` parses it into
# the .exe's Windows version resource, CI passes it to `installer.iss` for
# the Add/Remove Programs entry, and the tray menu shows it.
#
# On a `v*` tag build CI asserts that this equals the tag and fails the
# build otherwise — bumping the tag alone would ship an .exe whose
# properties still claimed the previous release.
__version__ = "0.4.0"

PUBLISHER = "V-Pad"
HOMEPAGE = "https://vpadcontroller.com/"
VIGEM_DOWNLOAD_URL = "https://github.com/nefarius/ViGEmBus/releases/latest"

IS_WINDOWS = sys.platform == "win32"


def _claim_taskbar_identity() -> None:
    """Tell Windows this process is V-Pad Helper, not its host interpreter.

    Without an explicit AppUserModelID the shell groups a window under the
    executable that created it and paints THAT executable's icon on the
    taskbar button — so a source run shows Python's logo (seen 2026-08-20)
    and a packaged run can still merge with an unrelated pinned entry. The
    window's own `iconbitmap` does not reach the taskbar; this does.

    Cosmetic, and deliberately silent on failure: an old shell without the
    API must not stop the app from starting.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes                       # noqa: PLC0415 — Windows only
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "VPad.Helper")
    except Exception:
        pass


_claim_taskbar_identity()


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
        # O an bağlı HER oyuncu: (slot, ad, ip). Motorun `@status peers=`
        # satırından; `connected_to` tek ada indirgenmiş eski görünümü.
        self.peers: list[tuple[int, str, str]] = []
        self.addresses: list[str] = []
        self.driver_missing = False
        # The injector is chosen once at startup, so installing the driver
        # mid-session changes nothing until the app is restarted. Saying so
        # is the difference between "it works now" and a silent dead end.
        self.driver_install_started = False
        self.driver_note = ""
        # Açık kayıt biletinin `vpad://…` adresi. Tepsi menüsündeki
        # "Eşleştirme kodunu göster" bunu QR'a çeviriyor — terminali
        # olmayan kullanıcı kareyi başka türlü göremez.
        self.pairing_payload: str | None = None
        # Aynı bilete bağlı 6 haneli elle giriş kodu. Kamerası olmayan
        # cihazın tek girişi, o yüzden QR'la eşit yer alıyor.
        self.pairing_code: str | None = None
        # Host'un defterindeki cihaz sayısı; None = motor henüz söylemedi.
        self.known_devices: int | None = None
        # İlk kurulum: defter boş, yani bu bilgisayara hiçbir telefon
        # kayıtlı değil. Tepsi bunu bir kez tüketip QR'ı kendiliğinden
        # açıyor — yeni kullanıcının tepsideki ok tuşunun arkasına saklanmış
        # bir menüyü kendi başına bulması beklenemez.
        self.offer_pairing = False

    # Motorun bastığı `@status anahtar=değer` satırları. İnsan satırları
    # Türkçe ve çevrilebilir; bunlar sabit. Aşağıdaki eski İngilizce
    # ayrıştırma, `vpad_daemon` ile koşulduğunda çalışsın diye duruyor.
    _STATUS = "@status "

    # Motor mDNS TXT etiketini yolluyor ('vigem' / 'test'), çünkü telefon
    # da aynı etiketi okuyor. Kullanıcıya gösterilecek metin buranın işi:
    # tepsi İngilizce, motorun günlüğü Türkçe, ve "Input: test" hiç kimseye
    # bir şey anlatmıyor.
    _INJECT_LABEL = {
        "vigem": "Virtual gamepad (ViGEmBus)",
        "test": "None — games will see no controller",
    }

    def absorb(self, line: str) -> None:
        if line.startswith(self._STATUS):
            key, _, value = line[len(self._STATUS):].partition("=")
            if key == "inject":
                self.backend = self._INJECT_LABEL.get(value, value)
                # Motorun KENDİ seçimi, sürücünün var olup olmadığına dair
                # en güvenilir kanıt: enjektör açılışta bir kez seçiliyor
                # ve `vigem` yerine `test`e düşmesinin tek sebebi ViGEmBus'a
                # ulaşamaması. Sürücü tepsiden kurulduğunda bu satır
                # yeniden başlatılana kadar `test` kalıyor — uyarı da öyle,
                # ki doğrusu bu: kurulum bu oturumu değiştirmiyor.
                if IS_WINDOWS:
                    self.driver_missing = value != "vigem"
            elif key == "addrs":
                self.addresses = [a for a in value.split(",") if a]
            elif key == "devices":
                try:
                    self.known_devices = int(value)
                except ValueError:
                    self.known_devices = None
            elif key == "code":
                self.pairing_code = value or None
            elif key == "pairing":
                first = self.pairing_payload is None
                self.pairing_payload = value
                # Yalnız ilk bilette ve yalnız defter boşken. `devices`
                # satırı `pairing`'den ÖNCE basılıyor, o yüzden burada
                # zaten okunmuş oluyor.
                if first and self.known_devices == 0:
                    self.offer_pairing = True
            elif key == "peer":
                # Eski tek-ad satırı; `peers` varken onu ezmesin — listeden
                # türetilen değer daha doğru (P2 ayrılınca P1 hâlâ bağlı).
                if not self.peers:
                    self.connected_to = value or None
            elif key == "peers":
                self.peers = self._parse_peers(value)
                self.connected_to = self.peers[0][1] if self.peers else None
            return
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

    @staticmethod
    def _parse_peers(value: str) -> list[tuple[int, str, str]]:
        """`1:V-Pad @ 192.168.1.134;2:Phone @ 10.0.0.7` → [(1, ad, ip), …]."""
        out: list[tuple[int, str, str]] = []
        for item in value.split(";"):
            item = item.strip()
            if not item:
                continue
            slot, _, label = item.partition(":")
            name, _, ip = label.rpartition(" @ ")
            try:
                out.append((int(slot), (name or label).strip() or "phone", ip.strip()))
            except ValueError:
                continue
        return out

    def summary(self) -> str:
        if self.peers:
            return "Connected: " + " · ".join(
                "P%d %s%s" % (slot, name, " (%s)" % ip if ip else "")
                for slot, name, ip in self.peers)
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


# ── Single instance ─────────────────────────────────────────────────

# Whatever claims the instance has to stay referenced for the life of the
# process. A garbage-collected mutex handle or a closed file releases the
# claim, and the guard then silently stops guarding.
_instance_claim: list = []

_MUTEX_NAME = "Local\\VPadHelperSingleInstance"


def claim_single_instance() -> bool:
    """True if this is the only helper running, False if one already is.

    Two copies are easy to end up with — "Start with Windows" is on and
    the user also opens the shortcut — and the result is not two tray
    icons doing the same harmless thing. Both engines start, both log
    success, but only **one** is reachable: mDNS carries a single record
    per instance name, so the phone talks to whichever registered first
    while the other sits invisible holding a virtual gamepad. The tray
    icon the user just clicked is then not the one the phone is talking
    to, and quitting it changes nothing.

    zeroconf's own conflict probe does not cover this. It looks the name
    up in its cache, and a second process on the same host does not
    reliably see the first one's multicast — measured: two daemons with
    an identical --name both started, no exception, one record on the
    wire.

    When the answer cannot be determined this returns True. A guard that
    refuses to start the app is worse than the duplicate it prevents.
    """
    if IS_WINDOWS:
        try:
            import ctypes  # noqa: PLC0415 — Windows-only
            error_already_exists = 183
            kernel32 = ctypes.windll.kernel32
            # `Local\` scopes the name to the logon session, which is what
            # we want: two users switched on one PC each get a helper.
            handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
            if not handle:
                return True
            if kernel32.GetLastError() == error_already_exists:
                return False
            _instance_claim.append(handle)
            return True
        except Exception as exc:
            print("single-instance check failed: %r" % (exc,))
            return True

    try:
        import fcntl  # noqa: PLC0415 — POSIX only
    except ImportError:
        return True
    folder = log_dir()
    if not folder:
        return True
    try:
        handle = open(os.path.join(folder, "instance.lock"), "a+")
    except OSError:
        return True
    try:
        # The lock is released by the OS when this process dies, however
        # it dies — no stale lock file to clean up after a crash.
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _instance_claim.append(handle)
    return True


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


def close_splash(text: str | None = None) -> None:
    """Dismiss the PyInstaller splash — or update its status line.

    Only exists inside a splash build; a source run and the macOS bundle
    have no `pyi_splash` and this is a no-op.

    **Closing is not optional.** The splash is a separate always-on-top Tk
    window that lives until it is told to go, and a tray app never exits
    on its own — leaving it open would park a borderless rectangle over
    the user's screen for the whole session. So it is closed from every
    path that ends in UI: the tray becoming visible, console mode, and
    before any dialog, which would otherwise open *behind* it.
    """
    try:
        import pyi_splash  # noqa: PLC0415 — only exists in a splash build
    except Exception:
        return
    try:
        if text is not None and pyi_splash.is_alive():
            pyi_splash.update_text(text)
            return
        pyi_splash.close()
    except Exception:
        pass


def tell(text: str, warn: bool = False) -> None:
    # A modal dialog behind an always-on-top splash is a hang, as far as
    # the user can tell: the app is waiting for a click on a window they
    # cannot see.
    close_splash()
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


def qr_image(payload: str, px: int = 300):
    """The bare QR as a PIL image, or None if `qrcode` is unavailable.

    NEAREST on resize, deliberately: a QR is a bitmap of hard squares and
    a smooth filter greys their edges, which is what makes a phone camera
    hunt for focus.
    """
    try:
        import qrcode                       # noqa: PLC0415 — optional
        from PIL import Image               # noqa: PLC0415
    except Exception:
        return None
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                         box_size=10, border=2)
    code.add_data(payload)
    code.make(fit=True)
    img = code.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((px, px), Image.NEAREST)


def _ui_font(size: int, bold: bool = False):
    """A system font, or PIL's bitmap default. No font file is shipped."""
    from PIL import ImageFont               # noqa: PLC0415
    names = (("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold
             else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf",
                   "/System/Library/Fonts/Helvetica.ttc"))
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def pairing_card(payload: str) -> str | None:
    """Fallback: draw the ticket to a PNG and return its path.

    Used only when the in-app window cannot open — no Tk in the build, or
    a display that refuses one. Handing the user a file to open in their
    image viewer is a poor substitute for a window, so it is a fallback
    and not the design.

    The folder is emptied on every call: the ticket is SINGLE-USE and the
    host mints a new one the moment a phone enrols. A stale square left on
    disk puts the user in a "but I scanned it" loop.
    """
    qr = qr_image(payload, 520)
    if qr is None:
        return None
    try:
        from PIL import Image, ImageDraw    # noqa: PLC0415
    except Exception:
        return None
    folder = log_dir()
    if not folder:
        return None
    folder = os.path.join(folder, "pairing")
    try:
        os.makedirs(folder, exist_ok=True)
        for stale in os.listdir(folder):
            try:
                os.remove(os.path.join(folder, stale))
            except OSError:
                pass       # a viewer may hold it open; not worth failing over
    except Exception:
        return None

    card = Image.new("RGB", (640, 760), "white")
    card.paste(qr, ((640 - 520) // 2, 120))
    draw = ImageDraw.Draw(card)

    def center(text: str, y: int, size: int, fill=(20, 20, 30)) -> None:
        f = _ui_font(size)
        width = draw.textbbox((0, 0), text, font=f)[2]
        draw.text(((640 - width) // 2, y), text, font=f, fill=fill)

    center(APP_NAME, 34, 34)
    center("Scan this code with the V-Pad app on your phone", 82, 17,
           (90, 90, 105))
    center("Works once. Your phone is remembered after this -", 660, 15,
           (120, 120, 135))
    center("no code needed next time.", 682, 15, (120, 120, 135))
    center("Each phone needs its own code.", 708, 15, (20, 20, 30))
    center(payload.split("?")[0].replace("vpad://", ""), 733, 13,
           (150, 150, 165))

    path = os.path.join(folder, "vpad-pairing.png")
    try:
        card.save(path)
    except Exception:
        return None
    return path


def _bundled(name: str) -> str:
    """Path to a file shipped with the app (or inside the one-file bundle).

    PyInstaller flattens data files into the bundle root, so a name that
    lives under `brand/` in the checkout is at the top level once frozen.
    Both layouts are tried rather than branching on `sys.frozen`, which
    would make a source run and a packaged run take different code paths
    for no reason.
    """
    roots = [getattr(sys, "_MEIPASS", None),
             os.path.dirname(os.path.abspath(__file__))]
    for root in roots:
        if not root:
            continue
        for candidate in (os.path.join(root, name),
                          os.path.join(root, "brand", name)):
            if os.path.exists(candidate):
                return candidate
    return os.path.join(roots[-1], name)


# -- Pairing window --------------------------------------------------
#
# The code has to appear IN the app. Writing a PNG and handing it to the
# system image viewer was the first attempt and it is not a real answer:
# the user gets a stray file, a foreign window, no way to tell a live code
# from a spent one, and no sign when the phone finally pairs.
#
# Tk is affordable here only because the splash screen already puts
# Tcl/Tk in the bundle; the marginal cost is `_tkinter.pyd` plus the
# tkinter package.
#
# THREADING: Tcl is thread-sensitive - an interpreter may only be touched
# by the thread that created it. pystray owns the main thread's message
# loop, so the window gets a thread of its own and every Tk call, the
# `after` polling included, happens on it. Nothing outside reaches in: the
# window READS `state` (plain attribute reads, atomic in CPython) and no
# other thread ever calls into Tk.

_window_lock = threading.Lock()
_window_open = False


def show_pairing(state: "HelperState", on_new_code=None) -> None:
    """Open the pairing window. One at a time; a second call is a no-op.

    Falls back to the PNG when Tk is missing, because a clumsy code beats
    no code: a phone that is not already enrolled has no other way in.
    """
    global _window_open
    if not state.pairing_payload:
        return
    with _window_lock:
        if _window_open:
            return
        _window_open = True

    def run() -> None:
        global _window_open
        try:
            _pairing_window(state, on_new_code)
        except Exception as exc:
            print("pairing window failed (%r) - falling back to an image"
                  % (exc,))
            path = pairing_card(state.pairing_payload or "")
            if path:
                _open_url(path)
        finally:
            with _window_lock:
                _window_open = False

    threading.Thread(target=run, daemon=True).start()


def _pairing_window(state: "HelperState", on_new_code) -> None:
    """The window itself. Runs start to finish on its own thread.

    Fixed size on purpose. Packed to fit, the window was exactly as wide as
    the QR and looked like a debug dialog; a pairing screen is the first
    thing a new user sees of this product. The size is also what lets the
    "connected" view replace the code view without the window jumping.
    """
    import base64                           # noqa: PLC0415
    import io as _io                        # noqa: PLC0415
    import tkinter as tk                    # noqa: PLC0415

    NAVY, PANEL, TEXT, MUTED, OK, FAINT = ("#0b1c3f", "#122a5c", "#ffffff",
                                           "#a8bad6", "#4ade80", "#6d84ad")
    # Two more tones, both from the same blue: the primary button's accent
    # and a hairline for outlined surfaces. Nothing else enters the palette.
    ACCENT, ACCENT_HOVER, LINE = "#2f6fe4", "#3b7cf0", "#1e3a70"
    # 8-pt rhythm: 16 at the edges, 24 between sections, 8 inside a group.
    # 30 px taller than before to make room for the connected strip without
    # squeezing the code's quiet zone.
    W, H, QR_PX = 760, 530, 256
    F = "Segoe UI"

    root = tk.Tk()
    root.title("%s - Pair your phone" % APP_NAME)
    root.configure(bg=NAVY)
    root.geometry("%dx%d" % (W, H))
    root.resizable(False, False)
    # Title-bar icon. Cosmetic, so a failure must not stop the window from
    # opening — but it MUST be visible in the log. The previous
    # `except Exception: pass` hid a real packaging bug for weeks: the .ico
    # was baked into the exe as a Windows resource but never shipped as a
    # file, so this call raised on every packaged run and the window kept
    # Tk's default feather. Silence is what made it survive.
    _icon = _bundled("vpad-helper.ico")
    if not os.path.exists(_icon):
        print("window: icon file missing at %s — title bar will use the "
              "Tk default (check the spec's `datas`)" % _icon)
    else:
        try:
            # `default=`: applies to this window AND every toplevel this
            # process opens later, instead of only the one in hand.
            root.iconbitmap(default=_icon)
        except Exception as exc:            # cosmetic only, but say so
            print("window: iconbitmap failed (%s: %s)" % (type(exc).__name__, exc))

    tk.Label(root, text=APP_NAME, bg=NAVY, fg=FAINT,
             font=(F, 9), anchor="w").pack(fill="x", padx=24, pady=(16, 0))

    # ── connected strip ──
    #
    # One quiet line under the title, only while something is connected:
    # "● P1  V-Pad · 192.168.1.134". The code view below keeps showing the
    # NEXT phone's code, so a second player can join without the first one's
    # status disappearing — the "is my phone connected?" question answered
    # without hunting for the tray tooltip (owner brief 2026-08-20).
    # Packed before `stack` so it keeps its place; hidden with pack_forget
    # when the list empties, and the stack takes the room back.
    # Outlined, not filled: a hairline on the window's own navy reads as a
    # status line; a solid block would compete with the QR for attention.
    strip = tk.Frame(root, bg=NAVY, padx=14, pady=7,
                     highlightthickness=1, highlightbackground=LINE)
    strip_items: list = []

    def refresh_strip(peers) -> None:
        for w in strip_items:
            w.destroy()
        strip_items.clear()
        if not peers:
            strip.pack_forget()
            return
        # Eyebrow: tracked capitals, the quietest element on the line.
        eyebrow = tk.Label(strip, text="C O N N E C T E D", bg=NAVY, fg=FAINT,
                           font=(F, 7, "bold"))
        eyebrow.pack(side="left", padx=(0, 14))
        strip_items.append(eyebrow)
        # Identity only — slot and name. Four players have to share one
        # quiet line, so the address stays in the tray tooltip and the log;
        # long phone names are clipped rather than allowed to push P4 off
        # the edge.
        for slot, name, _ip in peers:
            shown_name = name if len(name) <= 14 else name[:13] + "…"
            cell = tk.Frame(strip, bg=NAVY)
            tk.Label(cell, text="●", bg=NAVY, fg=OK,
                     font=(F, 7)).pack(side="left", padx=(0, 6))
            tk.Label(cell, text="P%d" % slot, bg=NAVY, fg=MUTED,
                     font=(F, 9, "bold")).pack(side="left", padx=(0, 5))
            tk.Label(cell, text=shown_name, bg=NAVY, fg=TEXT,
                     font=(F, 9)).pack(side="left")
            cell.pack(side="left", padx=(0, 16))
            strip_items.append(cell)
        # Same left edge as the headline and the code below.
        strip.pack(fill="x", padx=24, pady=(14, 0), before=stack)

    stack = tk.Frame(root, bg=NAVY)
    stack.pack(fill="both", expand=True)

    # ── code view: two columns ──
    #
    # Landscape, not portrait (owner, 2026-08-20): the old single column
    # needed 720 px of height and still clipped its own footer once the
    # connected strip arrived. Side by side, the code gets the left half at
    # full size and every word of guidance sits beside it — nothing scrolls,
    # nothing falls below the fold on a 768-px laptop.
    code = tk.Frame(stack, bg=NAVY)
    left = tk.Frame(code, bg=NAVY)
    left.pack(side="left", padx=(24, 0), pady=(20, 0), anchor="n")
    right = tk.Frame(code, bg=NAVY)
    right.pack(side="left", fill="both", expand=True, padx=(28, 24),
               pady=(20, 0), anchor="n")

    # White mat around the code: the quiet zone is part of the symbol, and
    # a QR flush against a dark background is measurably harder to read.
    mat = tk.Frame(left, bg="white", padx=14, pady=14)
    mat.pack()
    canvas = tk.Label(mat, bg="white")
    canvas.pack()
    addr = tk.Label(left, text="", bg=NAVY, fg=FAINT, font=(F, 8))
    addr.pack(pady=(8, 0))

    head = tk.Label(right, text="Scan this code with\nV-Pad on your phone",
                    bg=NAVY, fg=TEXT, font=(F, 14, "bold"), justify="left",
                    anchor="w")
    head.pack(fill="x")
    tk.Label(right, text="In the app: choose Wi-Fi, then Scan QR",
             bg=NAVY, fg=MUTED, font=(F, 9), anchor="w").pack(fill="x", pady=(6, 0))
    note = tk.Label(right, text="", bg=NAVY, fg=MUTED, font=(F, 9),
                    justify="left", anchor="w", wraplength=330)
    note.pack(fill="x", pady=(8, 0))

    # ── manual entry ──
    #
    # Not a fallback, an equal door. Android's QR scanner is an optional
    # Google Play services module: a phone without Play services at all
    # (Huawei post-2019, AOSP builds) can never download it, and a phone
    # with a broken camera never gets there either. Before this the app
    # told those users to "connect to the internet and try again" — a
    # promise that would never come true, with no other way in.
    #
    # Digits only, and read off a screen into a phone: no letters to
    # confuse (O/0, l/1), no keyboard layout to fight.
    tk.Label(right, text="No camera? Type this code in the app instead:",
             bg=NAVY, fg=TEXT, font=(F, 9, "bold"), anchor="w").pack(
        fill="x", pady=(24, 8))
    digits = tk.Label(right, text="", bg=PANEL, fg=TEXT,
                      font=("Consolas", 22, "bold"), padx=22, pady=8)
    digits.pack(anchor="w")

    # The ticket is single-use and the host mints the next one the moment a
    # phone enrols, so a second phone cannot reuse what the first one used —
    # and nothing on screen said so. A user with two phones would try the
    # same square twice and read the failure as a broken app.
    tk.Label(right, text="Each phone needs its own code.",
             bg=NAVY, fg=MUTED, font=(F, 9), anchor="w").pack(fill="x", pady=(8, 0))
    code.pack(fill="both", expand=True)

    # ── connected view ──
    done = tk.Frame(stack, bg=NAVY)
    # An inner frame packed with expand and no fill centres itself in
    # whatever space is left. The window height is fixed and this view is
    # shorter than the code view, so without it everything hugs the top.
    done_mid = tk.Frame(done, bg=NAVY)
    done_mid.pack(expand=True)
    tk.Label(done_mid, text="\u2713", bg=NAVY, fg=OK,
             font=(F, 56)).pack(pady=(0, 6))
    done_head = tk.Label(done_mid, text="", bg=NAVY, fg=OK,
                         font=(F, 15, "bold"))
    done_head.pack()
    tk.Label(done_mid, text="You can start playing.", bg=NAVY, fg=TEXT,
             font=(F, 10)).pack(pady=(6, 26))
    tk.Label(done_mid, text="This computer remembers it \u2014\n"
                            "no code needed next time.",
             bg=NAVY, fg=MUTED, font=(F, 9), justify="center").pack()
    tk.Label(done_mid, text="Adding another phone?\n"
                            "Press New code \u2014 each phone needs its own.",
             bg=NAVY, fg=TEXT, font=(F, 9, "bold"),
             justify="center").pack(pady=(20, 0))

    # Tk 8.6 reads PNG natively, so PIL.ImageTk - and with it a second
    # tkinter dependency in the bundle - is not needed. Declared up here
    # because `teardown` has to empty it.
    held: dict = {}
    timers: dict = {}
    # Which peer the "connected" view has already announced. Without it,
    # pressing New code would bounce straight back to that view: the phone
    # is still connected, so the condition that opened it is still true.
    # Seeded with whoever is ALREADY connected when the window opens: that
    # phone is on the strip, and the code below is for the next one — only a
    # fresh connection while the window is up earns the big tick.
    announced: dict = {"peer": state.connected_to}

    # Footer: what closing does, then the actions. The reassurance is here
    # because the button alone could not carry it — "Close" read as "stop
    # the helper" to the first person who used it (owner, 2026-08-20) and
    # they went looking for a way to start it again. Nothing stops: the
    # engine lives in the tray, and a phone that is playing keeps playing.
    #
    # `side="bottom"` and `before=stack`: the footer claims its space
    # before the code view does, so nothing can ever push the buttons off
    # the window.
    footer = tk.Frame(root, bg=NAVY)
    footer.pack(side="bottom", fill="x", padx=24, pady=(0, 18), before=stack)
    tk.Label(footer,
             text="%s keeps running in the %s — your phone stays connected."
                  % (APP_NAME, "system tray" if IS_WINDOWS else "menu bar"),
             bg=NAVY, fg=FAINT, font=(F, 8), anchor="w").pack(
        side="left", pady=(8, 0))
    bar = tk.Frame(footer, bg=NAVY)
    bar.pack(side="right")

    def button(label, command, primary=False):
        # One accent, one outline: the eye lands on "New code" first and
        # "Close" stays available without shouting.
        if primary:
            return tk.Button(bar, text=label, command=command, width=13,
                             relief="flat", bg=ACCENT, fg=TEXT,
                             activebackground=ACCENT_HOVER, activeforeground=TEXT,
                             borderwidth=0, cursor="hand2", pady=6,
                             font=(F, 9, "bold"))
        return tk.Button(bar, text=label, command=command, width=13,
                         relief="flat", bg=NAVY, fg=MUTED,
                         activebackground=PANEL, activeforeground=TEXT,
                         borderwidth=0, highlightthickness=1,
                         highlightbackground=LINE, cursor="hand2", pady=6,
                         font=(F, 9))

    def teardown() -> None:
        """Close the window WITHOUT tripping Tcl_AsyncDelete.

        `tk.PhotoImage.__del__` calls back into the interpreter. If the last
        Python reference to one outlives `root`, that call runs later, from
        whichever thread the collector happens to be on, against a dead
        interpreter - and Tcl aborts the whole process with "async handler
        deleted by the wrong thread". Measured, not theorised: it fired on
        the first run of this window.

        So the images are dropped here, on this thread, while the
        interpreter is still alive.
        """
        try:
            canvas.config(image="")
            held.clear()
        except Exception:
            pass
        root.destroy()

    def draw(payload: str) -> None:
        img = qr_image(payload, QR_PX)
        if img is None:
            note.config(text="Could not render the code.")
            return
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        photo = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        canvas.config(image=photo)
        held["photo"] = photo               # or Tk garbage-collects it away
        addr.config(text=payload.split("?")[0].replace("vpad://", ""))
        # Üçlü gruplama okumayı kolaylaştırıyor: kullanıcı bunu ekrandan
        # telefona aktarıyor. `@status code` `pairing`den ÖNCE basıldığı
        # için burada zaten güncel.
        raw = state.pairing_code or ""
        digits.config(text=f"{raw[:3]} {raw[3:]}" if len(raw) == 6 else raw)
        note.config(text="Works once. After this your phone is remembered — "
                         "no code needed next time.")

    def show_code() -> None:
        """Back to the code, cancelling the auto-close."""
        after_id = timers.pop("close", None)
        if after_id is not None:
            root.after_cancel(after_id)
        done.pack_forget()
        code.pack(fill="both", expand=True)
        draw(state.pairing_payload or "")

    def new_code() -> None:
        if on_new_code is not None:
            on_new_code()
        show_code()                         # the redraw follows the ticket

    # "Done", not "Close": this window is a step in a task, and the only
    # thing it closes is itself.
    button("Done", teardown).pack(side="left", padx=(0, 8))
    button("New code", new_code, primary=True).pack(side="left")
    # The X button must take the same path, or closing the window that way
    # becomes the one that crashes.
    root.protocol("WM_DELETE_WINDOW", teardown)

    shown = state.pairing_payload or ""
    draw(shown)

    root.update_idletasks()
    root.geometry("+%d+%d" % ((root.winfo_screenwidth() - W) // 2,
                              max(0, (root.winfo_screenheight() - H) // 2 - 30)))
    root.attributes("-topmost", True)
    root.after(1200, lambda: root.attributes("-topmost", False))

    strip_shown: list = [None]

    def poll() -> None:
        nonlocal shown
        peers = list(state.peers)
        if peers != strip_shown[0]:
            strip_shown[0] = peers
            refresh_strip(peers)
        peer = state.connected_to
        if peer and peer != announced["peer"]:
            # The question the window exists to answer: stop showing a code
            # that has already been spent, and say what to do next.
            announced["peer"] = peer
            done_head.config(text="%s is connected" % peer)
            code.pack_forget()
            done.pack(fill="both", expand=True)
            # Long enough to read the "another phone?" line, short enough
            # to get out of the way of someone with one phone.
            timers["close"] = root.after(9000, teardown)
        elif not peer:
            current = state.pairing_payload or ""
            if current and current != shown:
                shown = current
                draw(current)               # rotated; never show a dead one
        root.after(400, poll)

    root.after(400, poll)
    root.mainloop()

    # Tcl deletes its interpreter when the last reference to `root` goes,
    # and it aborts the process if that happens on a thread other than the
    # one that created it. `teardown()` already dropped the images, but the
    # nested functions above and this frame form reference CYCLES, and a
    # cycle is freed by the collector - which runs on whatever thread hits
    # the threshold, in practice the main one at shutdown. That is the
    # second half of the same Tcl_AsyncDelete abort, and it is why the
    # first fix alone was not enough.
    #
    # So the cycles are broken and collected right here, on this thread,
    # while it is still the owner.
    held.clear()
    timers.clear()
    head = note = addr = canvas = mat = code = done = done_head = None
    digits = None
    done_mid = None
    stack = bar = draw = poll = teardown = button = None
    show_code = new_code = root = None
    gc.collect()


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
        """The tray icon: the brand badge, plus a dot for the session.

        This used to be an abstract pad silhouette drawn in code. It was
        the crisper icon at 16 px and the wrong one — rendered next to the
        real badge it reads as a pair of goggles and carries no brand, so
        the one V-Pad icon a user sees all day was not a V-Pad icon.

        The badge is cropped above its wordmark, the same crop
        `make-icon.py` uses for the small .ico sizes: at tray size a
        wordmark is a grey smear, and dropping it is what keeps the pad and
        the antenna legible.

        Session state is brightness first, dot second. A dot large enough
        to see at 16 px is a quarter of the icon's width, and two of them —
        one per state — sat on top of the pad in both. Dimming the whole
        badge while waiting carries the state at any size without touching
        the artwork, and the green dot is then only ever drawn on the
        state where it means something.
        """
        size = 64
        art = None
        try:
            art = Image.open(_bundled("icongamepad.png")).convert("RGBA")
            # 0.75 = make-icon.py's WORDMARK_CUT, measured on the 512 px
            # source: the pad's lowest pixel is y=363, the wordmark's first
            # is y=405, and the crop falls in the gap.
            side = int(art.height * 0.75)
            left = (art.width - side) // 2
            art = art.crop((left, 0, left + side, side)).resize(
                (size, size), Image.LANCZOS)
        except Exception:
            art = None

        if art is None:
            # Artwork missing (a source checkout without it, a build that
            # dropped the data file). Better a plain mark than no icon.
            img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            body = (139, 195, 74, 255) if connected else (66, 165, 245, 255)
            draw.rounded_rectangle([6, 20, 58, 48], radius=12, fill=body)
            draw.ellipse([14, 28, 26, 40], fill=(26, 26, 46, 255))
            draw.ellipse([38, 28, 50, 40], fill=(26, 26, 46, 255))
            return img

        if not connected:
            from PIL import ImageEnhance  # noqa: PLC0415 — this branch only
            return ImageEnhance.Brightness(art).enhance(0.5)

        draw = ImageDraw.Draw(art)
        # Ringed in the badge's own navy so the dot reads against both the
        # artwork and whatever taskbar colour sits behind it.
        draw.ellipse([48, 48, 62, 62], fill=(11, 28, 63, 255))
        draw.ellipse([50, 50, 60, 60], fill=(74, 222, 128, 255))
        return art

    def _new_ticket(_icon=None, _item=None) -> None:
        """Burn the open ticket and mint another.

        Runs off the caller's thread: the tray menu callback runs on the
        main loop and the window's button callback runs on the window's Tk
        thread, and neither may block — a frozen menu or a frozen window is
        how a user decides an app has crashed.

        The window redraws itself when `@status pairing=` lands, so there
        is nothing to hand back.
        """
        threading.Thread(target=engine.request_new_ticket, daemon=True).start()

    def _show_pairing(_icon=None, _item=None) -> None:
        show_pairing(state, on_new_code=_new_ticket)

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
        # Eşleşme kalemleri sadece `--pair` açıkken (yani her zaman, ama
        # motor QR basana kadar payload boş) anlamlı.
        if state.pairing_payload:
            items += [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Show pairing code…", _show_pairing,
                                 default=True),
                pystray.MenuItem("New pairing code", _new_ticket),
            ]
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
            if state.offer_pairing:
                state.offer_pairing = False
                _show_pairing()
            now = (state.connected_to, state.backend,
                   state.driver_missing, state.driver_note,
                   bool(state.pairing_payload))
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

        # THE ENGINE IS GONE. Say so (2026-08-19 audit).
        #
        # `pump` is the ENGINE thread despite the name — `main` calls
        # `run_tray(state, engine_thread)`. So this loop ends exactly when
        # the bridge dies, and until now that was all that happened: this
        # thread returned, while `icon.run()` kept the tray alive forever.
        # The user was left with a live icon, a menu that opens, and a
        # pairing code that no longer means anything — with no phone able
        # to connect and nothing on screen admitting it. The only evidence
        # was a line in helper.log, which nobody reads unprompted.
        #
        # The engine ends for ordinary reasons too, not just crashes: an
        # mDNS name collision or a port that will not bind returns from
        # `main()` normally. Same silence, same support ticket.
        #
        # Quit is NOT this path — that unwinds the tray loop itself, so
        # this code does not run on a clean exit.
        try:
            icon.icon = icon_image(False)
            icon.title = "%s — stopped (restart required)" % APP_NAME
            icon.update_menu()
            icon.notify(
                "The connection service stopped. No phone can connect "
                "until you quit and start %s again." % APP_NAME, APP_NAME)
        except Exception:
            pass

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
        close_splash()
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

    # Ürün varsayılanları. Motorun CLI varsayılanları geliştirici içindir
    # (eşleşme kapalı, tek oyuncu); paketlenmiş uygulamada ikisi de yanlış:
    #
    #   --pair      Olmadan kapı herkese açık. Aynı Wi-Fi'daki HERHANGİ bir
    #               cihaz kumanda gönderebilir. Telefondaki 2.x da zaten
    #               eşleşmeli host bekliyor.
    #   --players 4 Sanal pad'ler tembel yaratılıyor (bkz. `PadSet`), yani
    #               tek telefonlu kullanıcıya maliyeti sıfır; buna karşılık
    #               dördüncü oyuncu bir bayrak aramak zorunda kalmıyor.
    #
    # Elle verilmişse dokunmuyoruz: `--no-tray` ile koşan geliştirici hâlâ
    # istediğini seçebilir.
    if "--pair" not in engine_argv:
        engine_argv.append("--pair")
    if not any(a == "--players" or a.startswith("--players=")
               for a in engine_argv):
        engine_argv += ["--players", "4"]

    # Backstop. Every path that reaches UI closes the splash itself; this
    # covers the ones that never reach any — an exception inside the tray
    # backend, a pystray version that never calls setup. Without it the
    # failure mode is an always-on-top rectangle the user cannot dismiss,
    # which is worse than the blank wait the splash exists to prevent.
    _splash_watchdog = threading.Timer(25.0, close_splash)
    _splash_watchdog.daemon = True   # Timer defaults to non-daemon, which
    _splash_watchdog.start()         # would hold the process open on Quit

    state = HelperState()
    lines: queue.Queue = queue.Queue(maxsize=2000)

    # Frozen GUI builds have no stdout at all; _Tee tolerates None. The log
    # file is the only place a packaged run leaves a trace.
    log = _open_log()
    sys.stdout = _Tee(sys.stdout, lines, log)
    sys.stderr = _Tee(sys.stderr, lines, log)
    print("── %s %s starting (pid %d) ──" % (APP_NAME, __version__, os.getpid()))

    if not claim_single_instance():
        print("another instance already holds the session — exiting")
        tell("V-Pad Helper is already running.\n\nIts icon is in the system "
             "tray — click the ^ arrow next to the clock if you can't see it.")
        return 0

    def pump_logs() -> None:
        while True:
            state.absorb(lines.get())

    threading.Thread(target=pump_logs, daemon=True).start()

    # Before the engine: it chooses its injection backend once, so a driver
    # installed now is used immediately instead of after a restart.
    first_run_driver_flow(state)
    first_run_accessibility_flow()

    close_splash("Starting the bridge…")
    engine_thread = threading.Thread(
        target=lambda: engine.main(engine_argv), daemon=True)
    engine_thread.start()

    if tray_disabled:
        _wait_for_engine(engine_thread)
        return 0

    if run_tray(state, engine_thread) != 0:
        print("pystray not available — running in console mode. "
              "Ctrl+C to stop.")
        _wait_for_engine(engine_thread)
        return 0

    # The tray loop returned, which means the user chose Quit.
    _stop_engine(engine_thread)
    return 0


def _wait_for_engine(engine_thread: threading.Thread) -> None:
    """Console mode: block until the engine ends or Ctrl+C arrives.

    The signal is delivered to *this* thread, not the engine's, so the
    engine's own KeyboardInterrupt handler never fires — it has to be
    told to unwind.
    """
    # No tray in this path, so nothing else will ever close the splash.
    close_splash()
    try:
        engine_thread.join()
    except KeyboardInterrupt:
        _stop_engine(engine_thread)


def _stop_engine(engine_thread: threading.Thread) -> None:
    """Unwind the engine so its Bonjour goodbye actually goes out.

    The engine runs in a daemon thread, and the threading docs are blunt
    about those: "Daemon threads are abruptly stopped at shutdown. Their
    resources … may not be released properly." The mDNS unregister lives
    in exactly such a `finally`, so simply returning from here used to
    leave the service advertised. A phone that had already cached the
    record then keeps listing this computer — RFC 6762 §10 recommends a
    75-minute TTL for these records, where §10.1's goodbye packet would
    have retired it in one second — and tapping it fails with no
    explanation on either end.

    Measured before the fix: a browsing client still listed the daemon
    30 s after it died.
    """
    engine.request_shutdown()
    engine_thread.join(timeout=5.0)
    if engine_thread.is_alive():
        # Not fatal: the process is about to exit anyway. Say so, because
        # a silent 5-second pause on Quit would otherwise look like a bug.
        print("engine did not stop within 5 s — exiting without a clean "
              "Bonjour withdrawal")


if __name__ == "__main__":
    sys.exit(main())
