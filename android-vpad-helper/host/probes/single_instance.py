#!/usr/bin/env python3
"""Only one helper may hold the session, and the claim must not outlive it.

Two copies are easy to end up with: "Start with Windows" is on and the
user also opens the shortcut. The result is not two harmless tray icons —
both engines start and both log success, but mDNS carries a single record
per instance name, so the phone reaches whichever registered first while
the other sits invisible holding a virtual gamepad. Measured before the
fix: two daemons with an identical --name, no exception from either, one
record on the wire.

The second property matters as much as the first. The installer force-
terminates the helper before replacing its files, so a claim that
survived a killed process would lock the user out of their own app after
every update.

    python single_instance.py

Runs anywhere. Spawns child processes of itself; no network, no driver.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "..")))

CHILD = "--child"


def _child():
    import vpad_helper as helper  # noqa: PLC0415 — child-side import
    granted = helper.claim_single_instance()
    print("GRANTED" if granted else "REFUSED", flush=True)
    # Hold the claim while the parent runs its checks against us.
    time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0.0)


if CHILD in sys.argv:
    _child()
    raise SystemExit(0)


def spawn(hold: float):
    return subprocess.Popen(
        [sys.executable, "-u", __file__, CHILD, str(hold)],
        stdout=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def verdict(proc) -> str:
    return (proc.stdout.readline() or "").strip()


failures = []


def check(label, condition):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        failures.append(label)


print("1. first instance claims the session")
holder = spawn(30)
first = verdict(holder)
print(f"   -> {first}")
check("the first instance is granted", first == "GRANTED")

print("2. a second instance, while the first is alive")
second = spawn(0)
denied = verdict(second)
print(f"   -> {denied}")
second.wait(timeout=10)
check("the second instance is refused", denied == "REFUSED")

print("3. the claim is released when the holder is KILLED, not asked")
holder.kill()
holder.wait(timeout=10)
time.sleep(0.5)
third = spawn(0)
result = verdict(third)
print(f"   -> {result}")
third.wait(timeout=10)
check("a new instance starts after a forced kill", result == "GRANTED")

print()
if failures:
    raise SystemExit(f"{len(failures)} check(s) failed: " + "; ".join(failures))
print("all checks passed")
