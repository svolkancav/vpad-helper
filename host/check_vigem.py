#!/usr/bin/env python3
"""
ViGEm çoklu sanal pad doğrulaması — GERÇEK sürücüyle, telefonsuz.

Birim testi DEĞİL, bilinçli olarak `test_` önekiyle başlamıyor: ViGEmBus
sürücüsü gerektiriyor, `unittest discover` onu toplamamalı.

Kapattığı boşluk şu: `test_e2e_host.py` slot mantığının doğruluğunu
gösteriyor ama enjektörü casusla değiştiriyor. Burada gerçek `vgamepad`
kullanılıyor ve sonuç **işletim sisteminden geri okunuyor** — XInput'a
`XInputGetState` ile sorulup "Windows gerçekten iki kumanda görüyor mu,
tuşlar doğru pad'e mi düşüyor, Y ekseni doğru yönde mi" sorularının
cevabı ölçülüyor. Kendi kodumuza sormuyoruz; sisteme soruyoruz.

Ön koşul:
    pip install vgamepad        # ViGEmBus sürücüsünü de kurar (UAC ister)

Koşturma:
    python check_vigem.py
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_host                                    # noqa: E402
import vpad_pairing as pairing                      # noqa: E402
import vpad_slots as slots                          # noqa: E402
from vpad_reference_client import Rejected, VPadClient   # noqa: E402

PORT = 55177
PLAYERS = slots.MAX_SLOTS

# Her telefona AYRI tuş: hangi raporun hangi pad'e düştüğü ancak böyle
# ayırt edilebiliyor.
PAD_BUTTONS = [
    ("A", vpad_host.BTN_A),
    ("B", vpad_host.BTN_B),
    ("X", vpad_host.BTN_X),
    ("Y", vpad_host.BTN_Y),
]

# XInput buton maskeleri (XINPUT_GAMEPAD_*).
XI_A, XI_B, XI_X, XI_Y = 0x1000, 0x2000, 0x4000, 0x8000
XI_NAMES = {XI_A: "A", XI_B: "B", XI_X: "X", XI_Y: "Y"}

ERROR_SUCCESS = 0


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint), ("Gamepad", XINPUT_GAMEPAD)]


def load_xinput():
    """XInput DLL'i — sürüm adı Windows sürümüne göre değişiyor."""
    for name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            return ctypes.windll.LoadLibrary(name)
        except OSError:
            continue
    return None


def read_pads(xinput) -> dict[int, XINPUT_GAMEPAD]:
    """Bağlı XInput yuvalarını okur. Anahtar = Windows'un yuva numarası.

    Bizim slot indeksimizle AYNI olmak zorunda değil: ViGEm pad'i o an boş
    olan yuvaya takıyor ve makinede başka kumanda da olabilir. Bu yüzden
    aşağıda yuva numarası değil, **durumların kümesi** karşılaştırılıyor.
    """
    found: dict[int, XINPUT_GAMEPAD] = {}
    for index in range(4):
        state = XINPUT_STATE()
        if xinput.XInputGetState(index, ctypes.byref(state)) == ERROR_SUCCESS:
            found[index] = state.Gamepad
    return found


def describe(pad: XINPUT_GAMEPAD) -> str:
    buttons = [n for m, n in XI_NAMES.items() if pad.wButtons & m] or ["—"]
    return (f"btn={'+'.join(buttons):<4s} "
            f"LX={pad.sThumbLX:+6d} LY={pad.sThumbLY:+6d} "
            f"LT={pad.bLeftTrigger:3d} RT={pad.bRightTrigger:3d}")


def fail(message: str) -> None:
    print(f"  ✗ {message}")
    globals()["FAILURES"] += 1


def ok(message: str) -> None:
    print(f"  ✓ {message}")


FAILURES = 0


