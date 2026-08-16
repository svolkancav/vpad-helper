"""6 haneli elle giriş kodu — QR'ın eşdeğeri, yedeği değil.

Kamerası olmayan cihaz (Play services'siz Android, kırık kamera) host'a
başka türlü giremiyor. Sınanan iddialar:

  * kod QR ile AYNI bilete bağlı; biri harcanınca ikisi de ölüyor
  * kodla kaydolan cihaz da kimlik alıyor (yani sonraki açılışta QR'sız
    dönebiliyor) — yarım bir giriş yolu değil
  * yanlış kod sayılıyor ve birkaç denemeden sonra KOD kilitleniyor
  * kilit QR'a DOKUNMUYOR — yoksa LAN'daki biri beş yanlış denemeyle
    meşru telefonun QR'ını öldürebilirdi (hizmet dışı bırakma)
  * 6 hanenin zayıflığını kapatan şey bu sayaç, türetme değil
"""
import os
import tempfile
import unittest

import vpad_devices as devices
import vpad_pairing as pairing


def auth_body(secret: bytes, challenge: bytes) -> bytes:
    return pairing.build_auth_body(secret, challenge)


class CodeShape(unittest.TestCase):
    def test_six_digits(self):
        for _ in range(200):
            code = pairing.generate_code()
            self.assertEqual(len(code), pairing.CODE_LEN)
            self.assertTrue(code.isdigit(), code)

    def test_leading_zero_survives(self):
        """Kod bir SAYI değil, altı karakterlik dize.

        `int()`'ten geçirilirse '004321' → 4321 olur ve host'takiyle asla
        eşleşmez. Türetme dizeyle çalışıyor.
        """
        self.assertNotEqual(pairing.secret_from_code("004321"),
                            pairing.secret_from_code("432100"))
        self.assertEqual(len(pairing.secret_from_code("000000")),
                         pairing.TOKEN_LEN)

    def test_rejects_malformed(self):
        for bad in ("12345", "1234567", "12345a", "", "12 456", "١٢٣٤٥٦"):
            with self.assertRaises(pairing.PairingError, msg=bad):
                pairing.secret_from_code(bad)

    def test_domain_separated_from_token(self):
        """Kod anahtarı, aynı baytların token olarak okunmasıyla çakışmasın."""
        code = "123456"
        self.assertNotEqual(pairing.secret_from_code(code),
                            code.encode().ljust(pairing.TOKEN_LEN, b"\0"))


