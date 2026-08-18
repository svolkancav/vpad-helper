#!/usr/bin/env python3
"""
QR üreticisi testleri — **taranabilirlik**, veri doğruluğu değil.

Koşturma:
    python test_qr_render.py

Payload'ın doğru olması yetmez: kullanıcının telefonu ekrandaki
KARAKTERLERE bakıyor. Varsayılan kipte bir karakter hücresi iki dikey modül
taşıyor (`▀` — ön plan üst modül, arka plan alt modül); oradaki bir kaydırma
ya da ters bir polarite, veri tarafı kusursuzken QR'ı okunamaz yapar. Ve bu
kusur sessizdir: host mutlu mutlu bir kare basar, telefon hiçbir şey görmez.

Bu yüzden testler ekrandaki METİNDEN başlıyor. ANSI çıktısı modül matrisine
geri çözülüyor; sonra iki şey doğrulanıyor:

  1. matris `qrcode`'un ürettiğiyle birebir mi (render kaydırmıyor mu),
  2. o matristen kurulan görüntüyü GERÇEK bir çözücü okuyor mu.

(2) `opencv-python` istiyor. Yoksa test atlanıyor — QR bir kolaylık ve
opsiyonel bir bağımlılık yüzünden bütün paketin testi düşmemeli. (1) her
zaman koşuyor ve pratikteki render hatalarının çoğunu zaten yakalar.
"""
from __future__ import annotations

import re
import unittest

import vpad_pairing as pairing

HALF = re.compile(r"\x1b\[(\d+);(\d+)m▀")
BIG = re.compile(r"\x1b\[(\d+)m {2}")

PAYLOAD = pairing.build_payload("192.168.1.64", 51590, bytes(range(16)))

try:
    import qrcode
    HAVE_QRCODE = True
except ImportError:                                     # pragma: no cover
    HAVE_QRCODE = False

try:
    import cv2
    import numpy as np
    HAVE_CV2 = True
except ImportError:                                     # pragma: no cover
    HAVE_CV2 = False


def parse_half(text: str) -> list[list[bool]]:
    """`▀` satırlarını modül matrisine çözer — karakter başına iki modül."""
    rows: list[list[bool]] = []
    for line in text.rstrip("\n").split("\n"):
        cells = HALF.findall(line)
        if not cells:
            continue
        rows.append([fg == "30" for fg, _ in cells])    # 30 = siyah ön plan
        rows.append([bg == "40" for _, bg in cells])    # 40 = siyah arka plan
    return rows


def parse_big(text: str) -> list[list[bool]]:
    return [
        [c == "40" for c in cells]
        for line in text.rstrip("\n").split("\n")
        if (cells := BIG.findall(line))
    ]


def source_matrix() -> list[list[bool]]:
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                         border=4)
    code.add_data(PAYLOAD)
    code.make(fit=True)
    return code.get_matrix()


def decode(matrix: list[list[bool]], scale: int = 8) -> str:
    """Matristen görüntü kurup gerçek çözücüye verir."""
    arr = np.array(matrix, dtype=np.uint8)
    img = np.where(arr == 1, 0, 255).astype(np.uint8)
    big = cv2.resize(img, None, fx=scale, fy=scale,
                     interpolation=cv2.INTER_NEAREST)
    text, points, _ = cv2.QRCodeDetector().detectAndDecode(big)
    return text if points is not None else ""


class TestQrRender(unittest.TestCase):

    def test_yarim_blok_matrisi_kaynakla_birebir(self):
        if not HAVE_QRCODE:
            self.skipTest("qrcode kurulu değil")
        rendered = parse_half(pairing.render_qr_terminal(PAYLOAD, big=False))
        source = source_matrix()
        # Tek yükseklikli matris çift satıra yuvarlanır; fazlalık satır
        # sessiz bölgedir ve beyaz olmalıdır.
        self.assertEqual(source, rendered[:len(source)],
                         "render modülleri kaydırıyor")
        for extra in rendered[len(source):]:
            self.assertFalse(any(extra), "dolgu satırı beyaz değil")

    def test_buyuk_kip_matrisi_kaynakla_birebir(self):
        if not HAVE_QRCODE:
            self.skipTest("qrcode kurulu değil")
        self.assertEqual(
            source_matrix(),
            parse_big(pairing.render_qr_terminal(PAYLOAD, big=True)),
        )

    def test_sessiz_bolge_dort_modul(self):
        # Spec 4 modül ister; daralırsa çoğu kamera QR'ı hiç bulamaz.
        if not HAVE_QRCODE:
            self.skipTest("qrcode kurulu değil")
        source = source_matrix()
        top = next(i for i, row in enumerate(source) if any(row))
        left = next(i for i, col in enumerate(zip(*source)) if any(col))
        self.assertGreaterEqual(top, 4)
        self.assertGreaterEqual(left, 4)

    def test_gercek_cozucu_ekrandakini_okuyor(self):
        if not HAVE_CV2:
            self.skipTest("opencv-python kurulu değil")
        for big in (False, True):
            with self.subTest(big=big):
                text = pairing.render_qr_terminal(PAYLOAD, big=big)
                matrix = parse_big(text) if big else parse_half(text)
                self.assertEqual(PAYLOAD, decode(matrix),
                                 "ekrana basılan QR çözülemiyor")

    def test_polarite_dogru_yonde(self):
        # Tarayıcılar AÇIK zemin üzerine KOYU modül bekler. Ters basılmış bir
        # QR bu testte "her iki yönde de okunuyor" diye görünmez: ters
        # çevrilmiş hâli okunmamalı.
        if not HAVE_CV2:
            self.skipTest("opencv-python kurulu değil")
        matrix = parse_half(pairing.render_qr_terminal(PAYLOAD))
        flipped = [[not cell for cell in row] for row in matrix]
        self.assertNotEqual(PAYLOAD, decode(flipped))

    def test_qrcode_yoksa_elle_giris_adresi_basiliyor(self):
        # QR opsiyonel; paket yoksa daemon açılmalı ve kullanıcı adresi elle
        # girebilmeli. Bu yolun sessizce boş dönmesi en kötüsü olurdu.
        import builtins
        real_import = builtins.__import__

        def no_qrcode(name, *a, **kw):
            if name == "qrcode":
                raise ImportError("test")
            return real_import(name, *a, **kw)

        builtins.__import__ = no_qrcode
        try:
            out = pairing.render_qr_terminal(PAYLOAD)
        finally:
            builtins.__import__ = real_import
        self.assertIn(PAYLOAD, out)
        self.assertIn("qrcode", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
