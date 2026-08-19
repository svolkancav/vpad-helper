#!/usr/bin/env python3
"""
vpad_devices birim testleri — dosya, kripto, çerçeve sınırları.

`test_e2e_host.py` akışı gerçek soket üzerinde sınıyor; burada onun
göremediği yerler var: bozuk defter dosyası, atomik yazma, süre
hesabının sınırları, çerçeve ayrıştırıcısının kırık girdiye tepkisi.

    python -m unittest test_vpad_devices -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import vpad_devices as d          # noqa: E402
import vpad_pairing as p          # noqa: E402

DAY = 86400


class StoreTestCase(unittest.TestCase):
    """Her testin kendi geçici defteri."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="vpad-dev-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "devices.json")
        self.now = [1_000_000.0]

    def store(self, ttl_days: int = d.TTL_DAYS) -> d.DeviceStore:
        return d.DeviceStore(self.path, ttl_days=ttl_days,
                             clock=lambda: self.now[0])


class CryptoTests(unittest.TestCase):

    def test_resume_mac_is_deterministic_and_key_bound(self):
        key = d.generate_device_key()
        challenge = p.make_challenge()
        nonce = b"\x01" * p.NONCE_LEN
        first = d.compute_resume(key, challenge, nonce)
        self.assertEqual(first, d.compute_resume(key, challenge, nonce))
        self.assertNotEqual(first, d.compute_resume(
            d.generate_device_key(), challenge, nonce))

    def test_resume_mac_is_bound_to_the_challenge(self):
        """Replay koruması: başka bağlantının MAC'i burada geçmemeli."""
        key = d.generate_device_key()
        nonce = b"\x02" * p.NONCE_LEN
        one = d.compute_resume(key, p.make_challenge(), nonce)
        two = d.compute_resume(key, p.make_challenge(), nonce)
        self.assertNotEqual(one, two)

    def test_resume_label_differs_from_enrollment_label(self):
        """Alan ayrımı.

        Kayıt biletinin MAC'i ile cihaz anahtarının MAC'i aynı formülü
        kullansaydı, bir bağlamda yakalanan MAC diğerinde oynatılabilirdi.
        16 baytlık token ve 32 baytlık anahtar farklı uzunlukta olduğu için
        doğrudan karşılaştıramıyoruz; etiketin ayrı olduğunu doğruluyoruz.
        """
        self.assertNotEqual(d.RESUME_LABEL, p.AUTH_LABEL)

    def test_wrong_key_length_refused(self):
        for bad in (b"", b"x" * 16, b"x" * 31, b"x" * 33):
            with self.assertRaises(d.DeviceError):
                d.compute_resume(bad, p.make_challenge(), b"n" * p.NONCE_LEN)


