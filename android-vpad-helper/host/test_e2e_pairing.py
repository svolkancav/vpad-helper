#!/usr/bin/env python3
"""
Uçtan uca eşleşme testleri — GERÇEK TCP soketi üzerinden.

Birim testleri fonksiyonların doğruluğunu gösterir; bu dosya iki tarafın
telde gerçekten anlaştığını gösterir: sunucu `PairingGate`'i kullanır,
istemci `vpad_reference_client.VPadClient`'ı kullanır, aralarında localhost
üzerinde asıl protokol akar.

Buradaki sunucu, `DAEMON_PATCH.md`'nin `vpad_daemon.py`'ye önerdiği akışın
birebir aynısıdır — yani bu testler yamanın da doğrulaması sayılır.

    python -m unittest discover -s host -p "test_e2e*" -v
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_pairing as pairing              # noqa: E402
import vpad_reference_client as client_mod  # noqa: E402
from vpad_reference_client import (         # noqa: E402
    FrameReader, ProtocolError, Rejected, VPadClient,
)

PROTO_VER = 1
T_REPORT = 0x02
T_PING = 0x03
T_BYE = 0x04
T_PONG = 0x12


class TestDaemon:
    """Eşleşme kapılı minik daemon. DAEMON_PATCH.md'deki akışın aynısı."""

    def __init__(self, token: bytes | None, slot: int | None = None):
        self.token = token
        # Çoklu oyuncu modu: HELLO_ACK ile AYNI `sendall` içinde gönderilir —
        # gerçek daemon da öyle yapıyor (DAEMON_PATCH.md §7.3) ve hatanın
        # ortaya çıktığı koşul tam olarak bu: tek `recv` iki çerçeveyi birden
        # getiriyor.
        self.slot = slot
        self.reports: list[bytes] = []
        self.hellos: list[str] = []
        self.rejections: list[int] = []
        self.errors: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- yaşam döngüsü --------------------------------------------------

    def payload(self) -> str:
        """Bu daemon'ın QR'a basacağı metin."""
        token = self.token if self.token is not None else pairing.generate_token()
        return pairing.build_payload("127.0.0.1", self.port, token)

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    def __enter__(self) -> "TestDaemon":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- sunucu ---------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5.0)
        reader = FrameReader(conn)
        try:
            # ── Eşleşme kapısı — yamanın eklediği tek blok ──
            gate = pairing.PairingGate(self.token)
            opening = gate.opening_frame()
            if opening is not None:
                conn.sendall(opening)
                msg_type, payload = reader.next_frame()
                ok, reject = gate.accept(msg_type, payload)
                if not ok:
                    assert reject is not None
                    self.rejections.append(reject[3])  # sebep baytı
                    conn.sendall(reject)
                    return

            # ── Buradan sonrası mevcut daemon akışı ──
            msg_type, payload = reader.next_frame()
            if msg_type != pairing.T_HELLO:
                conn.sendall(pairing.encode_reject(0xFF, "HELLO bekleniyordu"))
                return
            name_len = payload[1]
            self.hellos.append(payload[2:2 + name_len].decode("utf-8", "replace"))
            ack = pairing.encode_frame(pairing.T_HELLO_ACK,
                                       bytes([PROTO_VER, 1]))
            if self.slot is not None:
                ack += pairing.encode_slot(self.slot)   # tek yazım, bilinçli
            conn.sendall(ack)

            while True:
                msg_type, payload = reader.next_frame()
                if msg_type == T_REPORT:
                    self.reports.append(payload)
                elif msg_type == T_PING:
                    conn.sendall(pairing.encode_frame(T_PONG))
                elif msg_type == T_BYE:
                    return
        except (OSError, ProtocolError, IndexError) as exc:
            self.errors.append(repr(exc))
        finally:
            try:
                conn.close()
            except OSError:
                pass


