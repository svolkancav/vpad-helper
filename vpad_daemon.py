#!/usr/bin/env python3
"""
V-Pad companion daemon — reference implementation (Python).

Speaks the wire protocol in `docs/companion-daemon.md` (proto_ver = 1)
and **injects** the received gamepad state into the host OS, so an iPhone
running V-Pad actually controls this computer.

This is the development / self-hosting daemon. The shipping daemon is
planned as a separate Rust repo (spec §7); this file exists so the iOS
bridge can be tested and used end-to-end today, and it doubles as an
executable spec for whoever writes that binary.

Injection backends
------------------
    vigem   Windows. Real virtual Xbox 360 pad via ViGEmBus, so games see
            a genuine XInput controller. Requires the ViGEmBus driver
            (https://github.com/nefarius/ViGEmBus) + `pip install vgamepad`.
    macos   macOS. Synthesizes keyboard + mouse events via CGEvent. macOS
            has no user-space virtual-HID path (DriverKit needs a
            months-long entitlement; kexts need SIP disabled), so a real
            virtual pad is out of reach — this maps the pad onto
            keyboard/mouse instead, which most Mac games and every browser
            game accept.
            ⚠️  Needs Accessibility permission: System Settings → Privacy
            & Security → Accessibility → enable your terminal app.
    log     Any OS. Decodes and prints frames, injects nothing. Same role
            as `tools/test-server.py`, kept here so `--inject log` is a
            valid answer when a backend is unavailable.

Default is `auto`: vigem on Windows (falls back to log with instructions
if vgamepad/ViGEmBus is missing), macos on macOS, log elsewhere.

macOS key map (`--inject macos`)
--------------------------------
    Left stick   W A S D              Hat / D-pad   arrow keys
    Right stick  mouse move           RT            left mouse button
    A  Space     B  Left Ctrl         LT            right mouse button
    X  E         Y  R
    L1 Q         R1 F
    L3 Left Shift  R3 C
    Select Tab     Start Return       Home  M

Requires:
    pip install zeroconf          # Bonjour publish (both backends)
    pip install vgamepad          # Windows vigem backend only

Usage:
    python3 vpad_daemon.py                    # auto backend
    python3 vpad_daemon.py --inject log -v    # decode only
    python3 vpad_daemon.py --name "Studio PC" --mouse-speed 9
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import dataclasses
import socket
import struct
import sys
import threading
from contextlib import closing
from datetime import datetime

try:
    from zeroconf import IPVersion, ServiceInfo, Zeroconf
except ImportError:
    print("ERROR: zeroconf not installed. Run: pip install zeroconf",
          file=sys.stderr)
    sys.exit(1)


# ── Protocol constants (mirror docs/companion-daemon.md §4) ─────────

PROTO_VER = 1
MAX_FRAME = 4096

T_HELLO = 0x01
T_HELLO_ACK = 0x10
T_REJECT = 0x11
T_REPORT = 0x02
T_MOUSE = 0x05
T_PING = 0x03
T_PONG = 0x12
T_BYE = 0x04

R_VERSION_MISMATCH = 0x01
R_IN_USE = 0x02
R_UNSUPPORTED_SKIN = 0x03
R_INTERNAL_ERROR = 0xFF

# RFC 6763 §7.2 caps the service-name label at 15 characters; the
# original `_universalgamepad` was 16 and zeroconf refuses to publish
# it. Renamed 2026-07-27 in lockstep with Info.plist + the Swift host.
SERVICE_TYPE = "_vpad-bridge._tcp.local."

# Byte [0] bits 0..7
BTN_A, BTN_B, BTN_X, BTN_Y = 0x01, 0x02, 0x04, 0x08
BTN_L1, BTN_R1, BTN_SELECT, BTN_START = 0x10, 0x20, 0x40, 0x80
# Byte [1] bits 0..2 (bits 4-7 are the hat nibble)
BTN_L3, BTN_R3, BTN_HOME = 0x01, 0x02, 0x04

HAT_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
BTN_LOW_NAMES = ["A", "B", "X", "Y", "L1", "R1", "Sel", "Sta"]
BTN_HIGH_NAMES = ["L3", "R3", "Home"]

# Hat direction → (up, right, down, left) booleans. 8 = released.
HAT_VECTORS = {
    0: (True, False, False, False),
    1: (True, True, False, False),
    2: (False, True, False, False),
    3: (False, True, True, False),
    4: (False, False, True, False),
    5: (False, False, True, True),
    6: (False, False, False, True),
    7: (True, False, False, True),
}


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ── Frame codec ─────────────────────────────────────────────────────

def encode_frame(msg_type: int, payload: bytes) -> bytes:
    """Pack `[u16 LE length-inclusive][u8 type][payload]`."""
    total = 3 + len(payload)
    if total > MAX_FRAME:
        raise ValueError(f"frame too large: {total} > {MAX_FRAME}")
    return struct.pack("<HB", total, msg_type) + payload


def encode_hello_ack(server_ver: int = PROTO_VER, accept: bool = True) -> bytes:
    return encode_frame(T_HELLO_ACK,
                        struct.pack("BB", server_ver, 1 if accept else 0))


def encode_reject(reason: int, msg: str = "") -> bytes:
    return encode_frame(T_REJECT, bytes([reason]) + msg.encode("utf-8"))


def encode_pong() -> bytes:
    return encode_frame(T_PONG, b"")


def encode_bye() -> bytes:
    return encode_frame(T_BYE, b"")


def frame_reader(sock: socket.socket):
    """Yield (msg_type, payload) as complete frames arrive.

    Buffers partial reads and handles several frames per recv chunk. TCP
    gives no message boundaries (spec §3).
    """
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed socket")
        buf.extend(chunk)

        while len(buf) >= 3:
            length = buf[0] | (buf[1] << 8)
            if length < 3:
                raise ConnectionError(f"undersize frame: length={length}")
            if length > MAX_FRAME:
                raise ConnectionError(f"oversize frame: length={length}")
            if len(buf) < length:
                break
            msg_type = buf[2]
            payload = bytes(buf[3:length])
            del buf[:length]
            yield msg_type, payload


def decode_hello(payload: bytes) -> tuple[int, str, str]:
    """Parse HELLO per spec §4.1."""
    if len(payload) < 2:
        raise ValueError("HELLO too short")
    proto_ver = payload[0]
    name_len = payload[1]
    if len(payload) < 2 + name_len + 1:
        raise ValueError("HELLO name truncated")
    name = payload[2:2 + name_len].decode("utf-8", errors="replace")
    skin_idx = 2 + name_len
    skin_len = payload[skin_idx]
    if len(payload) < skin_idx + 1 + skin_len:
        raise ValueError("HELLO skin truncated")
    skin = payload[skin_idx + 1:skin_idx + 1 + skin_len].decode(
        "utf-8", errors="replace")
    return proto_ver, name, skin


@dataclasses.dataclass
class Report:
    """One decoded REPORT frame (spec §4.4).

    Sticks and triggers are **unsigned 0..255**; sticks rest at 128. That
    is the Android HID report layout this wire mirrors byte-for-byte
    (signed→unsigned pivot, 2026-06-13). Y grows **downward** (screen
    convention) — backends targeting an XInput-style pad must negate it.
    """
    btn_low: int
    btn_high: int
    lx: int
    ly: int
    rx: int
    ry: int
    lt: int
    rt: int

    @property
    def hat(self) -> int:
        return (self.btn_high >> 4) & 0x0F

    def pressed(self, mask: int, high: bool = False) -> bool:
        return bool((self.btn_high if high else self.btn_low) & mask)

    def axis(self, value: int) -> float:
        """Unsigned 0..255 (center 128) → float -1.0..+1.0."""
        return max(-1.0, min(1.0, (value - 128) / 127.0))

    @classmethod
    def decode(cls, payload: bytes) -> "Report":
        if len(payload) != 8:
            raise ValueError(f"REPORT must be 8 bytes, got {len(payload)}")
        return cls(*struct.unpack("BBBBBBBB", payload))

    def pretty(self) -> str:
        low = [n for i, n in enumerate(BTN_LOW_NAMES) if self.btn_low & (1 << i)]
        high = [n for i, n in enumerate(BTN_HIGH_NAMES)
                if self.btn_high & (1 << i)]
        names = "+".join(low + high) if (low or high) else "—"
        hat = self.hat
        hat_label = HAT_NAMES[hat] if 0 <= hat <= 7 else "·"
        return (f"btn=[{names:<22s}] hat={hat_label:<3s} "
                f"L=({self.lx - 128:+4d},{self.ly - 128:+4d}) "
                f"R=({self.rx - 128:+4d},{self.ry - 128:+4d}) "
                f"LT={self.lt:3d} RT={self.rt:3d}")


# ── Injection backends ──────────────────────────────────────────────

class Injector:
    """Turns decoded reports into host input events."""

    name = "none"
    tag = "none"      # Bonjour TXT `inj` value

    def apply(self, report: Report) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        """Release everything. Called when a client disconnects — without
        it a phone that drops mid-press leaves a key or button stuck
        down on the host."""

    def close(self) -> None:
        self.reset()


class LogInjector(Injector):
    """Decode-and-print only. `--inject log`."""

    name = "log only (no injection)"
    tag = "test"

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.count = 0

    def apply(self, report: Report) -> None:
        self.count += 1
        if self.verbose or self.count % 60 == 1:
            print(f"[{ts()}] ▤ REPORT #{self.count}: {report.pretty()}")


class VigemInjector(Injector):
    """Windows: real virtual Xbox 360 pad through ViGEmBus."""

    name = "ViGEmBus virtual Xbox 360 pad"
    tag = "vigem"

    def __init__(self):
        import vgamepad as vg  # noqa: PLC0415 — optional, Windows-only dep

        self._vg = vg
        self._pad = vg.VX360Gamepad()
        self._buttons = {
            BTN_A: vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
            BTN_B: vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
            BTN_X: vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
            BTN_Y: vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
            BTN_L1: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
            BTN_R1: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
            BTN_SELECT: vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
            BTN_START: vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        }
        self._buttons_high = {
            BTN_L3: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
            BTN_R3: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
            BTN_HOME: vg.XUSB_BUTTON.XUSB_GAMEPAD_GUIDE,
        }
        self._dpad = (
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        )

    @staticmethod
    def _to_short(value: int) -> int:
        """Unsigned 0..255 center 128 → signed 16-bit (spec §4.4).

        258 ≈ 65535/254 so 0 → -32768, 128 → 0, 255 → 32767.
        """
        return max(-32768, min(32767, (value - 128) * 258))

    def _to_short_inverted(self, value: int) -> int:
        # X360 sticks are +Y = up; the wire is +Y = down. Negate, then
        # clamp: -(-32768) would overflow the SHORT range.
        return max(-32768, min(32767, -self._to_short(value)))

    def apply(self, report: Report) -> None:
        pad = self._pad
        pad.reset()

        for mask, button in self._buttons.items():
            if report.pressed(mask):
                pad.press_button(button=button)
        for mask, button in self._buttons_high.items():
            if report.pressed(mask, high=True):
                pad.press_button(button=button)

        hat = HAT_VECTORS.get(report.hat)
        if hat:
            for active, button in zip(hat, self._dpad):
                if active:
                    pad.press_button(button=button)

        pad.left_joystick(
            x_value=self._to_short(report.lx),
            y_value=self._to_short_inverted(report.ly))
        pad.right_joystick(
            x_value=self._to_short(report.rx),
            y_value=self._to_short_inverted(report.ry))
        pad.left_trigger(value=report.lt)
        pad.right_trigger(value=report.rt)
        pad.update()

    def reset(self) -> None:
        try:
            self._pad.reset()
            self._pad.update()
        except Exception:
            pass


# macOS virtual key codes (Carbon `kVK_*`).
MAC_KEYS = {
    "a": 0, "s": 1, "d": 2, "f": 3, "c": 8, "q": 12, "w": 13, "e": 14,
    "r": 15, "m": 46, "return": 36, "tab": 48, "space": 49, "escape": 53,
    "shift": 56, "control": 59,
    "left": 123, "right": 124, "down": 125, "up": 126,
}

# CGEventType / tap constants.
_KCG_HID_EVENT_TAP = 0
_KCG_EVENT_LEFT_DOWN = 1
_KCG_EVENT_LEFT_UP = 2
_KCG_EVENT_RIGHT_DOWN = 3
_KCG_EVENT_RIGHT_UP = 4
_KCG_EVENT_MOUSE_MOVED = 5
_KCG_EVENT_LEFT_DRAGGED = 6
_KCG_EVENT_RIGHT_DRAGGED = 7
_KCG_MOUSE_BUTTON_LEFT = 0
_KCG_MOUSE_BUTTON_RIGHT = 1


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class MacKbmInjector(Injector):
    """macOS: gamepad → keyboard + mouse via CGEvent.

    macOS offers third-party code no user-space virtual-HID path
    (DriverKit needs an Apple-granted entitlement; the kext route needs
    SIP disabled), so a real virtual pad is not achievable here. Mapping
    onto keyboard + mouse is the honest alternative and is what most Mac
    and browser games accept.

    Events are posted at the HID tap, which requires **Accessibility**
    permission for the hosting terminal. Without it CGEventPost silently
    does nothing — hence the explicit warning in the startup banner.
    """

    name = "macOS keyboard + mouse (CGEvent)"
    tag = "cgevent"

    # Analog thresholds. The left stick has to cross a wide deadzone
    # before it latches a WASD key (a digital key can't express 30 %
    # deflection), while the mouse only needs to clear sensor noise.
    STICK_DEADZONE = 0.35
    MOUSE_DEADZONE = 0.10
    TRIGGER_THRESHOLD = 64

    def __init__(self, mouse_speed: float = 6.0):
        cg_path = (ctypes.util.find_library("CoreGraphics")
                   or ctypes.util.find_library("ApplicationServices"))
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not cg_path or not cf_path:
            raise RuntimeError("CoreGraphics / CoreFoundation not found")

        self._cg = ctypes.cdll.LoadLibrary(cg_path)
        self._cf = ctypes.cdll.LoadLibrary(cf_path)
        self._mouse_speed = mouse_speed

        cg, cf = self._cg, self._cf
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, _CGPoint, ctypes.c_uint32]
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = _CGPoint
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
        cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
        cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        display = cg.CGMainDisplayID()
        self._screen_w = float(cg.CGDisplayPixelsWide(display))
        self._screen_h = float(cg.CGDisplayPixelsHigh(display))

        self._down_keys: set[int] = set()
        self._left_down = False
        self._right_down = False

    # -- event helpers ------------------------------------------------

    def _post_key(self, keycode: int, down: bool) -> None:
        event = self._cg.CGEventCreateKeyboardEvent(None, keycode, down)
        if not event:
            return
        self._cg.CGEventPost(_KCG_HID_EVENT_TAP, event)
        self._cf.CFRelease(event)

    def _cursor(self) -> _CGPoint:
        probe = self._cg.CGEventCreate(None)
        if not probe:
            return _CGPoint(0.0, 0.0)
        point = self._cg.CGEventGetLocation(probe)
        self._cf.CFRelease(probe)
        return point

    def _post_mouse(self, event_type: int, point: _CGPoint, button: int) -> None:
        event = self._cg.CGEventCreateMouseEvent(
            None, event_type, point, button)
        if not event:
            return
        self._cg.CGEventPost(_KCG_HID_EVENT_TAP, event)
        self._cf.CFRelease(event)

    # -- state diffing ------------------------------------------------

    def _sync_keys(self, wanted: set[int]) -> None:
        for keycode in wanted - self._down_keys:
            self._post_key(keycode, True)
        for keycode in self._down_keys - wanted:
            self._post_key(keycode, False)
        self._down_keys = wanted

    def _sync_mouse_buttons(self, left: bool, right: bool) -> None:
        if left != self._left_down:
            self._post_mouse(
                _KCG_EVENT_LEFT_DOWN if left else _KCG_EVENT_LEFT_UP,
                self._cursor(), _KCG_MOUSE_BUTTON_LEFT)
            self._left_down = left
        if right != self._right_down:
            self._post_mouse(
                _KCG_EVENT_RIGHT_DOWN if right else _KCG_EVENT_RIGHT_UP,
                self._cursor(), _KCG_MOUSE_BUTTON_RIGHT)
            self._right_down = right

    def _move_mouse(self, dx: float, dy: float) -> None:
        if dx == 0.0 and dy == 0.0:
            return
        point = self._cursor()
        target = _CGPoint(
            max(0.0, min(self._screen_w - 1.0, point.x + dx)),
            max(0.0, min(self._screen_h - 1.0, point.y + dy)))
        # While a button is held the OS expects *Dragged, not Moved —
        # otherwise drag gestures (and aim-while-firing) break.
        if self._left_down:
            event_type, button = _KCG_EVENT_LEFT_DRAGGED, _KCG_MOUSE_BUTTON_LEFT
        elif self._right_down:
            event_type, button = (_KCG_EVENT_RIGHT_DRAGGED,
                                  _KCG_MOUSE_BUTTON_RIGHT)
        else:
            event_type, button = _KCG_EVENT_MOUSE_MOVED, _KCG_MOUSE_BUTTON_LEFT
        self._post_mouse(event_type, target, button)

    # -- Injector API -------------------------------------------------

    def apply(self, report: Report) -> None:
        keys: set[int] = set()

        lx, ly = report.axis(report.lx), report.axis(report.ly)
        if lx <= -self.STICK_DEADZONE:
            keys.add(MAC_KEYS["a"])
        elif lx >= self.STICK_DEADZONE:
            keys.add(MAC_KEYS["d"])
        # Wire Y grows downward: positive = stick pushed down = "S".
        if ly <= -self.STICK_DEADZONE:
            keys.add(MAC_KEYS["w"])
        elif ly >= self.STICK_DEADZONE:
            keys.add(MAC_KEYS["s"])

        hat = HAT_VECTORS.get(report.hat)
        if hat:
            up, right, down, left = hat
            if up:
                keys.add(MAC_KEYS["up"])
            if right:
                keys.add(MAC_KEYS["right"])
            if down:
                keys.add(MAC_KEYS["down"])
            if left:
                keys.add(MAC_KEYS["left"])

        for mask, key in (
            (BTN_A, "space"), (BTN_B, "control"), (BTN_X, "e"), (BTN_Y, "r"),
            (BTN_L1, "q"), (BTN_R1, "f"),
            (BTN_SELECT, "tab"), (BTN_START, "return"),
        ):
            if report.pressed(mask):
                keys.add(MAC_KEYS[key])
        for mask, key in ((BTN_L3, "shift"), (BTN_R3, "c"), (BTN_HOME, "m")):
            if report.pressed(mask, high=True):
                keys.add(MAC_KEYS[key])

        self._sync_keys(keys)
        self._sync_mouse_buttons(
            left=report.rt >= self.TRIGGER_THRESHOLD,
            right=report.lt >= self.TRIGGER_THRESHOLD)

        rx, ry = report.axis(report.rx), report.axis(report.ry)
        dx = rx * self._mouse_speed if abs(rx) > self.MOUSE_DEADZONE else 0.0
        # No Y flip: CGEvent screen coordinates also grow downward, and
        # the wire is already +y = down.
        dy = ry * self._mouse_speed if abs(ry) > self.MOUSE_DEADZONE else 0.0
        self._move_mouse(dx, dy)

    def reset(self) -> None:
        self._sync_keys(set())
        self._sync_mouse_buttons(left=False, right=False)


def build_injector(preference: str, verbose: bool,
                   mouse_speed: float) -> Injector:
    """Resolve `--inject` to a concrete backend, degrading loudly."""
    choice = preference
    if choice == "auto":
        if sys.platform == "win32":
            choice = "vigem"
        elif sys.platform == "darwin":
            choice = "macos"
        else:
            choice = "log"

    if choice == "vigem":
        try:
            return VigemInjector()
        except ImportError:
            print("!! vgamepad is not installed — falling back to log only.",
                  file=sys.stderr)
            print("   NO CONTROLLER will appear in games until this is fixed.",
                  file=sys.stderr)
            print("   From source: pip install vgamepad", file=sys.stderr)
        except Exception as exc:
            # Overwhelmingly this is "the ViGEmBus driver isn't installed":
            # vgamepad raises when it cannot open the bus. Say what it means
            # for the user rather than only what failed.
            print(f"!! ViGEmBus backend unavailable ({exc}) — log only.",
                  file=sys.stderr)
            print("   NO CONTROLLER will appear in games until the gamepad "
                  "driver is installed.", file=sys.stderr)
            print("   Tray menu → \"Install gamepad driver…\", accept the "
                  "prompt, then quit and reopen this app.", file=sys.stderr)
        return LogInjector(verbose)

    if choice == "macos":
        if sys.platform != "darwin":
            print("!! --inject macos only works on macOS — log only.",
                  file=sys.stderr)
            return LogInjector(verbose)
        try:
            return MacKbmInjector(mouse_speed=mouse_speed)
        except Exception as exc:
            print(f"!! CGEvent backend unavailable ({exc}) — log only.",
                  file=sys.stderr)
            return LogInjector(verbose)

    return LogInjector(verbose)


# ── Connection lifecycle ────────────────────────────────────────────

@dataclasses.dataclass
class SessionStats:
    reports: int = 0
    pings: int = 0
    dropped: int = 0
    bytes_in: int = 0


# Single-active-client gate per spec §5.3. The TCP socket is accepted
# unconditionally so a polite REJECT can be sent; only the post-HELLO
# session is mutex-gated.
_active_lock = threading.Lock()
_active_peer: str | None = None


def handle_client(client: socket.socket, addr, injector: Injector,
                  verbose: bool) -> None:
    global _active_peer

    # Spec §1: TCP_NODELAY on both ends — the 4 ms / 8 ms report cadence
    # must not be coalesced by Nagle.
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    client.settimeout(5.0)   # handshake window

    print(f"[{ts()}] ◇ connection from {addr[0]}:{addr[1]}")
    stats = SessionStats()
    holding_lock = False

    try:
        reader = frame_reader(client)

        msg_type, payload = next(reader)
        stats.bytes_in += 3 + len(payload)
        if msg_type != T_HELLO:
            client.sendall(encode_reject(
                R_INTERNAL_ERROR,
                f"expected HELLO first, got type=0x{msg_type:02x}"))
            print(f"[{ts()}] ✗ first frame type=0x{msg_type:02x}, not HELLO "
                  f"— rejected")
            return

        proto_ver, pad_name, skin = decode_hello(payload)
        print(f"[{ts()}] ▸ HELLO proto_ver={proto_ver} name={pad_name!r} "
              f"skin={skin!r}")

        if proto_ver != PROTO_VER:
            client.sendall(encode_reject(
                R_VERSION_MISMATCH,
                f"daemon speaks v{PROTO_VER}, client v{proto_ver}"))
            print(f"[{ts()}] ✗ version mismatch — rejected")
            return

        # Acquire BEFORE ACKing so a 2nd phone gets a clear REJECT(in_use)
        # instead of an ACK followed by a silent drop.
        if not _active_lock.acquire(blocking=False):
            client.sendall(encode_reject(
                R_IN_USE, f"daemon is already paired with {_active_peer}"))
            print(f"[{ts()}] ✗ refused — already serving {_active_peer} "
                  f"(in_use)")
            return
        holding_lock = True
        _active_peer = f"{pad_name} @ {addr[0]}"

        client.sendall(encode_hello_ack(PROTO_VER, accept=True))
        print(f"[{ts()}] ◂ HELLO_ACK accept=1 server_ver={PROTO_VER}")
        print(f"[{ts()}] ▶ injecting via {injector.name}")

        # Post-handshake: REPORTs while playing, PING every 2 s when idle.
        # 10 s covers both with slack for a Wi-Fi blip.
        client.settimeout(10.0)

        for msg_type, payload in reader:
            stats.bytes_in += 3 + len(payload)

            if msg_type == T_REPORT:
                stats.reports += 1
                try:
                    report = Report.decode(payload)
                except ValueError as exc:
                    stats.dropped += 1
                    print(f"[{ts()}] ⚠ bad REPORT dropped: {exc}")
                    continue
                if isinstance(injector, LogInjector):
                    injector.apply(report)
                else:
                    if verbose or stats.reports % 120 == 1:
                        print(f"[{ts()}] ▤ #{stats.reports}: {report.pretty()}")
                    try:
                        injector.apply(report)
                    except Exception as exc:
                        # One bad injection must not kill the session —
                        # the next report supersedes it anyway.
                        stats.dropped += 1
                        if stats.dropped <= 3:
                            print(f"[{ts()}] ⚠ injection failed: {exc!r}")
            elif msg_type == T_PING:
                stats.pings += 1
                client.sendall(encode_pong())
                if verbose or stats.pings <= 3 or stats.pings % 15 == 0:
                    print(f"[{ts()}] ⇄ PING → PONG (#{stats.pings})")
            elif msg_type == T_MOUSE:
                # 0x05 is spec'd but the current app never sends it (the
                # touchpad surface was removed). Count it, ignore it.
                stats.dropped += 1
            elif msg_type == T_BYE:
                print(f"[{ts()}] ▣ BYE — clean shutdown")
                return
            else:
                print(f"[{ts()}] ⚠ unknown frame type=0x{msg_type:02x} "
                      f"len={len(payload)}")

    except StopIteration:
        print(f"[{ts()}] ✗ peer closed before completing the handshake")
    except (ConnectionError, socket.timeout, OSError) as exc:
        print(f"[{ts()}] ✗ connection dropped: {exc}")
    except Exception as exc:
        print(f"[{ts()}] ✗ handler error: {exc!r}")
    finally:
        # Always neutralize: a phone that drops mid-press must not leave a
        # key held down or a trigger latched on the host.
        try:
            injector.reset()
        except Exception:
            pass
        if holding_lock:
            _active_peer = None
            _active_lock.release()
        print(f"[{ts()}] ◈ session: {stats.reports} reports, {stats.pings} "
              f"pings, {stats.dropped} dropped, {stats.bytes_in} bytes")
        try:
            client.sendall(encode_bye())
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass


# ── Discovery + main ────────────────────────────────────────────────

# Interface-name prefixes that are virtual point-to-point links (VPNs,
# tunnels) rather than a LAN the phone can be on. Their addresses are
# published last so a phone trying addresses in order reaches the real
# LAN first.
_TUNNEL_PREFIXES = ("utun", "tun", "tap", "ppp", "wg", "ipsec", "gpd", "zt")


def default_route_ip() -> str:
    """Source address of the default route — for the log line only.

    NOT trustworthy as *the* LAN address: with a VPN up, the route to an
    arbitrary internet IP goes through the tunnel, so this returns the
    tunnel address (e.g. 10.8.0.2) while the phone is on 192.168.1.x.
    That mismatch published a Bonjour A record no phone could reach.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
        try:
            # Connecting a UDP socket sends nothing; it just binds the
            # route's source address. The target IP is arbitrary.
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def lan_addresses(override: str | None = None) -> list[str]:
    """Every non-loopback IPv4 address, real LAN interfaces first.

    All of them go into the Bonjour A records: the daemon cannot know
    which subnet the phone is on, and `NWConnection` resolves a service
    name to the full address set and picks a reachable one. Publishing a
    single guessed address is what breaks discovery behind a VPN.

    `ifaddr` is a hard dependency of `zeroconf`, so this costs no extra
    install.
    """
    if override:
        return [override]

    try:
        import ifaddr  # noqa: PLC0415 — pulled in by zeroconf
    except ImportError:
        # Degrade to the old single-address guess rather than refusing to
        # start; the log prints what was published either way.
        return [default_route_ip()]

    physical: list[str] = []
    tunnels: list[str] = []
    for adapter in ifaddr.get_adapters():
        name = (adapter.name or "").lower()
        bucket = tunnels if name.startswith(_TUNNEL_PREFIXES) else physical
        for addr in adapter.ips:
            if not addr.is_IPv4:
                continue
            ip = addr.ip
            if not isinstance(ip, str) or ip.startswith("127."):
                continue
            # 169.254/16 is APIPA: an adapter that never got a DHCP lease
            # (Hyper-V, WSL and VPN stubs leave several behind on Windows).
            # No phone can ever reach those, and publishing them only makes
            # the client try dead addresses first.
            if ip.startswith("169.254."):
                continue
            if ip not in bucket:
                bucket.append(ip)
    ordered = physical + tunnels
    return ordered or [default_route_ip()]


