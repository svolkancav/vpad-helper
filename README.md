# V-Pad Host (C#) — download branch

This branch exists to host one file: **`vpad-host.exe`**, the Windows host
that turns your phone into a real XInput controller.

It is not source code — the C# source lives in the main app repository.
This is the file <https://vpadcontroller.com> links to.

## Install (2 steps)

1. Install the **ViGEmBus driver** first (one time only):
   <https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/ViGEmBus_1.22.0_x64_x86_arm64.exe>
2. Download **[vpad-host.exe](./vpad-host.exe?raw=true)**, double-click it,
   and look for the V-Pad icon in the notification area (bottom-right corner).

Windows will warn that the file is unsigned the first time —
choose **More info → Run anyway**.

`THIRD-PARTY-NOTICES.txt` carries the licences of everything the exe
redistributes; the exe prints the same text with `vpad-host --licenses`.

sha256 of the current build:

```
27e5aaaab48e53465b48e84ceda8f1e620e3cc4f8bc8c4fe960061d108131262  vpad-host.exe
```