class FrameTests(unittest.TestCase):

    def test_resume_round_trip(self):
        key = d.generate_device_key()
        challenge = p.make_challenge()
        device_id = d.generate_device_id()
        body = d.build_resume_body(device_id, key, challenge)
        got_id, nonce, mac = d.parse_resume_body(body)
        self.assertEqual(device_id, got_id)
        self.assertEqual(mac, d.compute_resume(key, challenge, nonce))

    def test_credential_round_trip(self):
        device_id, key = d.generate_device_id(), d.generate_device_key()
        frame = d.encode_credential(device_id, key)
        self.assertEqual(p.T_CREDENTIAL, frame[2])
        self.assertEqual((device_id, key), d.parse_credential(frame[3:]))

    def test_frame_type_codes_are_free(self):
        """Yeni tipler spec'in sahiplendiği hiçbir kodla çakışmamalı."""
        taken = {0x01, 0x02, 0x03, 0x04, 0x05,      # C→S
                 0x10, 0x11, 0x12, 0x20,            # S→C (+RUMBLE ayrılmış)
                 p.T_CHALLENGE, p.T_AUTH, p.T_SLOT}
        self.assertNotIn(p.T_RESUME, taken)
        self.assertNotIn(p.T_CREDENTIAL, taken)
        self.assertNotEqual(p.T_RESUME, p.T_CREDENTIAL)

    def test_reject_reason_is_distinct(self):
        """`R_DEVICE_UNKNOWN` istemciyi QR ekranına düşürüyor; `R_AUTH_FAILED`
        düşürmemeli. İkisinin ayrı kalması davranışsal bir gereklilik."""
        self.assertNotIn(p.R_DEVICE_UNKNOWN,
                         {p.R_AUTH_FAILED, p.R_AUTH_REQUIRED,
                          0x01, 0x02, 0x03, 0xFF})

    def test_truncated_bodies_raise(self):
        key = d.generate_device_key()
        challenge = p.make_challenge()
        body = d.build_resume_body("abcd", key, challenge)
        for cut in (0, 1, len(body) - 1):
            with self.assertRaises(d.DeviceError):
                d.parse_resume_body(body[:cut])
        with self.assertRaises(d.DeviceError):
            d.parse_resume_body(body + b"\x00")     # fazla bayt da bozuk

    def test_zero_length_id_refused(self):
        with self.assertRaises(d.DeviceError):
            d.parse_resume_body(bytes([0]) + b"\x00" * 48)
        with self.assertRaises(d.DeviceError):
            d.parse_credential(bytes([0]) + b"\x00" * 32)


