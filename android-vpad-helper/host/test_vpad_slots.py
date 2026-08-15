#!/usr/bin/env python3
"""
vpad_slots birim testleri.

Koşturma:
    python -m unittest discover -s host -v

Üçüncü parti bağımlılık yok.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_pairing as p  # noqa: E402
import vpad_slots as s  # noqa: E402


class PoolSizeTests(unittest.TestCase):

    def test_default_is_single_player(self):
        """Varsayılan 1: `--players` verilmezse davranış bugünküyle aynı."""
        self.assertEqual(s.SlotPool().size, 1)

    def test_size_bounds(self):
        for bad in (0, -1, 5, 99):
            with self.assertRaises(s.SlotError, msg=f"{bad} kabul edildi"):
                s.SlotPool(bad)
        for good in (1, 2, 3, 4):
            self.assertEqual(s.SlotPool(good).size, good)


class AcquireReleaseTests(unittest.TestCase):

    def test_four_clients_get_four_distinct_slots(self):
        pool = s.SlotPool(4)
        leases = [pool.acquire(f"telefon-{i}") for i in range(4)]
        self.assertEqual([l.index for l in leases], [0, 1, 2, 3])
        self.assertEqual(pool.free, 0)

    def test_fifth_client_is_refused(self):
        """XInput tavanı: 5. istemci slot bulamaz → daemon in_use döner."""
        pool = s.SlotPool(4)
        for i in range(4):
            pool.acquire(f"telefon-{i}")
        self.assertIsNone(pool.acquire("bes"))

    def test_single_player_pool_refuses_second(self):
        pool = s.SlotPool(1)
        self.assertIsNotNone(pool.acquire("bir"))
        self.assertIsNone(pool.acquire("iki"),
                          "tek oyunculu havuz ikinci istemciyi kabul etti")

    def test_release_frees_the_slot(self):
        pool = s.SlotPool(2)
        first = pool.acquire("bir")
        pool.acquire("iki")
        self.assertIsNone(pool.acquire("uc"))
        pool.release(first)
        self.assertEqual(pool.acquire("uc").index, 0)

    def test_double_release_is_harmless(self):
        """Kopma yollarında `finally` iki kez çalışabiliyor."""
        pool = s.SlotPool(2)
        lease = pool.acquire("bir")
        pool.release(lease)
        pool.release(lease)  # patlamamalı
        self.assertEqual(pool.free, 2)

    def test_stale_release_does_not_evict_the_new_owner(self):
        """Eski lease, aynı slotu kapmış YENİ istemciyi düşürmemeli."""
        pool = s.SlotPool(1)
        old = pool.acquire("eski")
        pool.release(old)
        new = pool.acquire("yeni")
        pool.release(old)  # gecikmiş temizlik
        self.assertEqual(pool.occupants(), {0: "yeni"})
        self.assertEqual(new.index, 0)

    def test_empty_key_refused(self):
        with self.assertRaises(s.SlotError):
            s.SlotPool(2).acquire("")


class StickinessTests(unittest.TestCase):

    def test_returning_client_gets_its_old_slot(self):
        """GERİLEME: kopup dönen telefon P1 iken P3 olmamalı."""
        pool = s.SlotPool(4)
        a = pool.acquire("a")
        b = pool.acquire("b")
        self.assertEqual([a.index, b.index], [0, 1])

        pool.release(a)          # 1. oyuncu koptu
        c = pool.acquire("c")    # araya yeni biri girdi
        self.assertEqual(c.index, 0, "boş slot yeniden kullanılmalı")

        pool.release(c)
        again = pool.acquire("a")  # 1. oyuncu geri döndü
        self.assertEqual(again.index, 0, "yapışkanlık çalışmadı")

    def test_sticky_slot_is_not_reserved(self):
        """Dönmeyen oyuncu slotu sonsuza kilitlemez."""
        pool = s.SlotPool(2)
        a = pool.acquire("a")
        pool.release(a)
        # a dönmedi; iki yeni istemci iki slotu da alabilmeli
        self.assertEqual(pool.acquire("b").index, 0)
        self.assertEqual(pool.acquire("c").index, 1)
        self.assertIsNone(pool.acquire("a"), "a artık sıra beklemeli")

    def test_taken_sticky_slot_falls_back_to_first_free(self):
        pool = s.SlotPool(2)
        a = pool.acquire("a")       # slot 0
        pool.release(a)
        pool.acquire("b")           # slot 0'ı kaptı
        again = pool.acquire("a")   # eski slotu dolu
        self.assertEqual(again.index, 1)


class SlotFrameTests(unittest.TestCase):

    def test_slot_frame_bytes(self):
        frame = p.encode_slot(2)
        self.assertEqual(len(frame), 4)          # 3 başlık + 1 gövde
        self.assertEqual(frame[0], 4)
        self.assertEqual(frame[1], 0)
        self.assertEqual(frame[2], p.T_SLOT)
        self.assertEqual(frame[3], 2)

    def test_slot_type_code_is_pinned_and_free(self):
        """0x14, spec'in sahiplendiği hiçbir kodla çakışmamalı."""
        self.assertEqual(p.T_SLOT, 0x14)
        self.assertNotIn(p.T_SLOT, {
            0x01, 0x02, 0x03, 0x04, 0x05,   # C→S
            0x10, 0x11, 0x12,               # S→C
            0x20,                           # RUMBLE, v3 için ayrılmış
            p.T_CHALLENGE, p.T_AUTH,
        })

    def test_out_of_range_slot_refused(self):
        for bad in (-1, 4, 255):
            with self.assertRaises(p.PairingError):
                p.encode_slot(bad)


