#!/usr/bin/env python3
"""A connection that is refused must not touch the pad of the one playing.

`vpad_daemon.handle_client()` neutralises the injector in its `finally`.
The injector is ONE object shared by every connection thread, so while
that reset was unconditional, any connection that never got past the
handshake ran it: a second phone getting REJECT(in_use), a port scanner,
a browser tab opened on the wrong port. Each of them released the buttons
of whoever was actually in a game.

Both halves are checked here, because the fix is a narrowing and a
narrowing can go too far:

    A. refused connections must NOT reset      (the bug)
    B. the real owner dropping mid-press MUST  (what the reset is for)

Read back through XInput — asking our own code what it thinks it sent
would have agreed with itself in both versions.

    python pad_isolation.py [port]        default 51555

Needs Windows + ViGEmBus, and a daemon started with --inject vigem.
"""
import ctypes
import socket
import struct
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 51555

T_HELLO, T_HELLO_ACK, T_REPORT = 0x01, 0x10, 0x02
BTN_A, BTN_B = 0x01, 0x02
XUSB_A, XUSB_B = 0x1000, 0x2000

# Hat nibble 8 = released. Sending 0 here means "D-pad North", which is
# how this probe first reported a phantom extra button.
HAT_RELEASED = 0x80


class GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", GAMEPAD)]


def _xinput():
    for dll in ("XInput1_4.dll", "XInput9_1_0.dll", "XInput1_3.dll"):
        try:
            return ctypes.WinDLL(dll)
        except OSError:
            continue
    raise SystemExit("no XInput DLL — this probe needs Windows")


XI = _xinput()


def buttons():
    """Button mask of the first connected XInput pad, or None."""
    state = STATE()
    for slot in range(4):
        if XI.XInputGetState(slot, ctypes.byref(state)) == 0:
            return state.Gamepad.wButtons
    return None


def frame(msg_type, payload=b""):
    return struct.pack("<HB", 3 + len(payload), msg_type) + payload


def hello(name):
    return frame(T_HELLO, bytes([1, len(name)]) + name + bytes([0]))


def report(btn_low):
    return frame(T_REPORT,
                 bytes([btn_low, HAT_RELEASED, 128, 128, 128, 128, 0, 0]))


def play(name, button):
    """Connect, take the session, hold `button`, then go quiet.

    Going quiet matters: it is what makes the window observable at all.
    A client streaming at 4 ms would repaint the pad immediately after
    any stray reset and hide the whole thing.
    """
    sock = socket.create_connection(("127.0.0.1", PORT), timeout=5)
    sock.sendall(hello(name))
    ack = sock.recv(64)
    if len(ack) < 3 or ack[2] != T_HELLO_ACK:
        raise SystemExit(f"{name.decode()} was not accepted: {ack!r}")
    sock.sendall(report(button))
    time.sleep(1.0)
    return sock


failures = []


def check(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


print(f"probing daemon on 127.0.0.1:{PORT}")

print("\nA. a refused connection must not disturb the active pad")
owner = play(b"owner", BTN_A)
held = buttons()
if held is None or not held & XUSB_A:
    raise SystemExit("setup failed: no pad, or A never registered — is the "
                     "daemon running with --inject vigem?")
print(f"  owner holds A            0x{held:04x}")

second = socket.create_connection(("127.0.0.1", PORT), timeout=5)
second.sendall(hello(b"second"))
try:
    second.recv(64)          # REJECT(in_use)
except OSError:
    pass
second.close()
time.sleep(0.6)
after_reject = buttons()
print(f"  after REJECT(in_use)     0x{after_reject:04x}")
check("a refused second phone leaves the pad alone", bool(after_reject & XUSB_A))

socket.create_connection(("127.0.0.1", PORT), timeout=5).close()
time.sleep(0.6)
after_scan = buttons()
print(f"  after bare connect+close 0x{after_scan:04x}")
check("a port scan leaves the pad alone", bool(after_scan & XUSB_A))

print("\nB. the owner dropping mid-press must still clear the pad")
owner.close()
time.sleep(1.2)
dropped = buttons()
print(f"  after the owner vanishes 0x{dropped:04x}")
check("a dropped owner does not leave A stuck down", not dropped & XUSB_A)

nxt = play(b"next", BTN_B)
inherited = buttons()
print(f"  next phone holds B       0x{inherited:04x}")
check("the next phone inherits no stuck button", not inherited & XUSB_A)
check("the next phone's own button works", bool(inherited & XUSB_B))
nxt.close()

print()
if failures:
    raise SystemExit(f"{len(failures)} check(s) failed: " + "; ".join(failures))
print("all checks passed")
