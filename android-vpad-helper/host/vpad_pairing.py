#!/usr/bin/env python3
"""
V-Pad eşleşme katmanı — QR ile kamera doğrulamalı WiFi eşleşmesi.

Bu modül `vpad_daemon.py`'nin YANINA gelir; onu içe aktarmaz ve onun
bağımlılıklarına (zeroconf, vgamepad) ihtiyaç duymaz. Sebep: eşleşme mantığı
protokolün en güvenlik-kritik parçası ve her yerde — CI dahil, hiçbir şey
kurulmadan — test edilebilmeli.

Tasarımın tamamı ve gerekçeleri: ../DESIGN.md

Üçüncü parti bağımlılık: YOK. Tek istisna `render_qr_terminal`, `qrcode`
paketini TEMBEL içe aktarır ve yoksa metin geri düşüşü yapar — yani bu modülün
hiçbir testi opsiyonel bir pakete bağlı değil.
"""
from __future__ import annotations

import hmac
import ipaddress
import secrets
import struct
from dataclasses import dataclass
from hashlib import sha256

# ── Protokol sabitleri ───────────────────────────────────────────────
#
# Çerçeve biçimi vpad_daemon.py ile aynı: [u16 LE uzunluk][u8 tip][gövde].
#
# Tip baytlarının kayıt defteri `docs/companion-daemon.md` §4'tür —
# `vpad_daemon.py` DEĞİL. Fark kritik: daemon yalnızca UYGULADIĞI tipleri
# tanımlar, spec ise ileride kullanılacakları da AYIRIR. Bu dosyanın ilk
# sürümü yalnızca daemon'a bakıp CHALLENGE'ı 0x20'ye koymuştu; oysa spec
# 0x20'yi RUMBLE'a ayırmış (S→C, 2 bayt, v3'e ertelenmiş) ve sahadaki iOS
# istemcisi de öyle biliyor (`ios/Runner/FrameCodec.swift`: "0x20 RUMBLE is
# v3, intentionally not implemented in Phase 1"). Aynı bayt iki anlama
# gelemez.
#
# Yeni kodlar spec'in kendi yön bloklarındaki ilk boş yerlere oturuyor:
#   S→C: 0x10 HELLO_ACK · 0x11 REJECT · 0x12 PONG   → sıradaki boş 0x13
#   C→S: 0x01 HELLO · 0x02 REPORT · 0x03 PING
#        0x04 BYE · 0x05 MOUSE                      → sıradaki boş 0x06

T_CHALLENGE = 0x13  # host → istemci: 16 bayt challenge
T_AUTH = 0x06       # istemci → host: 16 bayt nonce + 32 bayt mac

# Çoklu oyuncu: host → istemci, 1 bayt oyuncu indeksi (0..3).
# HELLO_ACK'ten hemen sonra gönderilir; tek oyunculu host hiç göndermez.
# HELLO_ACK'in gövdesi GENİŞLETİLEMEZDİ — spec'te 2 bayt sabit ve iOS
# istemcisi uzunluğu doğruluyor. Yeni tip eklemek ise §9'a göre kırıcı değil.
T_SLOT = 0x14

# Oturum sürekliliği (2026-08-14). QR artık bir erişim anahtarı değil,
# **tek kullanımlık kayıt bileti**; kaydolan cihaz kendi kimliğini alır ve
# sonraki açılışlarda QR'sız girer. Ayrıntı ve gerekçe: vpad_devices.py.
T_RESUME = 0x07      # istemci → host: kayıtlı cihaz kimliğiyle giriş
T_CREDENTIAL = 0x15  # host → istemci: kayıt sonrası verilen cihaz kimliği

# Sarmalanmış kimlik (2026-08-19). 0x15 anahtarı DÜZ METİN taşıyordu ve
# taşıma TLS'siz: WPA2-PSK bir ev ağında Wi-Fi parolasını bilen biri
# 4-way handshake'i yakalayıp trafiği çözebiliyor (CWE-319). Bu tip aynı
# gövdeyi, QR bileti token'ından türetilen anahtarla şifreli taşır.
#
# 0x15 KALDIRILMADI: yayındaki istemciler (Play'deki 2.0.2+49 ve App
# Store'daki iOS uygulaması) onu bekliyor. Hangisinin gönderileceğini
# biletin QR sürümü belirliyor — bkz. PAYLOAD_VERSION.
T_CREDENTIAL_ENC = 0x16

