#!/usr/bin/env python3
"""
`vpad_host.handle_client` uçtan uca testleri — GERÇEK TCP soketi üzerinden.

`test_e2e_pairing.py` yamanın *önerdiği* akışı minik bir `TestDaemon` ile
sınıyordu. Bu dosya farklı bir şey yapıyor: **üretimdeki `handle_client`'ın
kendisini** çağırıyor. Test kopyası yok, yani testin geçmesi host'un
çalıştığı anlamına geliyor.

Karşı taraf `vpad_reference_client.VPadClient` — Kotlin istemcisinin
`connect()` sıralamasının birebir aynısını uygulayan istemci.

Kapsanan şeyler dört telefon GEREKTİRMİYOR; hepsi localhost üzerinde
gerçek soketlerle koşuyor:

    * slot dağıtımı, çoklu istemci
    * havuz dolduğunda REJECT(in_use)
    * yapışkanlık — kopan telefon eski slotuna döner
    * eşleşme kapısının slot ALMADAN önce çalışması (güvenlik özelliği)
    * nötrleme → slot bırakma sırası
    * sürüm uyuşmazlığında slot sızmaması

    python -m unittest discover -s android-vpad-helper/host -p "test_e2e*" -v
"""
from __future__ import annotations

import os
import shutil
import socket
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_devices as devices                # noqa: E402
import vpad_host                              # noqa: E402
import vpad_pairing as pairing                # noqa: E402
import vpad_slots as slots                    # noqa: E402
from vpad_reference_client import (           # noqa: E402
    Rejected, VPadClient,
)

# Host sessizken istemcinin CHALLENGE'ı bekleme süresi. Varsayılan 2 sn,
# localhost'ta gereksiz — testleri yavaşlatmasın.
FAST_PAIR_WAIT = 0.3