def wait_for(predicate, timeout: float = 3.0) -> bool:
    """Kısa yoklama — sabit `sleep` yerine koşula bak."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class PairedConnectionTests(unittest.TestCase):

    def test_correct_token_pairs_and_input_flows(self):
        token = pairing.generate_token()
        with TestDaemon(token) as daemon:
            info = pairing.parse_payload(
                pairing.build_payload("127.0.0.1", daemon.port, token))
            with VPadClient(info, "Test Telefon") as client:
                client.connect(pair_wait=1.0)
                self.assertTrue(client.paired, "eşleşme gerçekleşmedi")
                client.send_report(
                    buttons=client_mod.BUTTONS["A"] | client_mod.BUTTONS["START"],
                    hat=2, lx=255, ly=0, rx=128, ry=64, lt=200, rt=55)
                self.assertTrue(wait_for(lambda: len(daemon.reports) >= 1),
                                "rapor host'a ulaşmadı")

            self.assertEqual(daemon.hellos, ["Test Telefon"])
            self.assertEqual(daemon.rejections, [])
            self.assertEqual(daemon.errors, [])

    def test_report_bytes_match_hid_layout(self):
        """Telden geçen 8 bayt, HidReportSender'ın ürettiğinin aynısı olmalı."""
        token = pairing.generate_token()
        with TestDaemon(token) as daemon:
            info = pairing.parse_payload(
                pairing.build_payload("127.0.0.1", daemon.port, token))
            with VPadClient(info) as client:
                client.connect(pair_wait=1.0)
                client.send_report(
                    buttons=client_mod.BUTTONS["A"] | client_mod.BUTTONS["HOME"],
                    hat=3, lx=1, ly=2, rx=3, ry=4, lt=5, rt=6)
                self.assertTrue(wait_for(lambda: len(daemon.reports) >= 1))

            report = daemon.reports[0]
            self.assertEqual(len(report), 8)
            #  A = 0x01 (düşük bayt)
            self.assertEqual(report[0], 0x01)
            #  HOME = yüksek baytın b2'si (0x04); hat=3 → üst nibble
            self.assertEqual(report[1], 0x04 | (3 << 4))
            self.assertEqual(report[1] >> 4, 3, "hat nibble'ı yanlış yerde")
            self.assertEqual(tuple(report[2:]), (1, 2, 3, 4, 5, 6))

    def test_neutral_report_sent_on_close(self):
        """Kopmadan önce nötr rapor → host'ta takılı tuş kalmasın."""
        token = pairing.generate_token()
        with TestDaemon(token) as daemon:
            info = pairing.parse_payload(
                pairing.build_payload("127.0.0.1", daemon.port, token))
            client = VPadClient(info)
            client.connect(pair_wait=1.0)
            client.send_report(buttons=client_mod.BUTTONS["A"])
            self.assertTrue(wait_for(lambda: len(daemon.reports) >= 1))
            client.close()
            self.assertTrue(wait_for(lambda: len(daemon.reports) >= 2))

            last = daemon.reports[-1]
            self.assertEqual(last[0], 0, "son rapor nötr değil (buton basılı)")
            self.assertEqual(last[1] >> 4, client_mod.HAT_CENTER)
            self.assertEqual(tuple(last[2:6]), (128, 128, 128, 128))

    def test_ping_pong(self):
        token = pairing.generate_token()
        with TestDaemon(token) as daemon:
            info = pairing.parse_payload(
                pairing.build_payload("127.0.0.1", daemon.port, token))
            with VPadClient(info) as client:
                client.connect(pair_wait=1.0)
                client.ping()  # PONG gelmezse ProtocolError atar
            self.assertEqual(daemon.errors, [])