# vpad_daemon.py'deki karşılıkları — bu modül daemon'ı içe aktarmadığı için
# burada da tanımlı. Değerler DEĞİŞTİRİLEMEZ, iki dosya aynı teli konuşuyor.
T_HELLO = 0x01
T_HELLO_ACK = 0x10
T_REJECT = 0x11

R_AUTH_REQUIRED = 0x04  # REJECT sebebi: eşleşme açık, AUTH gelmedi
R_AUTH_FAILED = 0x05    # REJECT sebebi: MAC uyuşmadı
# REJECT sebebi: cihaz kimliği tanınmıyor (hiç kaydolmamış, süresi dolmuş
# ya da iptal edilmiş). İstemci bunu görünce **QR ekranına düşmeli** —
# R_AUTH_FAILED'dan ayrı olması tam bu yüzden: biri "yanlış anahtar",
# diğeri "yeniden kaydol".
R_DEVICE_UNKNOWN = 0x06

MAX_FRAME = 4096  # vpad_daemon ile aynı

SCHEME = "vpad://"

# QR yükü sürümü — **yetenek bildirimi olarak da kullanılıyor.**
#
# Host, kimliği sarmalanmış (T_CREDENTIAL_ENC) göndermeden ÖNCE telefonun
# bunu çözebildiğini bilmek zorunda. Ama spec §4.8 gereği CREDENTIAL,
# AUTH'un hemen ardından ve HELLO'dan ÖNCE gidiyor — yani telefonun tek
# söz hakkı olan HELLO çok geç geliyor, AUTH'un gövdesi de sabit uzunlukta
# (uzatmak yayındaki `verify_auth`'u kırar).
#
# Kaldıraç bu yüzden QR'ın kendisi: **v2 bir QR'ı yalnızca v2 anlayan bir
# telefon ayrıştırabilir.** Bileti v2 QR'dan gelen bir istemci AUTH'u
# geçtiyse, sarmalamayı çözebildiğini KANITLAMIŞ olur.
#
# Bedeli açık: yayındaki eski uygulama (2.0.2+49) v2 QR'ı okuyunca
# "desteklenmeyen payload sürümü" ile düşer — sessiz değil, açık bir hata.
# Zaten kayıtlı cihazlar etkilenmez (RESUME yolu QR'a hiç uğramıyor).
PAYLOAD_VERSION = 2
PAYLOAD_VERSION_MIN = 1  # ayrıştırırken hâlâ kabul edilen en eski sürüm

TOKEN_LEN = 16      # ham bayt (QR'da 32 hex karakter)
CHALLENGE_LEN = 16
NONCE_LEN = 16
MAC_LEN = 32        # HMAC-SHA256 tam çıktısı
AUTH_LEN = NONCE_LEN + MAC_LEN

# Alan ayracı: aynı token ileride başka bir amaçla kullanılırsa MAC'ler
# birbirine karışmasın. Sürüm de burada — şema değişirse eski MAC yeni
# şemada geçerli olmaz.
AUTH_LABEL = b"vpad-auth-v1"

# Telefonun ve host'un kabul ettiği adres aralıkları. İkisi de AYNI listeyi
# kullanır; Kotlin tarafındaki karşılığı PairingPayload.LAN_RANGES.
#
# Python'un `IPv4Address.is_private`'ı BİLEREK kullanılmadı: o özellik
# 240/4, 198.18/15, 203.0.113/24 gibi "özel amaçlı ama LAN değil"
# aralıkları da kapsıyor. Burada ne istediğimiz açıkça yazılı olsun.
LAN_RANGES = tuple(
    ipaddress.ip_network(c)
    for c in (
        "127.0.0.0/8",     # loopback
        "10.0.0.0/8",      # RFC1918
        "172.16.0.0/12",   # RFC1918
        "192.168.0.0/16",  # RFC1918
        "169.254.0.0/16",  # link-local (yönlendiricisiz ağ)
        "100.64.0.0/10",   # operatör NAT (CGNAT)
    )
)