def main() -> int:
    global FAILURES

    if sys.platform != "win32":
        print("Bu betik yalnızca Windows'ta anlamlı.")
        return 1

    try:
        import vgamepad  # noqa: F401
    except ImportError:
        print("vgamepad kurulu değil. Kurulum ViGEmBus sürücüsünü de kurar\n"
              "ve UAC onayı ister:\n\n    pip install vgamepad\n")
        return 1
    except Exception as exc:
        print(f"vgamepad içe aktarılamadı: {exc}")
        return 1

    xinput = load_xinput()
    if xinput is None:
        print("XInput DLL bulunamadı — doğrulama yapılamaz.")
        return 1

    baseline = read_pads(xinput)
    print(f"Başlangıçta bağlı XInput yuvaları: "
          f"{sorted(baseline) if baseline else 'yok'}")
    if baseline:
        print("  (fiziksel kumanda takılıysa aşağıdaki sayımlar onu da içerir;"
              " çıkarıp tekrar koşmak en temizi)")

    # ── Gerçek host'u ayağa kaldır ───────────────────────────────────
    thread = threading.Thread(
        target=vpad_host.main,
        args=(["--players", str(PLAYERS), "--inject", "vigem",
               "--port", str(PORT), "--host-ip", "127.0.0.1",
               "--name", "ViGEm dogrulama"],),
        daemon=True)
    thread.start()
    time.sleep(2.0)

    info = pairing.parse_payload(
        pairing.build_payload("127.0.0.1", PORT, pairing.generate_token()))

    print(f"\n=== 1) Pad'ler TEK TEK belirmeli ({PLAYERS} telefon) ===")
    print("  (önden dört pad yaratsaydık tek telefon bağlıyken bile "
          "oyunlar dört kumanda görürdü)")
    clients: list[tuple[str, VPadClient]] = []
    for label, mask in PAD_BUTTONS[:PLAYERS]:
        client = VPadClient(info, name=f"Telefon {label}")
        client.connect(timeout=5.0, pair_wait=0.3)
        client.send_report(buttons=mask)
        clients.append((label, client))
        time.sleep(0.4)
        live = set(read_pads(xinput)) - set(baseline)
        if len(live) == len(clients):
            ok(f"{len(clients)}. telefon → {len(live)} pad "
               f"(yuvalar {sorted(live)})")
        else:
            fail(f"{len(clients)} telefon bağlıyken {len(live)} pad görüldü")

    state = read_pads(xinput)
    new = sorted(set(state) - set(baseline))
    print(f"  slot dağılımı: {[(n, c.slot) for n, c in clients]}")
    for index in new:
        print(f"    yuva {index}: {describe(state[index])}")

    print("\n=== 2) Her pad KENDİ tuşunu görmeli (çapraz sızma yok) ===")
    pressed = [{n for m, n in XI_NAMES.items() if state[i].wButtons & m}
               for i in new]
    expected = [{label} for label, _ in PAD_BUTTONS[:PLAYERS]]
    if sorted(pressed, key=sorted) == sorted(expected, key=sorted):
        ok(f"her pad yalnız kendi tuşunu görüyor: {pressed}")
    else:
        fail(f"buton dağılımı beklenenden farklı: {pressed} != {expected}")

    print(f"\n=== 3) {PLAYERS + 1}. telefon reddedilmeli (XInput tavanı) ===")
    try:
        extra = VPadClient(info, name="Fazladan telefon")
        extra.connect(timeout=5.0, pair_wait=0.3)
        extra.close()
        fail("havuz doluyken fazladan istemci kabul edildi")
    except Rejected as exc:
        if exc.reason == vpad_host.R_IN_USE:
            ok(f"reddedildi: reason=0x{exc.reason:02x}")
        else:
            fail(f"beklenen 0x{vpad_host.R_IN_USE:02x}, gelen "
                 f"0x{exc.reason:02x}")
    except Exception as exc:
        fail(f"REJECT yerine ham hata: {exc!r}")

    # Y ekseni ve nötrleme sınamaları ilk telefon üzerinden.
    a = clients[0][1]

    print("\n=== 4) Y ekseni yönü — telde +Y aşağı, XInput'ta +Y yukarı ===")
    # Sol çubuğu telde AŞAĞI it (ly=255). XInput NEGATİF görmeli.
    a.send_report(buttons=0, ly=255)
    time.sleep(0.4)
    state = read_pads(xinput)
    a_slots = sorted(set(state) - set(baseline))
    down_ok = any(state[i].sThumbLY < -20000 for i in a_slots)
    if down_ok:
        ok("çubuk aşağı → XInput sThumbLY negatif (ters çevirme doğru)")
    else:
        fail("çubuk aşağı itildi ama negatif LY okunmadı — "
             f"{[state[i].sThumbLY for i in a_slots]}")

    a.send_report(buttons=0, ly=0)
    time.sleep(0.4)
    state = read_pads(xinput)
    up_ok = any(state[i].sThumbLY > 20000 for i in a_slots)
    if up_ok:
        ok("çubuk yukarı → XInput sThumbLY pozitif")
    else:
        fail("çubuk yukarı itildi ama pozitif LY okunmadı — "
             f"{[state[i].sThumbLY for i in a_slots]}")

    print("\n=== 5) Kopan telefon pad'i basılı tuşla bırakmamalı ===")
    # Diğer telefonlar B/X/Y tutuyor; yalnız bu A basıyor, yani kopuştan
    # sonra herhangi bir pad'de A görülürse takılı kalmış demektir.
    a.send_report(buttons=vpad_host.BTN_A)
    time.sleep(0.3)
    a.close()
    time.sleep(0.8)
    state = read_pads(xinput)
    stuck = [i for i in sorted(set(state) - set(baseline))
             if state[i].wButtons & XI_A]
    if not stuck:
        ok("kopuş sonrası hiçbir pad'de A basılı kalmadı")
    else:
        fail(f"A tuşu basılı kaldı: yuva(lar) {stuck}")

    for _, client in clients[1:]:
        client.close()
    time.sleep(0.5)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"SONUÇ: {FAILURES} doğrulama BAŞARISIZ")
    else:
        print("SONUÇ: hepsi geçti — ViGEm çoklu pad gerçek sürücüyle çalışıyor")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
