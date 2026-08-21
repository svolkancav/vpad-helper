# V-Pad Helper

[![Build](https://github.com/svolkancav/vpad-helper/actions/workflows/build.yml/badge.svg)](https://github.com/svolkancav/vpad-helper/actions/workflows/build.yml) [![Release](https://img.shields.io/github/v/release/svolkancav/vpad-helper)](https://github.com/svolkancav/vpad-helper/releases/latest) [![License: MIT](https://img.shields.io/github/license/svolkancav/vpad-helper)](LICENSE)


The free companion app that lets **V-Pad: Virtual Gamepad** on your iPhone act as a
gamepad for your computer.

**[⬇ Download for Windows](https://github.com/svolkancav/vpad-helper/releases/latest/download/V-Pad-Helper-Setup.exe)** — a normal installer: no administrator rights, and an entry in Add/Remove Programs when you want it gone. The app itself lives in the system tray. A [single-file build](https://github.com/svolkancav/vpad-helper/releases/latest/download/V-Pad.Helper.exe) is published too, for anyone who prefers nothing installed.

**[⬇ Download for macOS](https://github.com/svolkancav/vpad-helper/releases/latest/download/V-Pad-Helper.dmg)** — signed and notarised by Apple, so it opens without a Gatekeeper detour. Open the disk image and **drag the app into Applications**, then launch it from there; it lives in the menu bar.

> **Drag it to Applications — this is not optional.** Launched from `~/Downloads`, macOS runs the app *translocated*: from a randomised read-only copy. Permissions you grant are recorded against the path you granted them for, so an Accessibility grant given to the copy in Downloads does not apply to the copy that is actually running, and input is discarded with no error anywhere.

---

## Why a helper app is needed

On Android, V-Pad presents the phone *itself* as a Bluetooth gamepad and pairs with
your PC directly — nothing to install.

iOS does not allow that. Apple reserves the Bluetooth HID device role for the
system: an app that tries to advertise the HID service (UUID `0x1812`) is refused
outright by CoreBluetooth, and there is no Bluetooth Classic HID device API at all.
So on iPhone the phone can never appear in your computer's Bluetooth list, no matter
what any app claims.

What *is* possible is Wi-Fi. V-Pad on iPhone finds this helper on your local network
and sends it the same gamepad reports the Android build sends over Bluetooth; the
helper turns them into real input on your computer. That is also why every iOS app in
this category ships a desktop component.

## Install

1. Run `V-Pad-Helper-Setup.exe`. It installs for your user only, so it does **not**
   ask for administrator rights, and it leaves an uninstaller behind. When it
   finishes, the app starts — but **no window opens**: this is a tray app, it just
   sits next to your clock. On Windows 11 new tray icons start out hidden, so click
   the **^** arrow to find it (Settings → Personalization → Taskbar → *Other system
   tray icons* keeps it visible). A notification on first run tells you it is up.
2. **The app is not code-signed yet**, so Windows may warn that the publisher is
   unknown, and a machine-learning antivirus scanner may object. Earlier releases were
   flagged as `Trojan:Win32/Wacatac.C!ml` — a **false positive** that
   PyInstaller-packaged Python apps are well known for. Every engine that flagged it
   was heuristic; no signature-based scanner did.

   Two things were changed at the source rather than asking users to live with it: the
   build now compiles its own PyInstaller bootloader (the prebuilt one, shared with
   thousands of real samples, is what the scanners matched on), and the app ships
   installed instead of unpacking itself into `%TEMP%` on every launch. A
   false-positive report is filed with Microsoft, and code signing is next.

   If your scanner still removes the download, restore it: **Windows Security → Virus &
   threat protection → Protection history →** find the V-Pad Helper entry **→ Actions →
   Allow on device**, then download again. On other antivirus products the equivalent is
   "restore from quarantine" plus an exclusion. Or skip the binary altogether — the
   whole thing is open source, so [read it](https://github.com/svolkancav/vpad-helper)
   and [run it from source](#running-from-source).
3. Windows then asks whether the app may use the network. **Allow it on private
   networks** — without that the phone can see your computer but cannot connect.
4. **On first run it offers the gamepad driver** and asks for permission. *This* one
   does need administrator rights, because it installs a driver. Accept it. That is
   [ViGEmBus](https://github.com/nefarius/ViGEmBus), what makes games see a genuine
   Xbox 360 controller; the installer is bundled, nothing extra to download. Skip it
   and games will see no controller — the tray menu can still install it later.

Then on the iPhone: open V-Pad → **Connection** → tap your computer → open any layout
and play. Keep the phone and the PC on the same Wi-Fi network.

## What the tray shows

| | |
|---|---|
| Blue icon | waiting for a phone (the address it published is in the menu) |
| Green icon | a phone is connected |
| Injection line | which backend is in use — real gamepad, keyboard/mouse, or log only |
| Start with Windows | optional; keeps the helper ready after a reboot |

## Platforms

| OS | What you get |
|---|---|
| **Windows** | A real virtual Xbox 360 pad via ViGEmBus. Games see an ordinary XInput controller — no key mapping, triggers and sticks are analog. |
| **macOS** | Keyboard + mouse, one player. macOS offers third-party code no user-space virtual-HID path (DriverKit needs an Apple-granted entitlement, the kext route needs SIP disabled), so the pad is mapped onto keys: left stick = WASD, D-pad = arrows, right stick = mouse, RT/LT = left/right click, A/B/X/Y = Space/Ctrl/E/R, L1/R1 = Q/F, L3/R3 = Shift/C, Select/Start = Tab/Return. Needs Accessibility permission (System Settings → Privacy & Security → Accessibility) — without it macOS accepts the input and silently discards it. |
| **Linux** | Not implemented yet. The protocol is documented and `uinput` is the intended path. |

## Running from source

```bash
pip install -r requirements.txt
python3 vpad_helper.py              # tray icon + the pairing engine
python3 vpad_helper.py --no-tray    # same engine, console only
```

The engine lives in `android-vpad-helper/host/vpad_host.py`: pairing
tickets, a device ledger, up to four players, and one injector per
platform. `vpad_daemon.py` is the older pairing-less engine, kept for
reference — a phone running the 2.x app cannot connect to it.

Useful flags: `--name` (the label the phone shows),
`--inject auto|vigem|macos|log` (auto picks ViGEmBus on Windows and
keyboard + mouse on macOS), `--players`,
`--mouse-speed`, `--host-ip`, `--verbose`.

## Building the Windows release

CI does all of this on every push (`.github/workflows/build.yml`); these are the
same commands, for a Windows box.

```powershell
pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm vpad-helper.spec                       # single-file .exe
$env:VPAD_ONEDIR=1
pyinstaller --noconfirm --distpath dist-onedir `
            --workpath build-onedir vpad-helper.spec           # installer payload
iscc /DAppVersion=0.2.2 installer.iss                          # -> dist-installer\
```

The version lives in exactly one place, `__version__` in `vpad_helper.py`; the
spec reads it into the .exe's version resource and CI refuses to build a `v*` tag
that disagrees with it. `vpad-helper.ico` is committed, and `make-icon.py`
regenerates it from `brand/icongamepad.png` if the artwork changes.

Signing is optional and off by default — see `sign-windows.ps1`.

## Troubleshooting

**The phone doesn't list my computer.** Both must be on the same Wi-Fi. A VPN on the
computer is the usual culprit: the helper prints every address it publishes
(`Published IPs:`) and the phone must be on one of those subnets. Corporate networks
often block mDNS entirely.

**It's listed but won't connect.** The Windows firewall prompt was probably declined.
Allow "V-Pad Helper" on private networks in Windows Defender Firewall settings.

**Connected, but nothing happens in the game.** On Windows: the tray still offers
"Install gamepad driver" — the driver isn't in place. In Steam, also enable
*Settings → Controller → Generic Gamepad Configuration Support*. On macOS: grant
Accessibility permission to the app running the helper, otherwise the synthesized
events are silently discarded.

**Two phones at once?** Not supported by design — the second one is politely refused,
one phone at a time.

## Privacy

The helper talks to nothing but your phone, over your own network. No accounts, no
telemetry, no outbound connections. It listens on an ephemeral TCP port and announces
itself over mDNS so the phone can find it; that traffic never leaves your LAN.
