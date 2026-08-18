#!/usr/bin/env python3
"""
vpad_pairing birim testleri.

Koşturma:
    python -m unittest discover -s host -v
    python host/test_vpad_pairing.py

Üçüncü parti bağımlılık yok — bu dosya her yerde koşar.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_pairing as p  # noqa: E402 — path önce ayarlanmalı


class TokenTests(unittest.TestCase):

    def test_token_length_and_randomness(self):
        a, b = p.generate_token(), p.generate_token()
        self.assertEqual(len(a), p.TOKEN_LEN)
        self.assertNotEqual(a, b, "token sabit üretiliyor")

    def test_hex_round_trip(self):
        token = p.generate_token()
        self.assertEqual(p.token_from_hex(p.token_to_hex(token)), token)
        self.assertEqual(len(p.token_to_hex(token)), 32)

    def test_hex_rejects_bad_input(self):
        for bad in ["", "abc", "z" * 32, "0" * 31, "0" * 33, " " * 32]:
            with self.assertRaises(p.PairingError, msg=f"{bad!r} kabul edildi"):
                p.token_from_hex(bad)


class LanRangeTests(unittest.TestCase):
    """Sınır değerleri Kotlin tarafıyla BİREBİR aynı olmalı."""

    def test_lan_addresses_accepted(self):
        for ip in [
            "127.0.0.1", "127.255.255.254",
            "10.0.0.0", "10.255.255.255",
            "172.16.0.0", "172.31.255.255",
            "192.168.0.0", "192.168.255.255",
            "169.254.0.1", "169.254.255.254",
            "100.64.0.0", "100.127.255.255",
        ]:
            self.assertTrue(p.is_lan_ipv4(ip), f"{ip} LAN sayılmalıydı")

    def test_public_addresses_rejected(self):
        for ip in [
            "8.8.8.8", "1.1.1.1", "93.184.216.34",
            "172.15.255.255",  # 172.16/12'nin HEMEN ALTI
            "172.32.0.0",      # 172.16/12'nin HEMEN ÜSTÜ
            "100.63.255.255",  # 100.64/10'un hemen altı
            "100.128.0.0",     # 100.64/10'un hemen üstü
            "11.0.0.1",        # 10/8 dışı
            "192.169.0.1",     # 192.168/16 dışı
            "192.167.255.255",
            "169.253.255.255", # link-local dışı
            "169.255.0.0",
            "0.0.0.0",
            "255.255.255.255",
        ]:
            self.assertFalse(p.is_lan_ipv4(ip), f"{ip} reddedilmeliydi")

    def test_non_ipv4_literals_rejected(self):
        """Alan adı asla kabul edilmez — DNS rebinding kapısı kapalı."""
        for text in [
            "example.com", "localhost", "vpad.local",
            "::1", "fe80::1", "[::1]",
            "", "   ", "192.168.1", "192.168.1.256", "192.168.1.1.1",
            "0x7f000001", "2130706433",  # sayısal kısayollar
            "192.168.01.1",  # baştaki sıfır — Python bunu da reddeder
        ]:
            self.assertFalse(p.is_lan_ipv4(text), f"{text!r} reddedilmeliydi")


class PayloadTests(unittest.TestCase):

    def setUp(self):
        self.token = bytes(range(16))
        self.token_hex = self.token.hex()

    def test_build_and_parse_round_trip(self):
        text = p.build_payload("192.168.1.34", 53124, self.token)
        self.assertEqual(
            text,
            f"vpad://192.168.1.34:53124?t={self.token_hex}&v=1",
        )
        info = p.parse_payload(text)
        self.assertEqual(info.host, "192.168.1.34")
        self.assertEqual(info.port, 53124)
        self.assertEqual(info.token, self.token)
        self.assertEqual(info.token_hex, self.token_hex)

    def test_build_refuses_public_address(self):
        with self.assertRaises(p.PairingError):
            p.build_payload("8.8.8.8", 8765, self.token)

    def test_build_refuses_bad_port(self):
        for port in [0, -1, 65536, 999999]:
            with self.assertRaises(p.PairingError):
                p.build_payload("192.168.1.1", port, self.token)

    def test_parse_rejects_hostile_payloads(self):
        cases = {
            "boş": "",
            "yanlış şema": f"http://192.168.1.1:80?t={self.token_hex}&v=1",
            "şemasız": f"192.168.1.1:80?t={self.token_hex}&v=1",
            "netpad şeması": f"netpad://192.168.1.1:80?t={self.token_hex}&v=1",
            "sorgu yok": "vpad://192.168.1.1:80",
            "port yok": f"vpad://192.168.1.1?t={self.token_hex}&v=1",
            "port boş": f"vpad://192.168.1.1:?t={self.token_hex}&v=1",
            "port sayı değil": f"vpad://192.168.1.1:abc?t={self.token_hex}&v=1",
            "port işaretli": f"vpad://192.168.1.1:+80?t={self.token_hex}&v=1",
            "port sıfır": f"vpad://192.168.1.1:0?t={self.token_hex}&v=1",
            "port aşkın": f"vpad://192.168.1.1:65536?t={self.token_hex}&v=1",
            "adres boş": f"vpad://:80?t={self.token_hex}&v=1",
            "genel adres": f"vpad://8.8.8.8:80?t={self.token_hex}&v=1",
            "alan adı": f"vpad://evil.example.com:80?t={self.token_hex}&v=1",
            "token yok": "vpad://192.168.1.1:80?v=1",
            "token kısa": "vpad://192.168.1.1:80?t=abcd&v=1",
            "token hex değil": f"vpad://192.168.1.1:80?t={'z' * 32}&v=1",
            "sürüm yok": f"vpad://192.168.1.1:80?t={self.token_hex}",
            "sürüm yanlış": f"vpad://192.168.1.1:80?t={self.token_hex}&v=2",
            "sürüm sayı değil": f"vpad://192.168.1.1:80?t={self.token_hex}&v=x",
            "biçimsiz sorgu": f"vpad://192.168.1.1:80?t={self.token_hex}&v=1&bozuk",
            "yinelenen anahtar": (
                f"vpad://192.168.1.1:80?t={self.token_hex}&t={'0'*32}&v=1"
            ),
        }
        for name, payload in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(p.PairingError):
                    p.parse_payload(payload)

    def test_unicode_digits_rejected_like_kotlin(self):
        """GERİLEME: `str.isdigit()` Unicode rakamlarını kabul ediyordu.

        `'٨٠'.isdigit()` True ve `int('٨٠')` 80 verir. Kotlin ayrıştırıcısı
        ASCII dışını reddettiği için aynı payload iki tarafta farklı sonuç
        veriyordu — protokolde kabul edilemez bir ayrışma.
        """
        arabic_80 = "٨٠"   # ٨٠
        arabic_1 = "١"          # ١
        self.assertTrue(arabic_80.isdigit(), "test öncülü değişti")

        with self.assertRaises(p.PairingError):
            p.parse_payload(f"vpad://192.168.1.1:{arabic_80}?t={self.token_hex}&v=1")
        with self.assertRaises(p.PairingError):
            p.parse_payload(f"vpad://192.168.1.1:80?t={self.token_hex}&v={arabic_1}")

    def test_ascii_digit_helper(self):
        for good in ["0", "80", "65535", "000"]:
            self.assertTrue(p._is_ascii_digits(good), f"{good!r} kabul edilmeliydi")
        for bad in ["", " ", "8 0", "+8", "-8", "8.0", "0x50",
                    "٨٠", "８０"]:  # Arapça-Hint, tam genişlik
            self.assertFalse(p._is_ascii_digits(bad), f"{bad!r} reddedilmeliydi")

    def test_parse_tolerates_unknown_extra_param(self):
        """İleri uyumluluk: bilinmeyen anahtar payload'ı geçersiz kılmaz."""
        text = f"vpad://10.0.0.5:9000?t={self.token_hex}&v=1&name=Studio"
        info = p.parse_payload(text)
        self.assertEqual(info.host, "10.0.0.5")
        self.assertEqual(info.port, 9000)

    def test_parse_trims_surrounding_whitespace(self):
        """QR tarayıcıları bazen sonda satır sonu bırakır."""
        text = f"  vpad://10.0.0.5:9000?t={self.token_hex}&v=1\n"
        self.assertEqual(p.parse_payload(text).port, 9000)