class PairingError(ValueError):
    """Geçersiz veya düşmanca eşleşme verisi."""


def _is_ascii_digits(text: str) -> bool:
    """Yalnızca ASCII 0-9 mu?

    `str.isdigit()` KULLANILAMAZ: Unicode rakamlarını da kabul eder —
    `'٨٠'.isdigit()` True döner ve `int('٨٠')` 80 verir. Kotlin tarafındaki
    ayrıştırıcı ise ASCII dışını reddediyor, yani `isdigit()` iki uygulamayı
    ayrıştırırdı: `vpad://192.168.1.1:٨٠?...` Python'da geçer, telefonda
    geçmezdi. Aynı payload'ın iki tarafta farklı sonuç vermesi, protokolde
    kabul edilebilir bir şey değil.
    """
    return text != "" and all("0" <= ch <= "9" for ch in text)


# ── Token ────────────────────────────────────────────────────────────

def generate_token() -> bytes:
    """Kriptografik olarak güvenli 16 baytlık eşleşme token'ı.

    `secrets` kullanılır, `random` DEĞİL: `random` Mersenne Twister'dır ve
    birkaç çıktıdan durumu geri çıkarılabilir.
    """
    return secrets.token_bytes(TOKEN_LEN)


def token_to_hex(token: bytes) -> str:
    if len(token) != TOKEN_LEN:
        raise PairingError(f"token {TOKEN_LEN} bayt olmalı, {len(token)} geldi")
    return token.hex()


def token_from_hex(text: str) -> bytes:
    text = text.strip()
    if len(text) != TOKEN_LEN * 2:
        raise PairingError(f"token {TOKEN_LEN * 2} hex karakter olmalı")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise PairingError(f"token hex değil: {exc}") from exc


# ── Adres kuralı ─────────────────────────────────────────────────────

def is_lan_ipv4(text: str) -> bool:
    """Adres LAN aralıklarından birinde mi?

    Yalnızca IPv4 literal kabul edilir. Alan adı verilirse False döner —
    çözümleme YAPILMAZ, çünkü DNS'e sormak tam olarak kapatmak istediğimiz
    kapıdır (kötü niyetli QR + DNS rebinding).

    **Asgari Python 3.9.5 (veya 3.8.12).** Baştaki sıfırı olan sekizliler
    (`192.168.010.1`) ancak o sürümlerden itibaren reddediliyor; öncesinde
    `ipaddress` sıfırları kırpıp adresi kabul ediyordu (CVE-2021-29921).
    Kotlin tarafındaki `PairingPayload.parseIpv4` her zaman reddettiği için,
    daha eski bir Python'da iki uygulama aynı payload'a farklı cevap verir.
    """
    try:
        addr = ipaddress.IPv4Address(text)
    except (ipaddress.AddressValueError, ValueError):
        return False
    return any(addr in net for net in LAN_RANGES)


# ── QR payload ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class PairingInfo:
    """Ayrıştırılmış ve DOĞRULANMIŞ eşleşme bilgisi."""
    host: str
    port: int
    token: bytes
    # Biletin geldiği QR sürümü. `version >= 2` ⇒ istemci sarmalanmış
    # kimliği çözebilir (bkz. PAYLOAD_VERSION). Eski çağıranlar için
    # varsayılan 1 — yani "sarmalama yok", güvenli taraf.
    version: int = PAYLOAD_VERSION_MIN

    @property
    def token_hex(self) -> str:
        return self.token.hex()


def build_payload(host: str, port: int, token: bytes) -> str:
    """QR'a gömülecek metni kurar."""
    if not is_lan_ipv4(host):
        # Host kendi adresini yayınlıyor; LAN dışı bir adres yayınlamak
        # telefona reddedeceği bir QR göstermek olurdu.
        raise PairingError(f"LAN dışı adres yayınlanamaz: {host}")
    if not 1 <= port <= 65535:
        raise PairingError(f"geçersiz port: {port}")
    return f"{SCHEME}{host}:{port}?t={token_to_hex(token)}&v={PAYLOAD_VERSION}"