def wait_until(predicate, timeout: float = 3.0) -> bool:
    """`predicate` doğru olana kadar bekler. Sunucu tarafı temizlik
    başka bir thread'de olduğu için gerekiyor: `client.close()` döndüğünde
    host henüz `finally` bloğunu bitirmemiş olabilir."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class SpyInjector(vpad_host.Injector):
    """Enjekte etmez, kaydeder."""

    name = "casus"
    tag = "test"

    def __init__(self, slot: int, journal: list[tuple[str, int]]):
        self.slot = slot
        self.reports: list[vpad_host.Report] = []
        self._journal = journal

    def apply(self, report: vpad_host.Report) -> None:
        self.reports.append(report)

    def reset(self) -> None:
        self._journal.append(("reset", self.slot))


class SpyPadSet(vpad_host.PadSet):
    """`PadSet`'in kendisi, yalnız arka ucu casusla değiştirilmiş.

    `acquire` üretimdeki hâliyle çalışıyor — tembel yaratım ve kilit dâhil.
    """

    def __init__(self, journal: list[tuple[str, int]]):
        super().__init__("log", verbose=False)
        self._journal = journal

    def _build(self, slot: int) -> vpad_host.Injector:
        return SpyInjector(slot, self._journal)


class SpyPool(slots.SlotPool):
    """Bırakma sırasını günlüğe yazan havuz."""

    def __init__(self, size: int, journal: list[tuple[str, int]]):
        super().__init__(size)
        self._journal = journal

    def release(self, lease: slots.SlotLease) -> None:
        super().release(lease)
        self._journal.append(("release", lease.index))


class Host:
    """Üretimdeki `handle_client`'ı gerçek bir dinleyicinin arkasına koyar.

    `vpad_host.main()`'in accept döngüsüyle aynı: bağlantı başına thread,
    aynı argümanlar. mDNS yok — keşif bu testlerin konusu değil.
    """

    def __init__(self, players: int = 1, pair: bool = False,
                 store: "devices.DeviceStore | None" = None):
        self.journal: list[tuple[str, int]] = []
        self.pads = SpyPadSet(self.journal)
        self.pool = SpyPool(players, self.journal)
        self.store = store
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port: int = self._sock.getsockname()[1]
        # Üretimdeki bilet masası — testte de aynısı, taklidi değil.
        self.desk = (vpad_host.TicketDesk("127.0.0.1", self.port, players)
                     if pair else None)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except OSError:
                return
            threading.Thread(
                target=vpad_host.handle_client,
                args=(conn, addr, self.pads, self.pool, self.desk, self.store,
                      False),
                daemon=True,
            ).start()

    def ticket_token(self) -> bytes:
        """Ekrandaki QR'ın taşıdığı bilet. Açık bilet yoksa çöp üretir —
        böylece "QR bitti" durumunu da sınayabiliyoruz."""
        ticket = self.desk.current() if self.desk is not None else None
        return ticket.token if ticket is not None else pairing.generate_token()

    def info(self, token: bytes | None = None) -> pairing.PairingInfo:
        """İstemcinin kullanacağı bağlantı bilgisi.

        Eşleşme kapalı host'ta da bir token gerekiyor (QR biçimi onu
        zorunlu kılıyor); istemci CHALLENGE gelmezse onu hiç kullanmaz.
        """
        use = token if token is not None else self.ticket_token()
        return pairing.parse_payload(
            pairing.build_payload("127.0.0.1", self.port, use))

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)

    def __enter__(self) -> "Host":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def connect(host: Host, name: str, token: bytes | None = None,
            pair_wait: float = FAST_PAIR_WAIT,
            credential: tuple[str, bytes] | None = None) -> VPadClient:
    client = VPadClient(host.info(token), name=name, credential=credential)
    client.connect(timeout=5.0, pair_wait=pair_wait)
    return client


class SlotAllocationTest(unittest.TestCase):
    """Slot dağıtımı — raporun 1. engelinin tam karşılığı."""

    def test_single_player_gets_no_slot_frame(self):
        """Tek oyuncuda telde referans daemon ile BİREBİR aynı baytlar.

        Denetimimiz dışındaki istemciler (App Store'daki iOS uygulaması)
        SLOT çerçevesiyle hiç sınanmamış olabilir; varsayılan
        yapılandırmada fazladan çerçeve göndermiyoruz. Spec §4.9.
        """
        with Host(players=1) as host:
            with connect(host, "Telefon A") as client:
                self.assertIsNone(client.slot)  # tek oyuncuda SLOT gönderilmiyor (spec §4.9)
                self.assertEqual(0, host.pool.free)

    def test_two_phones_get_distinct_slots(self):
        """Referans daemon'ın YAPAMADIĞI şey: ikinci telefon kabul ediliyor."""
        with Host(players=2) as host:
            with connect(host, "Telefon A") as a:
                with connect(host, "Telefon B") as b:
                    self.assertEqual(0, a.slot)
                    self.assertEqual(1, b.slot)
                    self.assertEqual(0, host.pool.free)

    def test_four_phones_fill_the_pool(self):
        with Host(players=4) as host:
            clients = [connect(host, f"Telefon {i}") for i in range(4)]
            try:
                self.assertEqual([0, 1, 2, 3], [c.slot for c in clients])
                self.assertEqual(0, host.pool.free)
            finally:
                for c in clients:
                    c.close()

    def test_fifth_phone_is_refused(self):
        with Host(players=4) as host:
            clients = [connect(host, f"Telefon {i}") for i in range(4)]
            try:
                with self.assertRaises(Rejected) as caught:
                    connect(host, "Telefon 5")
                self.assertEqual(vpad_host.R_IN_USE, caught.exception.reason)
            finally:
                for c in clients:
                    c.close()

    def test_second_phone_refused_when_pool_is_one(self):
        """Varsayılan tek oyuncu: bugünkü davranış birebir korunuyor."""
        with Host(players=1) as host:
            with connect(host, "Telefon A"):
                with self.assertRaises(Rejected) as caught:
                    connect(host, "Telefon B")
                self.assertEqual(vpad_host.R_IN_USE, caught.exception.reason)

    def test_slot_is_released_on_disconnect(self):
        with Host(players=1) as host:
            client = connect(host, "Telefon A")
            self.assertEqual(0, host.pool.free)
            client.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))
            # Havuz gerçekten boş: yeni telefon giriyor.
            with connect(host, "Telefon B") as b:
                self.assertIsNone(b.slot)  # tek oyuncuda SLOT gönderilmiyor (spec §4.9)

    def test_slot_is_sticky_across_reconnect(self):
        """Kopan telefon eski slotuna döner — P1 iken P3 olmaz.

        Ayırt edici kurgu: ikisi de kopunca B ÖNCE dönüyor. Yapışkanlık
        olmasaydı ilk boş slot (0) verilirdi; doğru cevap 1.
        """
        with Host(players=2) as host:
            a = connect(host, "Telefon A")
            b = connect(host, "Telefon B")
            self.assertEqual((0, 1), (a.slot, b.slot))
            a.close()
            b.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 2))

            with connect(host, "Telefon B") as b2:
                self.assertEqual(1, b2.slot)