def main(argv: list[str] | None = None) -> int:
    """Entry point. `argv` is explicit so `vpad_helper.py` can drive the
    same engine in a thread instead of shelling out to a second process."""
    parser = argparse.ArgumentParser(
        description="V-Pad companion daemon (reference implementation).",
        epilog="Wire protocol: docs/companion-daemon.md",
    )
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port (default 0 = OS-picked ephemeral, "
                             "published over Bonjour)")
    parser.add_argument("--name", default=None,
                        help="Bonjour service name shown in the iOS app "
                             "(default 'V-Pad daemon — <hostname>')")
    parser.add_argument("--inject", default="auto",
                        choices=["auto", "vigem", "macos", "log"],
                        help="injection backend (default auto)")
    parser.add_argument("--host-ip", default=None,
                        help="publish only this IPv4 address over Bonjour "
                             "(default: every non-loopback address, real "
                             "LAN interfaces before VPN tunnels)")
    parser.add_argument("--mouse-speed", type=float, default=6.0,
                        help="macOS backend: pixels per report at full "
                             "right-stick deflection (default 6)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="print every REPORT frame")
    args = parser.parse_args(argv)

    injector = build_injector(args.inject, args.verbose, args.mouse_speed)

    hostname = socket.gethostname()
    service_name = args.name or f"V-Pad daemon — {hostname}"

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", args.port))
    except OSError as exc:
        print(f"ERROR: bind failed on port {args.port}: {exc}", file=sys.stderr)
        return 2
    server.listen(4)
    port = server.getsockname()[1]

    addresses = lan_addresses(args.host_ip)
    os_tag = {"win32": "win", "darwin": "mac",
              "linux": "linux"}.get(sys.platform, "other")
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{service_name}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip) for ip in addresses],
        port=port,
        properties={
            "v": str(PROTO_VER),
            "os": os_tag,
            "inj": injector.tag,
            "caps": "",
        },
        server=f"{hostname}.local.",
    )

    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        zeroconf.register_service(info)
    except Exception as exc:
        print(f"ERROR: Bonjour publish failed: {exc}", file=sys.stderr)
        print("Hint: another process may already publish this service "
              "name. Try --name 'something-else'.", file=sys.stderr)
        return 3

    print(f"[{ts()}] ══ V-Pad daemon ready ══")
    print(f"[{ts()}] Listening on  0.0.0.0:{port}")
    print(f"[{ts()}] Published IPs: {', '.join(addresses)}")
    print(f"[{ts()}]               (the phone must be on one of these "
          f"subnets)")
    print(f"[{ts()}] Bonjour name:  {service_name!r}")
    print(f"[{ts()}] Injection:     {injector.name}")
    print(f"[{ts()}] TXT:           v={PROTO_VER}, os={os_tag}, "
          f"inj={injector.tag}")
    if isinstance(injector, MacKbmInjector):
        print(f"[{ts()}] ⚠  macOS needs Accessibility permission for this "
              f"terminal:")
        print(f"[{ts()}]    System Settings → Privacy & Security → "
              f"Accessibility")
        print(f"[{ts()}]    Without it the events are silently discarded.")
        print(f"[{ts()}] Map: stick=WASD hat=arrows right-stick=mouse "
              f"RT=left-click LT=right-click")
        print(f"[{ts()}]      A=Space B=Ctrl X=E Y=R L1=Q R1=F L3=Shift "
              f"R3=C Select=Tab Start=Return")
    print(f"[{ts()}] One phone at a time (spec §5.3). Ctrl+C to stop.")
    print()

    try:
        while True:
            client, addr = server.accept()
            # Thread per connection so a 2nd phone can be REJECTed
            # immediately instead of queueing behind the live session.
            threading.Thread(
                target=handle_client,
                args=(client, addr, injector, args.verbose),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        print(f"\n[{ts()}] ▣ shutting down")
    finally:
        try:
            injector.close()
        except Exception:
            pass
        try:
            zeroconf.unregister_service(info)
            zeroconf.close()
        except Exception:
            pass
        try:
            server.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