class ChallengeResponseTests(unittest.TestCase):

    def setUp(self):
        self.token = bytes(range(16))
        self.challenge = bytes(range(100, 116))
        self.nonce = bytes(range(200, 216))

    def test_mac_is_deterministic_and_32_bytes(self):
        a = p.compute_auth(self.token, self.challenge, self.nonce)
        b = p.compute_auth(self.token, self.challenge, self.nonce)
        self.assertEqual(a, b)
        self.assertEqual(len(a), p.MAC_LEN)

    def test_golden_vector(self):
        """Kotlin tarafı bu değeri birebir üretmek zorunda.

        Bu sabit, iki dildeki iki uygulamanın protokol sözleşmesidir. Değeri
        değiştirmek = protokolü kırmak; o zaman Kotlin testi de güncellenmeli.
        """
        mac = p.compute_auth(self.token, self.challenge, self.nonce)
        self.assertEqual(
            mac.hex(),
            GOLDEN_MAC_HEX,
            "altın vektör değişti — Kotlin tarafı da güncellenmeli",
        )

    def test_verify_accepts_correct_response(self):
        body = p.build_auth_body(self.token, self.challenge, self.nonce)
        self.assertEqual(len(body), p.AUTH_LEN)
        self.assertTrue(p.verify_auth(self.token, self.challenge, body))

    def test_verify_rejects_wrong_token(self):
        body = p.build_auth_body(self.token, self.challenge, self.nonce)
        other = bytes(16)
        self.assertFalse(p.verify_auth(other, self.challenge, body))

    def test_verify_rejects_replay_against_new_challenge(self):
        """Asıl güvenlik iddiası: yakalanan AUTH başka bağlantıda geçersiz."""
        body = p.build_auth_body(self.token, self.challenge, self.nonce)
        fresh_challenge = p.make_challenge()
        self.assertNotEqual(fresh_challenge, self.challenge)
        self.assertFalse(p.verify_auth(self.token, fresh_challenge, body))

    def test_verify_rejects_tampered_mac(self):
        body = bytearray(p.build_auth_body(self.token, self.challenge, self.nonce))
        body[-1] ^= 0x01  # tek bit
        self.assertFalse(p.verify_auth(self.token, self.challenge, bytes(body)))

    def test_verify_rejects_tampered_nonce(self):
        body = bytearray(p.build_auth_body(self.token, self.challenge, self.nonce))
        body[0] ^= 0x01
        self.assertFalse(p.verify_auth(self.token, self.challenge, bytes(body)))

    def test_verify_rejects_malformed_bodies_without_raising(self):
        for body in [b"", b"\x00", bytes(47), bytes(49), bytes(1000)]:
            self.assertFalse(
                p.verify_auth(self.token, self.challenge, body),
                f"{len(body)} baytlık gövde kabul edildi",
            )

    def test_challenge_is_fresh_each_call(self):
        seen = {p.make_challenge() for _ in range(100)}
        self.assertEqual(len(seen), 100, "challenge tekrar ediyor")
        self.assertEqual(len(seen.pop()), p.CHALLENGE_LEN)

    def test_random_nonce_still_verifies(self):
        """nonce=None yolu: istemci kendi entropisini katıyor."""
        for _ in range(20):
            body = p.build_auth_body(self.token, self.challenge)
            self.assertTrue(p.verify_auth(self.token, self.challenge, body))