class InjectionTest(unittest.TestCase):
    """Raporların doğru slotun enjektörüne gitmesi."""

    def test_reports_reach_the_matching_injector(self):
        with Host(players=2) as host:
            with connect(host, "Telefon A") as a, connect(host, "Telefon B") as b:
                # A: yalnız X (bayt[0] bit2). B: yalnız B tuşu (bit1).
                a.send_report(buttons=vpad_host.BTN_X)
                b.send_report(buttons=vpad_host.BTN_B)
                b.send_report(buttons=vpad_host.BTN_B)

                pad_a = host.pads.acquire(a.slot)
                pad_b = host.pads.acquire(b.slot)
                self.assertTrue(wait_until(lambda: len(pad_a.reports) >= 1))
                self.assertTrue(wait_until(lambda: len(pad_b.reports) >= 2))

                # Çapraz sızma yok: A'nın enjektörü B'nin tuşunu görmedi.
                self.assertTrue(all(r.pressed(vpad_host.BTN_X)
                                    for r in pad_a.reports))
                self.assertTrue(all(not r.pressed(vpad_host.BTN_B)
                                    for r in pad_a.reports))
                self.assertTrue(all(r.pressed(vpad_host.BTN_B)
                                    for r in pad_b.reports))

    def test_same_slot_reuses_the_same_pad(self):
        """Kopup dönen telefon aynı sanal pad'i bulur — yeniden takılmaz.

        Oyunlar pad'in çıkarılıp takılmasını "kumanda çıkarıldı" diye
        gösteriyor; yapışkan slotun tüm anlamı bunu önlemek.
        """
        with Host(players=1) as host:
            client = connect(host, "Telefon A")
            first = host.pads.acquire(0)
            client.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))
            with connect(host, "Telefon A"):
                self.assertIs(first, host.pads.acquire(0))

    def test_neutralized_before_slot_is_released(self):
        """Sıra: önce nötrle, sonra bırak.

        Tersi, o slotu devralan sonraki oyuncunun basılı bir tuşla
        başlaması demek.
        """
        with Host(players=1) as host:
            client = connect(host, "Telefon A")
            client.send_report(buttons=vpad_host.BTN_A)
            client.close()
            self.assertTrue(wait_until(lambda: ("release", 0) in host.journal))
            self.assertEqual([("reset", 0), ("release", 0)], host.journal)


def temp_store(test: unittest.TestCase, ttl_days: int = devices.TTL_DAYS,
               clock=None) -> devices.DeviceStore:
    """Testlik defter — gerçek dosya, geçici klasörde."""
    import tempfile

    directory = tempfile.mkdtemp(prefix="vpad-test-")
    test.addCleanup(shutil.rmtree, directory, ignore_errors=True)
    path = os.path.join(directory, "devices.json")
    if clock is None:
        return devices.DeviceStore(path, ttl_days=ttl_days)
    return devices.DeviceStore(path, ttl_days=ttl_days, clock=clock)