class TicketTests(unittest.TestCase):

    def test_only_the_first_caller_spends_it(self):
        ticket = d.Ticket()
        self.assertFalse(ticket.spent)
        self.assertTrue(ticket.try_spend())
        self.assertTrue(ticket.spent)
        self.assertFalse(ticket.try_spend())

    def test_concurrent_spend_has_exactly_one_winner(self):
        interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, interval)

        for _ in range(50):
            ticket = d.Ticket()
            start = threading.Barrier(8)
            wins: list[bool] = []
            guard = threading.Lock()

            def racer() -> None:
                start.wait()
                if ticket.try_spend():
                    with guard:
                        wins.append(True)

            threads = [threading.Thread(target=racer) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            self.assertEqual(1, len(wins))


class EnrollAndResolveTests(StoreTestCase):

    def test_enroll_creates_a_resolvable_record(self):
        store = self.store()
        record = store.enroll("Telefon A")
        self.assertEqual(d.DEVICE_KEY_LEN, len(record.key))
        self.assertEqual(record.device_id, store.resolve(record.device_id).device_id)

    def test_ids_and_keys_are_unique(self):
        store = self.store()
        records = [store.enroll("") for _ in range(20)]
        self.assertEqual(20, len({r.device_id for r in records}))
        self.assertEqual(20, len({r.key for r in records}))

    def test_unknown_id_resolves_to_none(self):
        self.assertIsNone(self.store().resolve("yok-boyle-bir-kimlik"))

    def test_revoke_removes_access(self):
        store = self.store()
        record = store.enroll("")
        self.assertTrue(store.revoke(record.device_id))
        self.assertIsNone(store.resolve(record.device_id))
        self.assertFalse(store.revoke(record.device_id))

    def test_revoke_all(self):
        store = self.store()
        for _ in range(3):
            store.enroll("")
        self.assertEqual(3, store.revoke_all())
        self.assertEqual([], store.devices())


class ExpiryTests(StoreTestCase):

    def test_record_expires_after_ttl(self):
        store = self.store(ttl_days=30)
        record = store.enroll("")
        self.now[0] += 31 * DAY
        self.assertIsNone(store.resolve(record.device_id))

    def test_record_survives_just_under_ttl(self):
        store = self.store(ttl_days=30)
        record = store.enroll("")
        self.now[0] += 30 * DAY - 1
        self.assertIsNotNone(store.resolve(record.device_id))

    def test_touch_resets_the_clock(self):
        """Sayaç her bağlantıda sıfırlanıyor — bu bilinçli bir takas."""
        store = self.store(ttl_days=30)
        record = store.enroll("")
        for _ in range(10):
            self.now[0] += 29 * DAY
            store.touch(record.device_id)
        self.now[0] += 29 * DAY
        self.assertIsNotNone(store.resolve(record.device_id))

    def test_ttl_zero_never_expires(self):
        store = self.store(ttl_days=0)
        record = store.enroll("")
        self.now[0] += 3650 * DAY
        self.assertIsNotNone(store.resolve(record.device_id))

    def test_expired_record_is_deleted_not_just_hidden(self):
        store = self.store(ttl_days=30)
        record = store.enroll("")
        self.now[0] += 31 * DAY
        store.resolve(record.device_id)          # burada silinmeli
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual([], saved["devices"])

    def test_sweep_reports_and_removes(self):
        store = self.store(ttl_days=30)
        old = store.enroll("")
        self.now[0] += 31 * DAY
        fresh = store.enroll("")
        self.assertEqual(1, store.sweep())
        self.assertEqual([fresh.device_id],
                         [r.device_id for r in store.devices()])
        self.assertIsNone(store.resolve(old.device_id))


class PersistenceTests(StoreTestCase):

    def test_records_survive_reopening(self):
        store = self.store()
        record = store.enroll("Telefon A")
        again = d.DeviceStore(self.path, clock=lambda: self.now[0])
        found = again.resolve(record.device_id)
        self.assertIsNotNone(found)
        self.assertEqual(record.key, found.key)
        self.assertEqual("Telefon A", found.name)

    def test_missing_file_is_an_empty_store(self):
        store = d.DeviceStore(os.path.join(self.dir, "yok.json"))
        self.assertEqual([], store.devices())

    def test_corrupt_file_does_not_crash_the_daemon(self):
        """Bozuk defterle açılmamak, hiç açılmamaktan kötü.

        Kullanıcı QR'ı yeniden tarayıp devam edebilmeli; daemon'ın
        başlamaması ise oyunu tamamen durdurur.
        """
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{bu json degil")
        store = d.DeviceStore(self.path)
        self.assertEqual([], store.devices())
        record = store.enroll("")               # üstüne yazabilmeli
        self.assertIsNotNone(store.resolve(record.device_id))

    def test_unknown_version_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"version": 999, "devices": [{"id": "x"}]}, handle)
        self.assertEqual([], d.DeviceStore(self.path).devices())

    def test_one_broken_row_does_not_drop_the_others(self):
        good_id, good_key = d.generate_device_id(), d.generate_device_key()
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({
                "version": d.STORE_VERSION,
                "devices": [
                    {"id": "bozuk", "key": "ZZZZ"},          # hex değil
                    {"id": "eksik"},                          # anahtar yok
                    {"id": good_id, "key": good_key.hex(),
                     "name": "", "created": 1.0, "last_seen": 1.0},
                ],
            }, handle)
        store = d.DeviceStore(self.path, ttl_days=0)
        self.assertEqual([good_id], [r.device_id for r in store.devices()])

    def test_write_leaves_no_temp_file_behind(self):
        store = self.store()
        store.enroll("")
        leftovers = [f for f in os.listdir(self.dir) if f.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def test_revoke_from_another_process_is_not_resurrected(self):
        """`--forget` çalışan daemon'ın belleği tarafından geri alınmamalı.

        Gerçek kusurun testi. `--forget` ayrı bir süreçte, ayrı bir
        `DeviceStore` ile dosyayı düzenliyor. Daemon'ın kopyası defteri
        yeniden okumadan yazdığı sürece bir sonraki `touch` silinen kaydı
        **geri diriltiyordu** — iptal edilen telefon yeniden bağlanabildi.
        Birim testleri kaçırmıştı; elle CLI denemesi yakaladı.
        """
        daemon = self.store()
        record = daemon.enroll("Telefon A")

        # Ayrı süreç: `--forget`
        cli = d.DeviceStore(self.path, clock=lambda: self.now[0])
        self.assertTrue(cli.revoke(record.device_id))

        # Daemon tarafı artık onu görmemeli...
        self.assertIsNone(daemon.resolve(record.device_id))
        # ...ve yazma işlemi dosyaya geri koymamalı.
        daemon.touch(record.device_id)
        daemon.enroll("Telefon B")
        self.assertNotIn(record.device_id,
                         [r.device_id for r in
                          d.DeviceStore(self.path).devices()])

    def test_external_enrollment_becomes_visible(self):
        """Tazeleme çift yönlü: başka süreç kaydettiyse daemon görmeli."""
        daemon = self.store()
        other = d.DeviceStore(self.path, clock=lambda: self.now[0])
        record = other.enroll("Telefon A")
        self.assertIsNotNone(daemon.resolve(record.device_id))

    def test_externally_deleted_file_empties_the_store(self):
        store = self.store()
        store.enroll("")
        os.remove(self.path)
        self.assertEqual([], store.devices())

    def test_store_creates_missing_directories(self):
        nested = os.path.join(self.dir, "a", "b", "devices.json")
        store = d.DeviceStore(nested)
        record = store.enroll("")
        self.assertTrue(os.path.exists(nested))
        self.assertIsNotNone(
            d.DeviceStore(nested).resolve(record.device_id))


class AccessGateTests(StoreTestCase):
    """Kapının kendisi — soket olmadan."""

    def test_disabled_gate_lets_everything_through(self):
        gate = d.AccessGate(None, None)
        self.assertFalse(gate.enabled)
        self.assertIsNone(gate.opening_frame())
        self.assertTrue(gate.accept(p.T_HELLO, b"").ok)

    def test_enrollment_issues_a_credential(self):
        store = self.store()
        ticket = d.Ticket()
        gate = d.AccessGate(ticket, store)
        opening = gate.opening_frame()
        self.assertEqual(p.T_CHALLENGE, opening[2])

        body = p.build_auth_body(ticket.token, gate.challenge)
        outcome = gate.accept(p.T_AUTH, body)
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.enrolled)
        # QR token'ıyla kaydolan istemci SARMALANMIŞ kimlik alır (0x16):
        # token yolundan geldiyse v2 QR'ı ayrıştırabilmiş demektir.
        self.assertEqual(p.T_CREDENTIAL_ENC, outcome.credential[2])
        self.assertTrue(ticket.spent)

        # Anahtar telde DÜZ GEÇMİYOR — bu testin asıl konusu bu.
        self.assertNotIn(outcome.record.key, outcome.credential)
        # Ama doğru sırla açılıyor ve içinden bugünkü gövde çıkıyor.
        body = p.unwrap_credential(
            ticket.token, gate.challenge, outcome.credential[3:])
        self.assertIsNotNone(body)
        self.assertEqual(
            (outcome.record.device_id, outcome.record.key),
            d.parse_credential(body))
        # Yanlış sır açamaz.
        self.assertIsNone(p.unwrap_credential(
            bytes(p.TOKEN_LEN), gate.challenge, outcome.credential[3:]))

    def test_manual_code_enrollment_stays_on_the_legacy_frame(self):
        """6 haneli kod yolu SARMALANMIYOR — bilinçli sınır.

        O sır ~20 bit; sarmalamak pasif dinleyiciye karşı korumazdı
        (çevrimdışı 10^6 deneme). Ayrıca kod yolundan gelen istemcinin
        QR'ı ayrıştırdığına dair bir kanıt yok, yani 0x16'yı çözebileceğini
        de bilmiyoruz.
        """
        store = self.store()
        ticket = d.Ticket()
        gate = d.AccessGate(ticket, store)
        gate.opening_frame()

        secret = p.secret_from_code(ticket.code)
        outcome = gate.accept(
            p.T_AUTH, p.build_auth_body(secret, gate.challenge))
        self.assertTrue(outcome.ok)
        self.assertEqual(p.T_CREDENTIAL, outcome.credential[2])
        self.assertEqual(
            (outcome.record.device_id, outcome.record.key),
            d.parse_credential(outcome.credential[3:]))

    def test_enrollment_without_store_grants_no_continuity(self):
        """`--no-remember`: bilet doğrulanır, kimlik verilmez."""
        ticket = d.Ticket()
        gate = d.AccessGate(ticket, None)
        gate.opening_frame()
        outcome = gate.accept(
            p.T_AUTH, p.build_auth_body(ticket.token, gate.challenge))
        self.assertTrue(outcome.ok)
        self.assertIsNone(outcome.credential)

    def test_resume_after_enrollment(self):
        store = self.store()
        ticket = d.Ticket()
        gate = d.AccessGate(ticket, store)
        gate.opening_frame()
        first = gate.accept(
            p.T_AUTH, p.build_auth_body(ticket.token, gate.challenge))
        record = first.record

        gate2 = d.AccessGate(None, store)
        gate2.opening_frame()
        outcome = gate2.accept(p.T_RESUME, d.build_resume_body(
            record.device_id, record.key, gate2.challenge))
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.enrolled)
        self.assertIsNone(outcome.credential)

    def test_resume_without_store_is_device_unknown(self):
        gate = d.AccessGate(d.Ticket(), None)
        gate.opening_frame()
        outcome = gate.accept(p.T_RESUME, d.build_resume_body(
            "abcd", d.generate_device_key(), gate.challenge))
        self.assertFalse(outcome.ok)
        self.assertEqual(p.R_DEVICE_UNKNOWN, outcome.reject[3])

    def test_garbage_resume_is_auth_failure(self):
        gate = d.AccessGate(None, self.store())
        gate.opening_frame()
        outcome = gate.accept(p.T_RESUME, b"\x04abcd")
        self.assertFalse(outcome.ok)
        self.assertEqual(p.R_AUTH_FAILED, outcome.reject[3])

    def test_hello_first_is_auth_required(self):
        gate = d.AccessGate(d.Ticket(), self.store())
        gate.opening_frame()
        outcome = gate.accept(p.T_HELLO, b"")
        self.assertFalse(outcome.ok)
        self.assertEqual(p.R_AUTH_REQUIRED, outcome.reject[3])

    def test_gate_refuses_to_be_reused(self):
        gate = d.AccessGate(d.Ticket(), None)
        gate.opening_frame()
        gate.accept(p.T_HELLO, b"")
        with self.assertRaises(d.DeviceError):
            gate.accept(p.T_HELLO, b"")

    def test_accept_before_opening_raises(self):
        gate = d.AccessGate(d.Ticket(), None)
        with self.assertRaises(d.DeviceError):
            gate.accept(p.T_AUTH, b"")

    def test_each_connection_gets_a_new_challenge(self):
        ticket = d.Ticket()
        seen = set()
        for _ in range(10):
            gate = d.AccessGate(ticket, None)
            gate.opening_frame()
            seen.add(gate.challenge)
        self.assertEqual(10, len(seen))

    def test_spent_ticket_cannot_enroll_again(self):
        store = self.store()
        ticket = d.Ticket()
        gate = d.AccessGate(ticket, store)
        gate.opening_frame()
        gate.accept(p.T_AUTH, p.build_auth_body(ticket.token, gate.challenge))

        gate2 = d.AccessGate(ticket, store)
        gate2.opening_frame()
        outcome = gate2.accept(
            p.T_AUTH, p.build_auth_body(ticket.token, gate2.challenge))
        self.assertFalse(outcome.ok)
        self.assertEqual(p.R_AUTH_REQUIRED, outcome.reject[3])
        self.assertEqual(1, len(store.devices()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