class FrameTests(unittest.TestCase):

    # Spec'in (docs/companion-daemon.md §4) sahiplendiği tip kodları.
    # 0x20 UYGULANMAMIŞ ama AYRILMIŞ: RUMBLE, v3'e ertelenmiş.
    SPEC_TYPES = {
        0x01: "HELLO", 0x02: "REPORT", 0x03: "PING", 0x04: "BYE",
        0x05: "MOUSE", 0x10: "HELLO_ACK", 0x11: "REJECT", 0x12: "PONG",
        0x20: "RUMBLE (v3)",
    }

    def test_pairing_type_codes_pinned_and_free(self):
        """GERİLEME: CHALLENGE bir ara 0x20'ye konmuştu — RUMBLE'ın yeri.

        Diğer testlerin hepsi `p.T_CHALLENGE` gibi SEMBOLİK kullanıyor,
        yani yanlış bir değer atansa hiçbiri kırılmaz. Kodlar burada telde
        oldukları hâliyle çivileniyor; Kotlin karşılığı
        `WifiFrameCodecTest.tip kodlari spec kayit defteriyle ayni`.
        """
        self.assertEqual(p.T_CHALLENGE, 0x13)
        self.assertEqual(p.T_AUTH, 0x06)
        for code in (p.T_CHALLENGE, p.T_AUTH):
            self.assertNotIn(
                code, self.SPEC_TYPES,
                f"eşleşme tipi 0x{code:02x} spec'te "
                f"{self.SPEC_TYPES.get(code)} için ayrılmış")
        # REJECT sebepleri: spec 0x01/0x02/0x03/0xff kullanıyor.
        self.assertEqual(p.R_AUTH_REQUIRED, 0x04)
        self.assertEqual(p.R_AUTH_FAILED, 0x05)
        self.assertNotIn(p.R_AUTH_REQUIRED, (0x01, 0x02, 0x03, 0xFF))
        self.assertNotIn(p.R_AUTH_FAILED, (0x01, 0x02, 0x03, 0xFF))

    def test_frame_header_matches_daemon_format(self):
        frame = p.encode_frame(0x02, b"12345678")
        # [u16 LE toplam][u8 tip][gövde]
        self.assertEqual(frame[0], 11)   # 3 + 8
        self.assertEqual(frame[1], 0)
        self.assertEqual(frame[2], 0x02)
        self.assertEqual(frame[3:], b"12345678")

    def test_challenge_frame(self):
        challenge = p.make_challenge()
        frame = p.encode_challenge(challenge)
        self.assertEqual(len(frame), 3 + p.CHALLENGE_LEN)
        self.assertEqual(frame[2], p.T_CHALLENGE)
        self.assertEqual(frame[3:], challenge)

    def test_auth_frame(self):
        token, challenge = p.generate_token(), p.make_challenge()
        frame = p.encode_auth(token, challenge)
        self.assertEqual(len(frame), 3 + p.AUTH_LEN)
        self.assertEqual(frame[2], p.T_AUTH)
        self.assertTrue(p.verify_auth(token, challenge, frame[3:]))

    def test_oversize_frame_refused(self):
        with self.assertRaises(p.PairingError):
            p.encode_frame(0x02, b"x" * p.MAX_FRAME)