class EnrollmentTest(unittest.TestCase):
    """Kayıt bileti — QR bir erişim anahtarı değil, tek kullanımlık bilet."""

    def test_correct_ticket_enrolls_and_issues_a_credential(self):
        with Host(players=1, pair=True, store=temp_store(self)) as host:
            with connect(host, "Telefon A", pair_wait=2.0) as client:
                self.assertTrue(client.paired)
                self.assertIsNone(client.slot)  # tek oyuncuda SLOT gönderilmiyor (spec §4.9)
                # Host kimlik verdi ve istemci onu yakaladı.
                self.assertIsNotNone(client.issued_credential)
                device_id, key = client.issued_credential
                self.assertEqual(devices.DEVICE_KEY_LEN, len(key))
                self.assertEqual([device_id],
                                 [r.device_id for r in host.store.devices()])

    def test_wrong_ticket_is_rejected(self):
        with Host(players=1, pair=True, store=temp_store(self)) as host:
            good = host.ticket_token()
            wrong = bytes((good[0] ^ 0xFF,)) + good[1:]
            with self.assertRaises(Rejected) as caught:
                connect(host, "Saldırgan", token=wrong, pair_wait=2.0)
            self.assertEqual(pairing.R_AUTH_FAILED, caught.exception.reason)
            # Reddedilen deneme kayıt AÇMAMALI.
            self.assertEqual([], host.store.devices())

    def test_ticket_is_single_use(self):
        """Fotoğraflanmış QR ikinci cihazı kaydettiremez.

        Kararın tam karşılığı: `--players 2` olsa bile ilk kayıttan sonra
        AYNI bilet ölür. (Masa sıradaki bileti açar; burada bilerek eski
        token'la deniyoruz.)
        """
        with Host(players=2, pair=True, store=temp_store(self)) as host:
            stolen = host.ticket_token()
            first = connect(host, "Telefon A", token=stolen, pair_wait=2.0)
            self.addCleanup(first.close)
            self.assertIsNotNone(first.issued_credential)

            with self.assertRaises(Rejected) as caught:
                connect(host, "Telefon B", token=stolen, pair_wait=2.0)
            # Sebep `R_AUTH_FAILED`: masa ilk kayıttan sonra sıradaki
            # bileti açtığı için eski token artık HİÇBİR canlı biletle
            # eşleşmiyor. "Bilet harcandı" dalı yalnızca aynı biletin
            # eşzamanlı okutulmasında görülüyor (bkz. yarış testi).
            self.assertEqual(pairing.R_AUTH_FAILED, caught.exception.reason)
            # Asıl güvence bu: ikinci cihaz KAYDOLMADI.
            self.assertEqual(1, len(host.store.devices()))

    def test_desk_opens_a_fresh_ticket_for_the_next_device(self):
        """Dört oyuncu dört QR turu demek olmasın diye masa yenisini açıyor —
        ama her biri yine tek cihaz kaydediyor."""
        with Host(players=2, pair=True, store=temp_store(self)) as host:
            a = connect(host, "Telefon A", pair_wait=2.0)
            self.addCleanup(a.close)
            # Sıradaki bilet farklı bir token taşımalı.
            self.assertNotEqual(a.info.token, host.ticket_token())
            b = connect(host, "Telefon B", pair_wait=2.0)
            self.addCleanup(b.close)
            self.assertEqual(2, len(host.store.devices()))

    def test_desk_closes_after_players_enrollments(self):
        with Host(players=1, pair=True, store=temp_store(self)) as host:
            stolen = host.ticket_token()
            a = connect(host, "Telefon A", pair_wait=2.0)
            self.addCleanup(a.close)
            self.assertIsNone(host.desk.current(),
                              "tek oyunculu host'ta ikinci bilet açılmamalı")

            # Açık bilet kalmadığında kayıt kapısı tamamen kapalı:
            # eski QR da yenisi de içeri alamaz.
            a.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))
            with self.assertRaises(Rejected) as caught:
                connect(host, "Telefon B", token=stolen, pair_wait=2.0)
            self.assertEqual(pairing.R_AUTH_REQUIRED,
                             caught.exception.reason)
            self.assertEqual(1, len(host.store.devices()))

    def test_concurrent_scans_enroll_only_one_device(self):
        """İki telefon aynı QR'ı aynı anda okutursa yalnız biri kaydolur."""
        import sys as _sys

        interval = _sys.getswitchinterval()
        _sys.setswitchinterval(1e-6)
        self.addCleanup(_sys.setswitchinterval, interval)

        with Host(players=4, pair=True, store=temp_store(self)) as host:
            token = host.ticket_token()
            start = threading.Barrier(4)
            issued: list[tuple[str, bytes]] = []
            guard = threading.Lock()

            def racer(n: int) -> None:
                start.wait()
                try:
                    client = connect(host, f"Telefon {n}", token=token,
                                     pair_wait=2.0)
                except Exception:
                    return
                self.addCleanup(client.close)
                if client.issued_credential is not None:
                    with guard:
                        issued.append(client.issued_credential)

            threads = [threading.Thread(target=racer, args=(n,))
                       for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            self.assertEqual(1, len(issued),
                             f"tek bilet {len(issued)} cihaz kaydetti")

    def test_failed_pairing_does_not_consume_a_slot(self):
        """Güvenlik özelliği.

        Kapı slottan SONRA çalışsaydı, LAN'daki biri yanlış token'la
        bağlanıp meşru telefonu dışarıda bırakabilirdi. Tek slotlu host'ta
        beş başarısız denemeden sonra doğru bilet hâlâ girebilmeli.
        """
        with Host(players=1, pair=True, store=temp_store(self)) as host:
            good = host.ticket_token()
            wrong = bytes((good[0] ^ 0xFF,)) + good[1:]
            for _ in range(5):
                with self.assertRaises(Rejected):
                    connect(host, "Saldırgan", token=wrong, pair_wait=2.0)
            self.assertTrue(wait_until(lambda: host.pool.free == 1))

            with connect(host, "Telefon A", token=good,
                         pair_wait=2.0) as client:
                self.assertIsNone(client.slot)  # tek oyuncuda SLOT gönderilmiyor (spec §4.9)

    def test_client_without_auth_is_rejected(self):
        """Eşleşme açıkken doğrudan HELLO gönderen eski istemci.

        `VPadClient` CHALLENGE'a doğru cevap verdiği için burada ham soket
        kullanılıyor — sınanan şey tam olarak protokolü bilmeyen istemci.
        """
        with Host(players=1, pair=True, store=temp_store(self)) as host:
            sock = socket.create_connection(("127.0.0.1", host.port), 5.0)
            try:
                sock.settimeout(5.0)
                # CHALLENGE'ı oku ve YOKSAY, doğrudan HELLO gönder.
                head = sock.recv(3)
                self.assertEqual(pairing.T_CHALLENGE, head[2])
                length = head[0] | (head[1] << 8)
                sock.recv(length - 3)

                name = b"Eski Istemci"
                sock.sendall(pairing.encode_frame(
                    pairing.T_HELLO,
                    bytes([1, len(name)]) + name + bytes([0])))

                reply = sock.recv(64)
                self.assertEqual(pairing.T_REJECT, reply[2])
                self.assertEqual(pairing.R_AUTH_REQUIRED, reply[3])
            finally:
                sock.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))