def parse_payload(text: str) -> PairingInfo:
    """QR'dan okunan metni ayrıştırır ve doğrular.

    Bu fonksiyon DÜŞMANCA girdi alır: kullanıcı herhangi bir QR kodunu
    tarayabilir. Her adım açıkça reddeder; hiçbir yerde "makul varsayım"
    yapılmaz.

    Kotlin tarafındaki `PairingPayload.parse` bunun birebir karşılığıdır;
    ikisi aynı vakalarla test edilir.
    """
    if not isinstance(text, str):
        raise PairingError("payload metin değil")
    text = text.strip()

    if not text.startswith(SCHEME):
        raise PairingError("şema vpad:// değil")
    body = text[len(SCHEME):]

    if "?" not in body:
        raise PairingError("sorgu bölümü yok")
    authority, query = body.split("?", 1)

    # --- adres:port ---
    if ":" not in authority:
        raise PairingError("port yok")
    host, _, port_text = authority.rpartition(":")
    if not host:
        raise PairingError("adres boş")
    if not _is_ascii_digits(port_text):
        # "+80", " 80", "0x50" ve Unicode rakamları burada elenir.
        raise PairingError(f"port sayı değil: {port_text!r}")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise PairingError(f"port aralık dışı: {port}")

    if not is_lan_ipv4(host):
        raise PairingError(
            f"adres LAN değil veya IPv4 literal değil: {host!r} — "
            "kötü niyetli QR olabilir"
        )

    # --- sorgu parametreleri ---
    params: dict[str, str] = {}
    for part in query.split("&"):
        if not part:
            continue
        key, sep, value = part.partition("=")
        if not sep:
            raise PairingError(f"biçimsiz sorgu parçası: {part!r}")
        if key in params:
            # Yinelenen anahtar: hangi değerin geçerli olduğu belirsiz.
            # Belirsizliği kabul etmek yerine reddet.
            raise PairingError(f"yinelenen sorgu anahtarı: {key!r}")
        params[key] = value

    version_text = params.get("v")
    if version_text is None:
        raise PairingError("sürüm (v) yok")
    if not _is_ascii_digits(version_text):
        raise PairingError(
            f"desteklenmeyen payload sürümü: {version_text!r} "
            f"(bu sürüm {PAYLOAD_VERSION})"
        )
    version = int(version_text)
    # ARALIK kabul ediliyor, eşitlik değil: yeni telefon eski host'un
    # bastığı v1 QR'ı da okuyabilmeli. Ters yön (eski telefon + v2 QR)
    # bilinçli olarak kapalı — sarmalamayı çözemeyecek bir istemcinin
    # kaydolmasını istemiyoruz, bkz. PAYLOAD_VERSION.
    if not PAYLOAD_VERSION_MIN <= version <= PAYLOAD_VERSION:
        raise PairingError(
            f"desteklenmeyen payload sürümü: {version_text!r} "
            f"(bu sürüm {PAYLOAD_VERSION})"
        )

    token_text = params.get("t")
    if token_text is None:
        raise PairingError("token (t) yok")
    token = token_from_hex(token_text)

    return PairingInfo(host=host, port=port, token=token, version=version)


# ── Challenge-response ───────────────────────────────────────────────

CODE_LEN = 6
CODE_LABEL = b"vpad-code-v1"


def generate_code() -> str:
    """Elle girilecek 6 haneli doğrulama kodu.

    QR'ın alternatifi, yedeği değil: kamerası olmayan (Play services'siz
    Android, kırık kamera) cihazın host'a girmesinin TEK yolu. Aynı bilete
    bağlı, aynı anda yaşar ve aynı anda ölür.

    Neden yalnız rakam: kullanıcı bunu ekrandan okuyup telefona yazacak.
    Harf karıştırma (O/0, l/1) ve klavye düzeni sorunu olmasın diye alfabe
    0-9. Baştaki sıfır korunuyor — kod bir sayı değil, altı karakterlik bir
    dize.
    """
    return "".join(str(secrets.randbelow(10)) for _ in range(CODE_LEN))


