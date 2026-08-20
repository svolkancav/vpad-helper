#!/usr/bin/env python3
"""macOS eşlemesi: gamepad raporu → klavye tuş kodları.

    python -m unittest discover -s host -v

Neden bu dosya var: macOS'ta sanal bir kumanda yaratmanın kullanıcı alanı
yolu yok (DriverKit Apple'dan entitlement ister, kext yolu SIP kapatmak
ister), o yüzden Mac'te girdi klavye + fareye çevriliyor. Bu çeviri
`vpad_daemon.py`'de yazılmıştı ve yeni motora taşınırken sessizce
bozulabilirdi — üstelik projede macOS koşan bir CI yok, yani kimse fark
etmezdi. Eşleme saf bir fonksiyona (`mac_keys_for`) ayrıldı ve burada her
platformda çiviliyor; CoreGraphics'e ihtiyaç duymuyor.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_host as host  # noqa: E402

K = host.MAC_KEYS
CENTER = 128
# Hat, `btn_high`'ın üst yarısında ve NÖTRÜ 8 (0x80) — sıfır DEĞİL: sıfır
# "yukarı"dır. Dinlenen bir rapor kurarken bunu unutmak, testin her
# raporuna bir ok tuşu ekler (ilk yazımda tam bu oldu).
HAT_NEUTRAL = 0x80


def report(btn_low: int = 0, btn_high: int = HAT_NEUTRAL, lx: int = CENTER,
           ly: int = CENTER, rx: int = CENTER, ry: int = CENTER,
           lt: int = 0, rt: int = 0) -> host.Report:
    return host.Report(btn_low, btn_high, lx, ly, rx, ry, lt, rt)


class MacMappingTest(unittest.TestCase):
    def test_resting_report_presses_nothing(self):
        self.assertEqual(host.mac_keys_for(report()), set())

    def test_left_stick_becomes_wasd(self):
        self.assertEqual(host.mac_keys_for(report(lx=0)), {K["a"]})
        self.assertEqual(host.mac_keys_for(report(lx=255)), {K["d"]})
        # Telde Y aşağı büyüyor: 0 = yukarı = W.
        self.assertEqual(host.mac_keys_for(report(ly=0)), {K["w"]})
        self.assertEqual(host.mac_keys_for(report(ly=255)), {K["s"]})

    def test_a_small_lean_is_not_a_keypress(self):
        """Dijital tuş %20 eğimi ifade edemez; ölü bölge geçilmeli."""
        self.assertEqual(host.mac_keys_for(report(lx=150)), set())
        self.assertEqual(host.mac_keys_for(report(lx=CENTER + 60)), {K["d"]})

    def test_diagonal_stick_presses_two_keys(self):
        self.assertEqual(host.mac_keys_for(report(lx=0, ly=0)),
                         {K["a"], K["w"]})

    def test_hat_becomes_arrow_keys(self):
        # hat, btn_high'ın üst yarısında yaşıyor.
        for direction, key in (("up", "up"), ("right", "right"),
                               ("down", "down"), ("left", "left")):
            with self.subTest(direction=direction):
                value = {"up": 0, "right": 2, "down": 4, "left": 6}[direction]
                keys = host.mac_keys_for(report(btn_high=value << 4))
                self.assertIn(K[key], keys)

    def test_face_and_shoulder_buttons(self):
        cases = [
            (host.BTN_A, "space"), (host.BTN_B, "control"),
            (host.BTN_X, "e"), (host.BTN_Y, "r"),
            (host.BTN_L1, "q"), (host.BTN_R1, "f"),
            (host.BTN_SELECT, "tab"), (host.BTN_START, "return"),
        ]
        for mask, key in cases:
            with self.subTest(key=key):
                self.assertEqual(host.mac_keys_for(report(btn_low=mask)),
                                 {K[key]})

    def test_stick_clicks_and_home_live_in_the_high_byte(self):
        for mask, key in ((host.BTN_L3, "shift"), (host.BTN_R3, "c"),
                          (host.BTN_HOME, "m")):
            with self.subTest(key=key):
                self.assertEqual(
                    host.mac_keys_for(report(btn_high=mask | HAT_NEUTRAL)),
                    {K[key]})

    def test_a_full_hand_maps_every_input_at_once(self):
        keys = host.mac_keys_for(report(
            btn_low=host.BTN_A | host.BTN_L1,
            btn_high=host.BTN_L3 | (2 << 4),   # L3 + hat sağ
            lx=255,
        ))
        self.assertEqual(keys, {K["space"], K["q"], K["shift"],
                                K["right"], K["d"]})


class MacBackendSelectionTest(unittest.TestCase):
    def test_macos_backend_is_refused_off_macos(self):
        """Yanlış platformda sessizce 'çalışıyor' demek en kötüsü olurdu."""
        if sys.platform == "darwin":
            self.assertEqual(host.resolve_backend("macos"), "cgevent")
        else:
            self.assertEqual(host.resolve_backend("macos"), "log")

    def test_auto_picks_the_platform_backend(self):
        expected = {"win32": ("vigem", "log"), "darwin": ("cgevent",)}
        got = host.resolve_backend("auto")
        if sys.platform in expected:
            self.assertIn(got, expected[sys.platform])
        else:
            self.assertEqual(got, "log")


if __name__ == "__main__":
    unittest.main()