class SessionContinuityTest(unittest.TestCase):
    """Oturum sürekliliği — kayıtlı cihaz QR görmeden giriyor."""

    def test_registered_device_connects_without_a_qr(self):
        store = temp_store(self)
        with Host(players=1, pair=True, store=store) as host:
            first = connect(host, "Telefon A", pair_wait=2.0)
            credential = first.issued_credential
            first.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))

            # Bilet bitti (tek oyuncu), ama kayıtlı cihaz yine de girer.
            self.assertIsNone(host.desk.current())
            with connect(host, "Telefon A", pair_wait=2.0,
                         credential=credential) as again:
                self.assertIsNone(again.slot)  # tek oyuncuda SLOT gönderilmiyor (spec §4.9)
                # RESUME yolunda yeni kimlik verilmiyor.
                self.assertIsNone(again.issued_credential)

    def test_credential_survives_a_daemon_restart(self):
        """Asıl istenen: PC kapanıp açılınca telefon QR taramasın."""
        store = temp_store(self)
        with Host(players=1, pair=True, store=store) as host:
            client = connect(host, "Telefon A", pair_wait=2.0)
            credential = client.issued_credential
            client.close()

        # Yeni host = yeni bilet, yeni port; defter aynı dosyada.
        reopened = devices.DeviceStore(store.path)
        self.assertEqual(1, len(reopened.devices()))
        with Host(players=1, pair=True, store=reopened) as host2:
            with connect(host2, "Telefon A", pair_wait=2.0,
                         credential=credential) as again:
                self.assertIsNone(again.slot)  # tek oyuncuda SLOT gönderilmiyor (spec §4.9)

    def test_unknown_device_is_told_to_rescan(self):
        """`R_DEVICE_UNKNOWN` ayrı bir sebep: istemci QR ekranına düşmeli."""
        with Host(players=1, pair=True, store=temp_store(self)) as host:
            bogus = (devices.generate_device_id(), devices.generate_device_key())
            with self.assertRaises(Rejected) as caught:
                connect(host, "Hayalet", pair_wait=2.0, credential=bogus)
            self.assertEqual(pairing.R_DEVICE_UNKNOWN, caught.exception.reason)

    def test_revoked_device_cannot_return(self):
        """Gerçek iptal yolu — kaybolan telefonun erişimi burada bitiyor."""
        store = temp_store(self)
        with Host(players=1, pair=True, store=store) as host:
            client = connect(host, "Telefon A", pair_wait=2.0)
            credential = client.issued_credential
            client.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))

            self.assertTrue(store.revoke(credential[0]))
            with self.assertRaises(Rejected) as caught:
                connect(host, "Telefon A", pair_wait=2.0,
                        credential=credential)
            self.assertEqual(pairing.R_DEVICE_UNKNOWN, caught.exception.reason)

    def test_tampered_credential_is_rejected_as_auth_failure(self):
        """Doğru kimlik, yanlış anahtar → 'yeniden kaydol' DEĞİL, 'başarısız'.

        Ayrım önemli: `R_DEVICE_UNKNOWN` istemciyi QR ekranına düşürüyor;
        yanlış anahtarda düşürmemeli, yoksa saldırgan kullanıcıya sahte
        bir "yeniden eşleş" ekranı gösterebilirdi.
        """
        with Host(players=1, pair=True, store=temp_store(self)) as host:
            client = connect(host, "Telefon A", pair_wait=2.0)
            device_id, key = client.issued_credential
            client.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))

            broken = (device_id, bytes((key[0] ^ 0xFF,)) + key[1:])
            with self.assertRaises(Rejected) as caught:
                connect(host, "Sahte", pair_wait=2.0, credential=broken)
            self.assertEqual(pairing.R_AUTH_FAILED, caught.exception.reason)

    def test_expired_device_must_rescan(self):
        """Kullanılmazsa sönme — user kararı."""
        now = [1_000_000.0]
        store = temp_store(self, ttl_days=30, clock=lambda: now[0])
        with Host(players=1, pair=True, store=store) as host:
            client = connect(host, "Telefon A", pair_wait=2.0)
            credential = client.issued_credential
            client.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))

            now[0] += 31 * 86400          # 31 gün sessizlik
            with self.assertRaises(Rejected) as caught:
                connect(host, "Telefon A", pair_wait=2.0,
                        credential=credential)
            self.assertEqual(pairing.R_DEVICE_UNKNOWN, caught.exception.reason)

    def test_use_keeps_the_device_alive(self):
        """Sayaç her bağlantıda sıfırlanıyor: 29 günde bir giren hiç sönmez."""
        now = [1_000_000.0]
        store = temp_store(self, ttl_days=30, clock=lambda: now[0])
        with Host(players=1, pair=True, store=store) as host:
            client = connect(host, "Telefon A", pair_wait=2.0)
            credential = client.issued_credential
            client.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))

            for _ in range(4):
                now[0] += 29 * 86400
                again = connect(host, "Telefon A", pair_wait=2.0,
                                credential=credential)
                again.close()
                self.assertTrue(wait_until(lambda: host.pool.free == 1))

            self.assertEqual(1, len(store.devices()))