def secret_from_code(code: str) -> bytes:
    """6 haneli kodu AUTH'un beklediği 16 baytlık anahtara çevirir.

    Alan ayrımı `AUTH_LABEL`/`RESUME_LABEL` ile aynı gerekçeyle: aynı
    baytların iki ayrı amaçta anlam kazanması engelleniyor. Böylece tel
    protokolü hiç değişmiyor — istemci kodu bu türetmeden geçirip normal
    AUTH gövdesini kuruyor, host da öyle doğruluyor.

    **Kaba kuvvet koruması burada DEĞİL.** 10^6 uzay küçük; onu kapatan
    şey `devices.Ticket`'ın deneme sayacı (birkaç yanlıştan sonra bilet
    yanıyor ve kod değişiyor). Uzun bir türetme bunu çözmezdi: saldırgan
    zaten host'a soru sormak zorunda, offline deneme yapamıyor.
    """
    if len(code) != CODE_LEN or not _is_ascii_digits(code):
        raise PairingError(f"kod {CODE_LEN} haneli rakam olmalı")
    return sha256(CODE_LABEL + code.encode("ascii")).digest()[:TOKEN_LEN]


def make_challenge() -> bytes:
    """Her TCP bağlantısı için YENİ challenge. Tek kullanımlık."""
    return secrets.token_bytes(CHALLENGE_LEN)


def compute_auth(token: bytes, challenge: bytes, nonce: bytes) -> bytes:
    """HMAC-SHA256(token, LABEL ‖ challenge ‖ nonce) → 32 bayt.

    Kotlin karşılığı: PairingCrypto.computeAuth
    """
    if len(token) != TOKEN_LEN:
        raise PairingError(f"token {TOKEN_LEN} bayt olmalı")
    if len(challenge) != CHALLENGE_LEN:
        raise PairingError(f"challenge {CHALLENGE_LEN} bayt olmalı")
    if len(nonce) != NONCE_LEN:
        raise PairingError(f"nonce {NONCE_LEN} bayt olmalı")
    return hmac.new(token, AUTH_LABEL + challenge + nonce, sha256).digest()


def build_auth_body(token: bytes, challenge: bytes,
                    nonce: bytes | None = None) -> bytes:
    """İstemcinin göndereceği AUTH gövdesi: nonce(16) ‖ mac(32)."""
    if nonce is None:
        nonce = secrets.token_bytes(NONCE_LEN)
    return nonce + compute_auth(token, challenge, nonce)


def verify_auth(token: bytes, challenge: bytes, body: bytes) -> bool:
    """AUTH gövdesini doğrular. Sabit zamanlı; hiçbir durumda exception atmaz.

    Uzunluk yanlışsa `compare_digest`'e hiç gitmeden False döner — bu bir
    zamanlama sızıntısı değil, çünkü uzunluk zaten tel üzerinde açıkça
    görünüyor.
    """
    if len(body) != AUTH_LEN:
        return False
    nonce, mac = body[:NONCE_LEN], body[NONCE_LEN:]
    try:
        expected = compute_auth(token, challenge, nonce)
    except PairingError:
        return False
    return hmac.compare_digest(mac, expected)


# ── Çerçeve yardımcıları ─────────────────────────────────────────────
#
# vpad_daemon.encode_frame ile aynı biçim. Burada tekrarlanıyor ki bu modül
# ve testleri daemon'ı (ve onun zeroconf bağımlılığını) içe aktarmak zorunda
# kalmasın.