class ConcurrencyTests(unittest.TestCase):
    """Havuz bağlantı başına bir thread'den çağrılıyor.

    `acquire` bir slotu ÖNCE boş diye okuyup SONRA sahipleniyor. Bu iki
    adımın arasına thread değişimi girerse iki telefon aynı slotu alır ve
    biri sessizce diğerinin sanal pad'ine yazar — üstelik başka bir slot
    boş dururken. GIL bileşik işlemi korumaz.
    """

    # Yarışın penceresi birkaç bytecode genişliğinde; varsayılan 5 ms'lik
    # thread değişim aralığıyla neredeyse hiç yakalanmaz. Aralığı kısmak
    # testin dişini takıyor: kilit kaldırıldığında 40 turun ~10'u
    # ihlal veriyor (ölçüldü: kilitsiz 4000 turda 951 ihlal, kilitli 0).
    ROUNDS = 40
    RACERS = 16

    def setUp(self) -> None:
        self._switch_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)

    def tearDown(self) -> None:
        sys.setswitchinterval(self._switch_interval)

    def test_concurrent_acquire_never_double_allocates(self):
        import threading

        for round_no in range(self.ROUNDS):
            pool = s.SlotPool(s.MAX_SLOTS)
            start = threading.Barrier(self.RACERS)
            leases: list[s.SlotLease] = []
            guard = threading.Lock()

            def racer(n: int) -> None:
                start.wait()                   # hepsi aynı anda dalsın
                lease = pool.acquire(f"telefon-{n}")
                if lease is not None:
                    with guard:
                        leases.append(lease)

            threads = [threading.Thread(target=racer, args=(n,))
                       for n in range(self.RACERS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            indexes = sorted(lease.index for lease in leases)
            # Tavan kadar lease verilmeli, fazlası None almalı; ve hiçbir
            # indeks iki kez dağıtılmamalı.
            self.assertEqual(list(range(s.MAX_SLOTS)), indexes,
                             f"tur {round_no}: slot iki kez dağıtıldı")
            self.assertEqual(0, pool.free)

    def test_concurrent_release_returns_every_slot(self):
        import threading

        pool = s.SlotPool(s.MAX_SLOTS)
        leases = [pool.acquire(f"telefon-{n}") for n in range(s.MAX_SLOTS)]
        start = threading.Barrier(s.MAX_SLOTS)

        def releaser(lease: s.SlotLease) -> None:
            start.wait()
            pool.release(lease)
            pool.release(lease)                # çift bırakma zararsız olmalı

        threads = [threading.Thread(target=releaser, args=(lease,))
                   for lease in leases]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(s.MAX_SLOTS, pool.free)
        self.assertEqual({}, pool.occupants())


if __name__ == "__main__":
    unittest.main(verbosity=2)