class HandshakeTest(unittest.TestCase):
    """El sıkışmanın sınır durumları."""

    def test_version_mismatch_is_rejected_without_leaking_a_slot(self):
        with Host(players=1) as host:
            sock = socket.create_connection(("127.0.0.1", host.port), 5.0)
            try:
                sock.settimeout(5.0)
                name = b"Gelecek Istemci"
                sock.sendall(pairing.encode_frame(
                    pairing.T_HELLO,
                    bytes([99, len(name)]) + name + bytes([0])))
                reply = sock.recv(128)
                self.assertEqual(pairing.T_REJECT, reply[2])
                self.assertEqual(vpad_host.R_VERSION_MISMATCH, reply[3])
            finally:
                sock.close()
            # Erken dönüş `finally`'de NameError vermemeli ve slot
            # sızmamalı; ikisi de aynı kusurun belirtisi.
            self.assertTrue(wait_until(lambda: host.pool.free == 1))
            self.assertNotIn(("release", 0), host.journal)

    def test_non_hello_first_frame_is_rejected(self):
        with Host(players=1) as host:
            sock = socket.create_connection(("127.0.0.1", host.port), 5.0)
            try:
                sock.settimeout(5.0)
                sock.sendall(pairing.encode_frame(vpad_host.T_PING))
                reply = sock.recv(128)
                self.assertEqual(pairing.T_REJECT, reply[2])
                self.assertEqual(vpad_host.R_INTERNAL_ERROR, reply[3])
            finally:
                sock.close()
            self.assertTrue(wait_until(lambda: host.pool.free == 1))

    def test_ping_is_answered_with_pong(self):
        with Host(players=1) as host:
            with connect(host, "Telefon A") as client:
                client.ping()   # cevap gelmezse yükselir


