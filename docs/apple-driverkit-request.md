# Asking Apple for the virtual-HID entitlement

## What this would buy

A real virtual gamepad on macOS. Today the Mac helper types on the keyboard and
moves the mouse, because macOS grants third-party code no user-space path to
create an HID device. That is why a Mac gets one player and Windows gets four:
on Windows, ViGEmBus makes four XInput pads; on macOS there is no pad to make,
and four people cannot share one keyboard.

With the entitlement, macOS reaches parity: real controllers, four players,
games that demand a gamepad stop refusing.

## What to ask for

Submit at **https://developer.apple.com/contact/request/system-extension/**

Entitlements to request:

| Entitlement | Why |
|---|---|
| `com.apple.developer.driverkit` | Base — permission to ship a DriverKit extension at all |
| `com.apple.developer.driverkit.family.hid.virtual.device` | The one that matters: create a *virtual* HID device |
| `com.apple.developer.driverkit.transport.hid` | Communicate with HID devices |

The precedent worth naming in the request is
[Karabiner-DriverKit-VirtualHIDDevice](https://github.com/pqrs-org/Karabiner-DriverKit-VirtualHIDDevice)
— an open-source DriverKit extension that creates virtual HID keyboards and
pointers, shipped and running on ordinary Macs. It establishes that Apple grants
this for exactly this shape of product.

## Draft request

```
Company: DolceFarNiente Mondo
Team ID: X44QH5PVRJ
App: V-Pad Helper (com.dfnmondo.vpadhelper), distributed outside the Mac App
Store, Developer ID signed and notarised.

What the app does
V-Pad turns a phone into a game controller for a computer. The phone runs our
iOS/Android app; a small helper runs on the computer and presents the phone's
input to games as a controller. On Windows the helper creates a virtual XInput
pad, so games see an ordinary gamepad and need no special support.

What we are asking for
com.apple.developer.driverkit, com.apple.developer.driverkit.family.hid.virtual.device
and com.apple.developer.driverkit.transport.hid, so a DriverKit extension shipped
inside the helper can create a virtual HID gamepad.

Why no other API is sufficient
macOS exposes no user-space way for third-party code to create an HID device.
IOHIDUserDevice is entitlement-gated; the kernel-extension route requires the
user to disable SIP, which we will not ask anyone to do. Today we translate the
pad into keyboard and mouse events through CGEvent. That works for games driven
by keyboard and mouse and fails for every game that requires a real controller,
which is most of the category our users are in. It also caps us at one player:
CGEvent has one keyboard focus and one cursor, while the Windows build supports
four simultaneous players.

What the driver will and will not do
It will create a virtual HID gamepad and deliver reports our own helper hands it,
originating from a phone the user paired on their own network. It will not read
input from other devices, not observe other processes, and not communicate off
the machine — the network side stays in the user-space helper, outside the
driver.

Distribution
Developer ID, notarised, outside the Mac App Store, same as the current helper.
The extension will be embedded in the helper app and activated with
OSSystemExtensionRequest, with the user approving it in System Settings.

Precedent
Karabiner-DriverKit-VirtualHIDDevice (open source, pqrs-org) ships the same class
of extension — a virtual HID device created by a third-party DriverKit driver.
```

## What it actually costs, before anyone celebrates

The entitlement is not a switch. Granting it means we can *build* the thing that
does not exist yet:

- **The Mac helper has to be rewritten.** A DriverKit extension is a native
  driver, C++ against the DriverKit SDK, embedded in a real macOS app bundle. The
  helper today is Python packaged by PyInstaller, which cannot host one. The
  network and pairing layer can stay Python-adjacent, but the Mac app becomes a
  native project.
- **The user gains a step, not loses one.** System extensions must be approved in
  System Settings and the Mac restarted on some versions. We would be trading the
  Accessibility prompt for an extension-approval prompt, not removing a prompt.
- **Apple decides, and takes its time.** The request is discretionary and the
  turnaround is measured in weeks. Our notarisation runs this month took 20 to 43
  hours; an entitlement review is a different order of magnitude.

So the honest sequence is: send the request, keep shipping the keyboard-and-mouse
Mac build, and only start the native rewrite if and when the entitlement lands.
Nothing about the current roadmap should depend on it.