class QrRenderTests(unittest.TestCase):

    def test_render_never_raises(self):
        """qrcode kurulu olsun olmasın, açılış yolu patlamamalı."""
        payload = p.build_payload("192.168.1.34", 8765, p.generate_token())
        for big in (False, True):
            out = p.render_qr_terminal(payload, big=big)
            self.assertIsInstance(out, str)
            self.assertTrue(out.strip(), "boş çıktı")

    def test_matrix_to_text_dimensions(self):
        """qrcode paketinden bağımsız: matris → metin dönüşümü."""
        matrix = [[(x + y) % 2 == 0 for x in range(21)] for y in range(21)]
        compact = p._matrix_to_text(matrix, big=False)
        big = p._matrix_to_text(matrix, big=True)
        # Yarım blok: iki satır bir karakter satırına iner
        self.assertEqual(len(compact.rstrip("\n").split("\n")), 11)  # ceil(21/2)
        self.assertEqual(len(big.rstrip("\n").split("\n")), 21)


# Altın vektör — `test_golden_vector` bunu doğrular ve Kotlin testi aynısını
# kullanır. Değer, aşağıdaki `__main__` bloğuyla yeniden üretilebilir.
GOLDEN_MAC_HEX = (
    "ef9831a78ba47467094297a247929b5a4c98ba105cbd363dd18a61cd994e64df"
)


if __name__ == "__main__":
    if "--print-golden" in sys.argv:
        token = bytes(range(16))
        challenge = bytes(range(100, 116))
        nonce = bytes(range(200, 216))
        print("token    :", token.hex())
        print("challenge:", challenge.hex())
        print("nonce    :", nonce.hex())
        print("mac      :", p.compute_auth(token, challenge, nonce).hex())
        sys.exit(0)
    unittest.main(verbosity=2)
