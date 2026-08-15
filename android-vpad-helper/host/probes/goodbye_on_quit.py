#!/usr/bin/env python3
"""Quitting must withdraw the Bonjour record, not just stop answering.

The engine runs in a DAEMON thread when the tray drives it, and the
threading docs are blunt about those: "Daemon threads are abruptly
stopped at shutdown. Their resources … may not be released properly."
The mDNS unregister lives in exactly such a `finally`, so letting the
process exit skipped the goodbye packet entirely.

What that costs is in RFC 6762: §10.1's goodbye carries a TTL of zero and
the receiver "record[s] a TTL of 1 and then delete[s] the record one
second later", whereas §10 recommends a **75 minute** TTL for these
records otherwise. A phone with the record cached kept offering a
computer whose helper had been closed — measured at 30 s and still
listed, before the fix.

This mirrors the real shutdown path: engine in a daemon thread, asked to
unwind, with a browsing client watching the way the phone does.

    python goodbye_on_quit.py

Runs anywhere; uses --inject log so it needs no gamepad driver.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf  # noqa: E402

import vpad_daemon as engine  # noqa: E402

TYPE = "_vpad-bridge._tcp.local."
NAME = "goodbye-probe"
GRACE = 5.0

live: set[str] = set()


class Watcher(ServiceListener):
    def add_service(self, zc, type_, name):
        live.add(name)

    update_service = add_service

    def remove_service(self, zc, type_, name):
        live.discard(name)


def advertised():
    return sorted(n for n in live if NAME in n)


zc = Zeroconf()
ServiceBrowser(zc, TYPE, Watcher())

thread = threading.Thread(
    target=lambda: engine.main(
        ["--port", "0", "--inject", "log", "--name", NAME]),
    daemon=True)
thread.start()
time.sleep(6)

print(f"engine advertised : {advertised() or 'NO'}")
if not advertised():
    zc.close()
    raise SystemExit("the daemon never advertised; nothing to test")

print("asking it to unwind (what tray Quit does)…")
started = time.time()
engine.request_shutdown()
thread.join(timeout=GRACE)
elapsed = time.time() - started
print(f"engine stopped in {elapsed:.2f}s  (still alive: {thread.is_alive()})")

# The goodbye tells the receiver to drop the record after one second, so
# anything past ~3 s is a comfortable margin without being a wait.
time.sleep(3.0)
remaining = advertised()
print(f"record after +3s  : {remaining or 'WITHDRAWN'}")
zc.close()

if thread.is_alive():
    raise SystemExit(f"FAIL: the engine did not unwind within {GRACE:.0f}s")
if remaining:
    raise SystemExit("FAIL: the Bonjour record outlived the daemon")
print("\nall checks passed")