class ReportDecodeTest(unittest.TestCase):
    """Tel biçimi — belge değil, yayındaki Android istemcisi hakem.

    `docs/companion-daemon.md` §4.4 hâlâ "signed int8, +Y = yukarı" diyor;
    bu bayat. 2026-06-13'ten beri tel işaretsiz 0..255, merkez 128, +Y
    aşağı (`HidReportSender.kt` satır 14-21). Bu testler o dönüşü
    çiviliyor ki bir sonraki host yazarı belgeye kanıp Y'yi ters
    çevirmesin.
    """

    def test_center_is_128(self):
        report = vpad_host.Report.decode(bytes([0, 0, 128, 128, 128, 128, 0, 0]))
        self.assertEqual(128, report.lx)
        self.assertEqual(0, vpad_host.VigemInjector._to_short(report.lx))

    def test_axis_extremes_map_to_short_range(self):
        to_short = vpad_host.VigemInjector._to_short
        self.assertEqual(-32768, to_short(0))
        # 127*258 = 32766, 32767 değil. Referans daemon'ın yorumu "32767"
        # diyor ve yanlış; ölçeğin artığı. Kırpma yine de tutuyor.
        self.assertEqual(32766, to_short(255))

    def test_y_is_inverted_for_xinput(self):
        """Telde +Y aşağı, X360'ta +Y yukarı."""
        invert = vpad_host.VigemInjector._to_short_inverted
        # Çubuk aşağı itilmiş (255) → XInput'ta negatif olmalı.
        self.assertLess(invert(255), 0)
        self.assertGreater(invert(0), 0)
        self.assertEqual(0, invert(128))
        # -(-32768) taşmıyor.
        self.assertEqual(32767, invert(0))

    def test_hat_nibble_is_the_high_four_bits(self):
        report = vpad_host.Report.decode(bytes([0, 0x40, 128, 128, 128, 128, 0, 0]))
        self.assertEqual(4, report.hat)          # 0x40 >> 4 = güney
        self.assertEqual((False, False, True, False),
                         vpad_host.HAT_VECTORS[report.hat])

    def test_wrong_length_is_rejected(self):
        with self.assertRaises(ValueError):
            vpad_host.Report.decode(bytes(7))


class ServiceNameTest(unittest.TestCase):
    """mDNS ad hijyeni — sessizce görünmez olmanın en kolay yolu."""

    def test_dots_become_hyphens(self):
        # Nokta fazladan DNS etiketi üretir, NWBrowser kaydı sessizce atar.
        self.assertEqual("host-home", vpad_host.sanitize_instance("host.home"))

    def test_long_name_is_truncated_on_a_character_boundary(self):
        name = "ü" * 60          # UTF-8'de 120 bayt
        result = vpad_host.sanitize_instance(name)
        self.assertLessEqual(len(result.encode("utf-8")), 63)
        self.assertNotIn("�", result)

    def test_empty_name_falls_back(self):
        self.assertEqual("V-Pad host", vpad_host.sanitize_instance("   "))


class FrameCodecTest(unittest.TestCase):
    """Çerçeveleme — istemciyle aynı tel biçimi."""

    def test_hello_ack_layout(self):
        frame = vpad_host.encode_hello_ack(1, accept=True)
        length, msg_type = struct.unpack("<HB", frame[:3])
        self.assertEqual(5, length)
        self.assertEqual(pairing.T_HELLO_ACK, msg_type)
        self.assertEqual(bytes([1, 1]), frame[3:])

    def test_hello_round_trip(self):
        name, skin = "Gamepad ++ Classic", "classic"
        payload = (bytes([1, len(name.encode())]) + name.encode()
                   + bytes([len(skin.encode())]) + skin.encode())
        self.assertEqual((1, name, skin), vpad_host.decode_hello(payload))

    def test_truncated_hello_raises(self):
        with self.assertRaises(ValueError):
            vpad_host.decode_hello(bytes([1, 40]) + b"kisa")


if __name__ == "__main__":
    unittest.main(verbosity=2)