# ── Kimlik sarmalama (2026-08-19) ────────────────────────────────────
#
# NEDEN: `CREDENTIAL` (0x15) 32 baytlık kalıcı cihaz anahtarını DÜZ METİN
# taşıyordu ve taşıma TLS'siz. WPA2-PSK bir ev ağında Wi-Fi parolasını
# bilen biri (aile, misafir, eski kiracı) 4-way handshake'i yakalayıp —
# ya da deauth ile yeniden tetikleyip — o paketi okuyabiliyor. CWE-319.
# WPA3-SAE bu saldırıyı kapatıyor ama WPA2 hâlâ yaygın.
#
# NEDEN AES-GCM DEĞİL: Python standart kütüphanesinde simetrik şifre YOK.
# `cryptography` eklemek PyInstaller paketine native wheel sokardı;
# yayındaki exe zaten bir kez Defender'a `Wacatac.C!ml` diye takılmıştı.
# Bu yüzden yapı tamamen HMAC-SHA256 üzerine kuruldu: RFC 5869 HKDF ile
# anahtar akışı + ayrı anahtarla encrypt-then-MAC. İlkel yazmıyoruz,
# yalnız standart bir kompozisyon kuruyoruz; iki dil arasındaki uyum
# altın vektörle pinleniyor.
#
# NE KORUMUYOR: 6 haneli elle giriş kodu yolunu. O sır ~20 bit; sarmalama
# yapılsa da dinleyen 10^6 adayı ÇEVRİMDIŞI dener. Onu kapatmak PAKE
# ister (Android'in kendi ADB eşleşmesi SPAKE2 kullanıyor) ve bilinçli
# olarak bu turun dışında bırakıldı.
CRED_ENC_LABEL = b"vpad-cred-enc-v1"
CRED_MAC_LABEL = b"vpad-cred-mac-v1"
CRED_NONCE_LEN = 16
CRED_TAG_LEN = 16


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF (SHA-256). Kotlin karşılığı: PairingCrypto.hkdf.

    Extract-then-Expand; `salt` HMAC anahtarı, `ikm` mesajdır (RFC 5869
    §2.2 — sırası ters yazmak sessizce farklı ama "çalışır görünen" bir
    çıktı üretir, bu yüzden altın vektörle pinlendi).
    """
    if length <= 0 or length > 255 * 32:
        raise PairingError(f"hkdf uzunluğu aralık dışı: {length}")
    prk = hmac.new(salt, ikm, sha256).digest()
    out = bytearray()
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), sha256).digest()
        out += block
        counter += 1
    return bytes(out[:length])


def wrap_credential(secret: bytes, challenge: bytes, plaintext: bytes,
                    nonce: bytes | None = None) -> bytes:
    """CREDENTIAL gövdesini şifreler → `nonce(16) ‖ ct ‖ tag(16)`.

    `plaintext` bugünkü 0x15 gövdesinin AYNISI (`id_len ‖ id ‖ key`), yani
    çözen taraf mevcut ayrıştırıcıyı olduğu gibi kullanabiliyor.

    Anahtar akışı `challenge ‖ nonce` tuzuyla türetiliyor: challenge zaten
    bağlantı başına yeni, nonce da mesaj başına — ikisi birlikte akışın
    tekrar kullanılmasını imkânsız kılıyor (XOR akış şifresinde tekrar
    kullanım felakettir).
    """
    if len(secret) != TOKEN_LEN:
        raise PairingError(f"sır {TOKEN_LEN} bayt olmalı")
    if len(challenge) != CHALLENGE_LEN:
        raise PairingError(f"challenge {CHALLENGE_LEN} bayt olmalı")
    if not plaintext:
        raise PairingError("boş gövde sarmalanamaz")
    if nonce is None:
        nonce = secrets.token_bytes(CRED_NONCE_LEN)
    if len(nonce) != CRED_NONCE_LEN:
        raise PairingError(f"nonce {CRED_NONCE_LEN} bayt olmalı")

    salt = challenge + nonce
    keystream = hkdf_sha256(secret, salt, CRED_ENC_LABEL, len(plaintext))
    mac_key = hkdf_sha256(secret, salt, CRED_MAC_LABEL, 32)
    ciphertext = bytes(p ^ k for p, k in zip(plaintext, keystream))
    # Encrypt-then-MAC: etiket ŞİFRELİ metnin üstünde. Tersi (MAC-then-
    # encrypt) çözmeden önce doğrulama yapamamak demekti.
    tag = hmac.new(mac_key, nonce + ciphertext, sha256).digest()[:CRED_TAG_LEN]
    return nonce + ciphertext + tag


def unwrap_credential(secret: bytes, challenge: bytes,
                      body: bytes) -> bytes | None:
    """Sarmalanmış gövdeyi çözer; etiket tutmazsa `None` — istisna DEĞİL.

    Bozuk/sahte çerçeve oturumu düşürmemeli: bu tip el sıkışmanın yan
    kanalı, `parse_credential`'ın hoşgörüsüyle aynı gerekçe.
    """
    if len(secret) != TOKEN_LEN or len(challenge) != CHALLENGE_LEN:
        return None
    if len(body) <= CRED_NONCE_LEN + CRED_TAG_LEN:
        return None
    nonce = body[:CRED_NONCE_LEN]
    ciphertext = body[CRED_NONCE_LEN:-CRED_TAG_LEN]
    tag = body[-CRED_TAG_LEN:]

    salt = challenge + nonce
    mac_key = hkdf_sha256(secret, salt, CRED_MAC_LABEL, 32)
    expected = hmac.new(mac_key, nonce + ciphertext, sha256).digest()
    if not hmac.compare_digest(tag, expected[:CRED_TAG_LEN]):
        return None

    keystream = hkdf_sha256(secret, salt, CRED_ENC_LABEL, len(ciphertext))
    return bytes(c ^ k for c, k in zip(ciphertext, keystream))


def encode_frame(msg_type: int, payload: bytes = b"") -> bytes:
    total = 3 + len(payload)
    if total > MAX_FRAME:
        raise PairingError(f"çerçeve çok büyük: {total} > {MAX_FRAME}")
    return struct.pack("<HB", total, msg_type) + payload


def encode_challenge(challenge: bytes) -> bytes:
    if len(challenge) != CHALLENGE_LEN:
        raise PairingError(f"challenge {CHALLENGE_LEN} bayt olmalı")
    return encode_frame(T_CHALLENGE, challenge)


def encode_auth(token: bytes, challenge: bytes,
                nonce: bytes | None = None) -> bytes:
    return encode_frame(T_AUTH, build_auth_body(token, challenge, nonce))


def encode_slot(index: int) -> bytes:
    """Oyuncu indeksi çerçevesi. Kotlin karşılığı: `WifiFrameCodec.T_SLOT`."""
    if not 0 <= index <= 3:
        raise PairingError(f"slot 0..3 olmalı, {index} geldi")
    return encode_frame(T_SLOT, bytes([index]))


def encode_reject(reason: int, message: str = "") -> bytes:
    """vpad_daemon.encode_reject ile aynı biçim."""
    return encode_frame(T_REJECT, bytes([reason]) + message.encode("utf-8"))


# ── Sunucu tarafı eşleşme kapısı ─────────────────────────────────────

class PairingGate:
    """Sunucu tarafı el sıkışma durumu. **Hiç I/O yapmaz.**

    Çerçeve üretir ve gelen çerçeveyi doğrular; soketi çağıran yönetir. Bu
    ayrım bilinçli: kapı, gerçek bir soket olmadan test edilebiliyor ve
    daemon'a bağlanması birkaç satır kalıyor (bkz. DAEMON_PATCH.md).

    Kullanım:

        gate = PairingGate(token)              # token None → eşleşme kapalı
        opening = gate.opening_frame()
        if opening:
            sock.sendall(opening)              # CHALLENGE
            mtype, payload = next(reader)
            ok, reject = gate.accept(mtype, payload)
            if not ok:
                sock.sendall(reject)
                return
        # buradan sonrası mevcut akış: HELLO bekle
    """

    __slots__ = ("_token", "_challenge", "_settled")

    def __init__(self, token: bytes | None):
        if token is not None and len(token) != TOKEN_LEN:
            raise PairingError(f"token {TOKEN_LEN} bayt olmalı")
        self._token = token
        self._challenge: bytes | None = None
        self._settled = False

    @property
    def enabled(self) -> bool:
        return self._token is not None

    @property
    def challenge(self) -> bytes | None:
        """Bu bağlantı için üretilmiş challenge (tanı/test amaçlı)."""
        return self._challenge

    def opening_frame(self) -> bytes | None:
        """Bağlantı kabul edilir edilmez gönderilecek CHALLENGE çerçevesi.

        Eşleşme kapalıysa None döner ve akış bugünkünün aynısı kalır —
        mevcut iPhone istemcisi hiçbir şey fark etmez.
        """
        if not self.enabled:
            return None
        # Her bağlantıda YENİ challenge. Aynı kapı yeniden kullanılırsa
        # (olmamalı) yine tazelenir.
        self._challenge = make_challenge()
        self._settled = False
        return encode_challenge(self._challenge)

    def accept(self, msg_type: int, payload: bytes) -> tuple[bool, bytes | None]:
        """İstemcinin ilk çerçevesini değerlendirir.

        Dönüş: `(kabul, reddedilirse gönderilecek REJECT çerçevesi)`.
        """
        if not self.enabled:
            return True, None
        if self._settled:
            raise PairingError("kapı zaten karara bağlandı")
        if self._challenge is None:
            raise PairingError("opening_frame() çağrılmadan accept() çağrıldı")

        self._settled = True

        if msg_type != T_AUTH:
            # Eşleşme açıkken doğrudan HELLO göndermek = token'sız istemci
            # (eski sürüm uygulama veya elle bağlanmaya çalışan biri).
            return False, encode_reject(
                R_AUTH_REQUIRED,
                "bu host QR ile eşleşme istiyor; uygulamada QR'ı tarayın",
            )

        if not verify_auth(self._token, self._challenge, payload):
            return False, encode_reject(R_AUTH_FAILED, "eşleşme doğrulaması başarısız")

        return True, None


# ── QR gösterimi ─────────────────────────────────────────────────────

def render_qr_terminal(payload: str, big: bool = False) -> str:
    """QR'ı terminale basılabilir metne çevirir.

    `qrcode` paketi yoksa ImportError YAYMAZ — bunun yerine adresi metin
    olarak veren bir geri düşüş döner. Sebep: QR bir kolaylık; onun eksikliği
    daemon'ın açılmasını engellememeli. Kullanıcı hâlâ elle girebilir.
    """
    try:
        import qrcode  # noqa: PLC0415 — opsiyonel, tembel içe aktarma
    except ImportError:
        return (
            "  [!] `qrcode` paketi kurulu değil, QR basılamıyor.\n"
            "      Kurmak için: pip install qrcode\n"
            f"      Elle giriş  : {payload}\n"
        )

    code = qrcode.QRCode(
        # ERROR_CORRECT_M: %15 hata toleransı. Ekran QR'ı için L yeterli
        # olurdu, ama M kamera açısı/parlama toleransını gözle görülür
        # artırıyor ve payload kısa olduğu için matris büyümüyor.
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        border=4,  # spec 4 modül sessiz bölge ister; taranabilirlik buna bağlı
    )
    code.add_data(payload)
    code.make(fit=True)

    matrix = code.get_matrix()
    return _matrix_to_text(matrix, big)


def _matrix_to_text(matrix: list[list[bool]], big: bool) -> str:
    """Modül matrisini ANSI renkli metne çevirir.

    Renkler AÇIKÇA veriliyor: tarayıcılar AÇIK zemin üzerine KOYU modül
    bekler. Terminal teması koyuysa varsayılan renklerle QR ters çıkar ve
    çoğu telefon kamerası onu okumaz.
    """
    reset = "\x1b[0m"
    out: list[str] = []

    if big:
        # Modül başına iki boşluk — uzaktan/düşük ışıkta taramak için.
        for row in matrix:
            line = "".join("\x1b[40m  " if cell else "\x1b[107m  " for cell in row)
            out.append(line + reset)
        return "\n".join(out) + "\n"

    # Yarım blok '▀': bir karakter hücresi iki dikey modül taşır, en-boy
    # oranı doğru çıkar ve QR yarı yüksekliğe sığar.
    height = len(matrix)
    for y in range(0, height, 2):
        upper_row = matrix[y]
        lower_row = matrix[y + 1] if y + 1 < height else [False] * len(upper_row)
        line = []
        for x in range(len(upper_row)):
            fg = "30" if upper_row[x] else "97"
            bg = "40" if lower_row[x] else "107"
            line.append(f"\x1b[{fg};{bg}m▀")
        out.append("".join(line) + reset)
    return "\n".join(out) + "\n"