class CodeEnrolment(unittest.TestCase):
    def setUp(self):
        # Gerçek dosya: `DeviceStore` diske yazıyor ve kaydın gerçekten
        # yazılması testin konusu. Her test kendi klasörünü alıyor.
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = devices.DeviceStore(
            path=os.path.join(self._dir.name, "devices.json"), ttl_days=0)

    def test_code_enrols_like_qr(self):
        ticket = devices.Ticket()
        gate = devices.AccessGate(ticket, self.store)
        gate.opening_frame()
        secret = pairing.secret_from_code(ticket.code)
        out = gate.accept(pairing.T_AUTH, auth_body(secret, gate.challenge))
        self.assertTrue(out.ok)
        self.assertTrue(out.enrolled)
        # Kimlik ŞART: kodla giren cihaz da sonraki açılışta QR'sız dönmeli.
        self.assertIsNotNone(out.credential)
        self.assertIsNotNone(out.record)

    def test_token_still_works(self):
        ticket = devices.Ticket()
        gate = devices.AccessGate(ticket, self.store)
        gate.opening_frame()
        out = gate.accept(pairing.T_AUTH,
                          auth_body(ticket.token, gate.challenge))
        self.assertTrue(out.ok)

    def test_code_and_qr_share_one_ticket(self):
        """QR harcanınca kod da ölmeli — iki ayrı giriş değil, tek bilet."""
        ticket = devices.Ticket()
        first = devices.AccessGate(ticket, self.store)
        first.opening_frame()
        self.assertTrue(first.accept(
            pairing.T_AUTH, auth_body(ticket.token, first.challenge)).ok)

        second = devices.AccessGate(ticket, self.store)
        second.opening_frame()
        out = second.accept(pairing.T_AUTH, auth_body(
            pairing.secret_from_code(ticket.code), second.challenge))
        self.assertFalse(out.ok)

    def test_wrong_code_locks_the_code_after_limit(self):
        ticket = devices.Ticket()
        wrong = pairing.secret_from_code("000000")
        if ticket.code == "000000":            # astronomik ama ücretsiz
            ticket.code = "111111"

        for attempt in range(1, devices.Ticket.MAX_CODE_ATTEMPTS):
            gate = devices.AccessGate(ticket, self.store)
            gate.opening_frame()
            out = gate.accept(pairing.T_AUTH, auth_body(wrong, gate.challenge))
            self.assertFalse(out.ok)
            self.assertFalse(out.code_locked, f"{attempt}. denemede kilitlendi")
            self.assertFalse(ticket.code_locked)

        gate = devices.AccessGate(ticket, self.store)
        gate.opening_frame()
        out = gate.accept(pairing.T_AUTH, auth_body(wrong, gate.challenge))
        self.assertFalse(out.ok)
        self.assertTrue(out.code_locked, "sınırda kod kilitlenmedi")
        self.assertTrue(ticket.code_locked)
        self.assertFalse(ticket.spent, "kilit bileti de harcadı")

    def test_locked_code_refuses_the_right_code_too(self):
        """Kilit gerçek olmalı: doğru kod bile artık geçmemeli."""
        ticket = devices.Ticket()
        wrong = pairing.secret_from_code("000000" if ticket.code != "000000"
                                         else "111111")
        for _ in range(devices.Ticket.MAX_CODE_ATTEMPTS):
            gate = devices.AccessGate(ticket, self.store)
            gate.opening_frame()
            gate.accept(pairing.T_AUTH, auth_body(wrong, gate.challenge))

        gate = devices.AccessGate(ticket, self.store)
        gate.opening_frame()
        out = gate.accept(pairing.T_AUTH, auth_body(
            pairing.secret_from_code(ticket.code), gate.challenge))
        self.assertFalse(out.ok, "kilitli kod doğru değeri kabul etti")

    def test_correct_code_resets_nothing_but_succeeds_within_limit(self):
        """Birkaç yanlıştan sonra doğru kod hâlâ girebilmeli.

        Kullanıcı ekrandan yanlış okuyup düzeltiyor; sınırın altında kalan
        her hata affedilmeli, yoksa kod pratikte tek denemelik olurdu.
        """
        ticket = devices.Ticket()
        wrong = pairing.secret_from_code("000000" if ticket.code != "000000"
                                         else "111111")
        for _ in range(devices.Ticket.MAX_CODE_ATTEMPTS - 1):
            gate = devices.AccessGate(ticket, self.store)
            gate.opening_frame()
            gate.accept(pairing.T_AUTH, auth_body(wrong, gate.challenge))

        gate = devices.AccessGate(ticket, self.store)
        gate.opening_frame()
        out = gate.accept(pairing.T_AUTH, auth_body(
            pairing.secret_from_code(ticket.code), gate.challenge))
        self.assertTrue(out.ok, "sınırın altındaki doğru kod reddedildi")


    def test_locking_the_code_leaves_the_qr_alive(self):
        """Kilidin sınırı. **Regresyon testi.**

        İlk tasarım kilitlenince bileti yakıyordu ve bu, LAN'daki birine
        beş yanlış denemeyle meşru telefonu dışarıda bırakma imkânı
        veriyordu. `test_e2e_host.test_failed_pairing_does_not_consume_a_slot`
        aynı özelliği uçtan uca koruyor; burada birim düzeyinde.
        """
        ticket = devices.Ticket()
        wrong = pairing.secret_from_code("000000" if ticket.code != "000000"
                                         else "111111")
        for _ in range(devices.Ticket.MAX_CODE_ATTEMPTS + 3):
            gate = devices.AccessGate(ticket, self.store)
            gate.opening_frame()
            gate.accept(pairing.T_AUTH, auth_body(wrong, gate.challenge))
        self.assertTrue(ticket.code_locked)

        gate = devices.AccessGate(ticket, self.store)
        gate.opening_frame()
        out = gate.accept(pairing.T_AUTH,
                          auth_body(ticket.token, gate.challenge))
        self.assertTrue(out.ok, "kod kilitlenince QR da öldü")
        self.assertIsNotNone(out.credential)


if __name__ == "__main__":
    unittest.main(verbosity=2)