class RejectionTests(unittest.TestCase):

    def test_wrong_token_is_rejected(self):
        host_token = pairing.generate_token()
        phone_token = pairing.generate_token()
        self.assertNotEqual(host_token, phone_token)

        with TestDaemon(host_token) as daemon:
            info = pairing.parse_payload(
                pairing.build_payload("127.0.0.1", daemon.port, phone_token))
            with VPadClient(info) as client:
                with self.assertRaises(Rejected) as ctx:
                    client.connect(pair_wait=1.0)
            self.assertEqual(ctx.exception.reason, pairing.R_AUTH_FAILED)
            self.assertEqual(daemon.reports, [], "reddedilen istemci girdi geçirdi")

    def test_client_skipping_auth_is_rejected(self):
        """Token'sız istemci (eski sürüm veya elle bağlanan) reddedilir."""
        token = pairing.generate_token()
        with TestDaemon(token) as daemon:
            sock = socket.create_connection(("127.0.0.1", daemon.port), 3)
            sock.settimeout(3)
            reader = FrameReader(sock)
            try:
                msg_type, _ = reader.next_frame()
                self.assertEqual(msg_type, pairing.T_CHALLENGE)

                # CHALLENGE'ı yok say, doğrudan HELLO yolla
                sock.sendall(client_mod.encode_hello("Kurnaz Istemci"))
                msg_type, payload = reader.next_frame()
                self.assertEqual(msg_type, pairing.T_REJECT)
                self.assertEqual(payload[0], pairing.R_AUTH_REQUIRED)
            finally:
                sock.close()
            self.assertEqual(daemon.reports, [])

    def test_replayed_auth_is_rejected(self):
        """ASIL GÜVENLİK İDDİASI: yakalanan AUTH ikinci bağlantıda geçersiz."""
        token = pairing.generate_token()
        with TestDaemon(token) as daemon:
            # 1) Meşru bağlantı — AUTH gövdesini "dinleyip" sakla
            sock1 = socket.create_connection(("127.0.0.1", daemon.port), 3)
            sock1.settimeout(3)
            r1 = FrameReader(sock1)
            msg_type, challenge1 = r1.next_frame()
            self.assertEqual(msg_type, pairing.T_CHALLENGE)
            captured = pairing.build_auth_body(token, challenge1)
            sock1.sendall(pairing.encode_frame(pairing.T_AUTH, captured))
            sock1.sendall(client_mod.encode_hello("Mesru"))
            msg_type, _ = r1.next_frame()
            self.assertEqual(msg_type, pairing.T_HELLO_ACK)
            sock1.close()

            # 2) Saldırgan aynı AUTH'u yeni bağlantıda tekrar oynatır
            sock2 = socket.create_connection(("127.0.0.1", daemon.port), 3)
            sock2.settimeout(3)
            r2 = FrameReader(sock2)
            msg_type, challenge2 = r2.next_frame()
            self.assertEqual(msg_type, pairing.T_CHALLENGE)
            self.assertNotEqual(challenge1, challenge2,
                                "challenge tekrar kullanıldı — replay açık!")

            sock2.sendall(pairing.encode_frame(pairing.T_AUTH, captured))
            msg_type, payload = r2.next_frame()
            self.assertEqual(msg_type, pairing.T_REJECT,
                             "tekrar oynatılan AUTH kabul edildi")
            self.assertEqual(payload[0], pairing.R_AUTH_FAILED)
            sock2.close()

    def test_truncated_auth_is_rejected(self):
        token = pairing.generate_token()
        with TestDaemon(token) as daemon:
            sock = socket.create_connection(("127.0.0.1", daemon.port), 3)
            sock.settimeout(3)
            reader = FrameReader(sock)
            try:
                msg_type, challenge = reader.next_frame()
                self.assertEqual(msg_type, pairing.T_CHALLENGE)
                body = pairing.build_auth_body(token, challenge)[:-1]  # 1 bayt eksik
                sock.sendall(pairing.encode_frame(pairing.T_AUTH, body))
                msg_type, payload = reader.next_frame()
                self.assertEqual(msg_type, pairing.T_REJECT)
                self.assertEqual(payload[0], pairing.R_AUTH_FAILED)
            finally:
                sock.close()


class BackwardCompatibilityTests(unittest.TestCase):

    def test_pairing_disabled_host_still_serves_token_bearing_client(self):
        """`--pair` verilmemiş host: istemci zaman aşımıyla eşleşmesiz moda düşer.

        Bu, "QR'ı taradım ama host'u --pair olmadan yeniden başlattım"
        senaryosu. Kilitlenme olmamalı; bağlantı kurulmalı.
        """
        with TestDaemon(None) as daemon:  # eşleşme KAPALI
            info = pairing.parse_payload(
                pairing.build_payload("127.0.0.1", daemon.port,
                                      pairing.generate_token()))
            with VPadClient(info, "Eski Akis") as client:
                client.connect(pair_wait=0.4)
                self.assertFalse(client.paired, "eşleşme kapalıyken eşleşti sanıldı")
                client.send_report(buttons=client_mod.BUTTONS["B"])
                self.assertTrue(wait_for(lambda: len(daemon.reports) >= 1))

            self.assertEqual(daemon.hellos, ["Eski Akis"])
            self.assertEqual(daemon.errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class SlotTests(unittest.TestCase):
    """GERİLEME: slot, HELLO_ACK ile AYNI paketten gelir.

    Bu hata birim testlerinden kaçtı ve ancak yamalanmış gerçek daemon'a
    karşı koşturunca göründü: `HELLO_ACK` okunurken tek bir `recv` iki
    çerçeveyi birden getiriyor, yani SLOT için sokette okunacak bayt
    KALMIYOR. İstemcinin bloklamayan tahliyesi önce sokete bakıp tamponu
    atlıyordu; sonuç, host slot atadığı hâlde `client.slot is None`.
    """

    def test_slot_arrives_in_the_same_packet_as_hello_ack(self):
        with TestDaemon(None, slot=2) as daemon:
            info = pairing.parse_payload(daemon.payload())
            with VPadClient(info, "Ucuncu Oyuncu") as client:
                client.connect(pair_wait=0.4)
                self.assertEqual(client.slot, 2,
                                 "slot HELLO_ACK ile aynı pakette geldi ve "
                                 "kaçırıldı")

    def test_single_player_host_leaves_slot_none(self):
        with TestDaemon(None) as daemon:
            info = pairing.parse_payload(daemon.payload())
            with VPadClient(info, "Tek") as client:
                client.connect(pair_wait=0.4)
                self.assertIsNone(client.slot,
                                  "tek oyunculu host slot göndermez")
