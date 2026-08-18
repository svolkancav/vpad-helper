#!/usr/bin/env python3
"""
Cihaz kayıt defteri — QR'ı erişim anahtarı olmaktan çıkarıp kayıt bileti yapar.

Problem
───────
İki uç da yanlıştı:

* **Her açılışta yeni QR, başka hiçbir şey yok.** Telefon her daemon
  yeniden başlatmasında QR taramak zorunda. PC'yi kapat-aç = yeniden tara.
* **Tek kalıcı token.** QR'ı bir kez gören —ya da fotoğrafını çeken—
  sonsuza kadar bağlanabilir. İptal yolu yok, kim bağlanabiliyor
  görünmüyor.

Çözüm: **kayıt bileti ≠ erişim anahtarı**
─────────────────────────────────────────
QR bir *bilet*. Telefon onunla bir kez doğrulanır, host ona **cihaza özel**
bir kimlik (`device_id` + 32 baytlık anahtar) verir ve kendi defterine
yazar. Sonraki bağlantılarda telefon o kimlikle girer, QR görmez.

    İlk kez   : CHALLENGE → AUTH(bilet)      → CREDENTIAL(id, key) → bağlı
    Sonrakiler: CHALLENGE → RESUME(id, mac)                        → bağlı

Bunun kazandırdıkları:

* Bilet **tek kullanımlık** (user kararı 2026-08-14): bir cihaz kaydolunca
  ölür. Ekranda unutulmuş ya da fotoğraflanmış bir QR ikinci bir cihaz
  kaydettiremez.
* Anahtar **cihaz başına**, yani tek tek iptal edilebilir. Bir telefonu
  kaybetmek diğerlerini yeniden eşleştirmeyi gerektirmiyor.
* Kayıt **kullanılmazsa söner** (user kararı): `TTL_DAYS` gün boyunca
  bağlanmayan cihaz defterden düşer, o telefon yeniden QR tarar.

Dürüst sınır — bilerek kabul edildi
───────────────────────────────────
Sönme sayacını **her başarılı bağlantı** tazeliyor. Yani anahtarı çalan ve
düzenli kullanan biri hiç sönmez; TTL unutulmuş cihazları temizler,
saldırganı değil. Gerçek iptal yolu `revoke()` — host defteri görebiliyor
(`devices()`), kim bağlanabiliyor listelenebiliyor. Bu, "tek kalıcı
token"a göre net kazanç ama sihir değil.

Bu modül **soket bilmez.** Dosya ve saat kullanır, ikisi de dışarıdan
verilebilir; böylece telefon olmadan test edilebiliyor —
`vpad_slots.PairingGate` ile aynı disiplin.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import struct
import sys
import time
from dataclasses import dataclass
from hashlib import sha256

import vpad_pairing as pairing

# Cihaz kimliği: 8 rastgele bayt, telde ve dosyada 16 onaltılık karakter.
# Sır DEĞİL — anahtarı taşıyan `key`. Kimlik yalnız defterde satır bulmaya
# yarıyor, o yüzden kısa olması sorun değil.
DEVICE_ID_LEN = 8
DEVICE_KEY_LEN = 32

# Alan ayrımı (domain separation). Kayıt biletinin MAC'i ile cihaz
# anahtarının MAC'i **farklı etiket** kullanıyor; aksi hâlde bir bağlamda
# yakalanan MAC diğerinde yeniden oynatılabilirdi.
RESUME_LABEL = b"vpad-resume-v1"

# Kullanılmayan kayıt kaç günde söner.
TTL_DAYS = 30
_SECONDS_PER_DAY = 86400

STORE_VERSION = 1


class DeviceError(ValueError):
    """Geçersiz cihaz işlemi — çağıran hatası."""


# ── Kripto ───────────────────────────────────────────────────────────

def generate_device_id() -> str:
    return secrets.token_bytes(DEVICE_ID_LEN).hex()


def generate_device_key() -> bytes:
    return secrets.token_bytes(DEVICE_KEY_LEN)


def compute_resume(key: bytes, challenge: bytes, nonce: bytes) -> bytes:
    """HMAC-SHA256(key, RESUME_LABEL ‖ challenge ‖ nonce) → 32 bayt.

    `pairing.compute_auth` ile aynı şekil, **farklı etiket ve farklı
    anahtar uzunluğu**. Kotlin karşılığı yazılırken ikisi karıştırılmamalı.
    """
    if len(key) != DEVICE_KEY_LEN:
        raise DeviceError(f"cihaz anahtarı {DEVICE_KEY_LEN} bayt olmalı")
    if len(challenge) != pairing.CHALLENGE_LEN:
        raise DeviceError(f"challenge {pairing.CHALLENGE_LEN} bayt olmalı")
    if len(nonce) != pairing.NONCE_LEN:
        raise DeviceError(f"nonce {pairing.NONCE_LEN} bayt olmalı")
    return hmac.new(key, RESUME_LABEL + challenge + nonce, sha256).digest()


# ── Çerçeveler ───────────────────────────────────────────────────────
#
#   RESUME     (0x07, C→S): u8 id_len ‖ id(ascii) ‖ nonce(16) ‖ mac(32)
#   CREDENTIAL (0x15, S→C): u8 id_len ‖ id(ascii) ‖ key(32)

def build_resume_body(device_id: str, key: bytes, challenge: bytes,
                      nonce: bytes | None = None) -> bytes:
    if nonce is None:
        nonce = secrets.token_bytes(pairing.NONCE_LEN)
    raw = device_id.encode("ascii")
    if not 1 <= len(raw) <= 255:
        raise DeviceError("cihaz kimliği 1..255 bayt olmalı")
    return (bytes([len(raw)]) + raw + nonce
            + compute_resume(key, challenge, nonce))


def parse_resume_body(body: bytes) -> tuple[str, bytes, bytes]:
    """RESUME gövdesini çözer. Bozuksa `DeviceError`."""
    if len(body) < 1:
        raise DeviceError("RESUME boş")
    id_len = body[0]
    expected = 1 + id_len + pairing.NONCE_LEN + 32
    if id_len == 0 or len(body) != expected:
        raise DeviceError(
            f"RESUME uzunluğu tutmuyor: {len(body)}, beklenen {expected}")
    device_id = body[1:1 + id_len].decode("ascii", errors="replace")
    rest = body[1 + id_len:]
    return device_id, rest[:pairing.NONCE_LEN], rest[pairing.NONCE_LEN:]


def encode_resume(device_id: str, key: bytes, challenge: bytes,
                  nonce: bytes | None = None) -> bytes:
    return pairing.encode_frame(
        pairing.T_RESUME, build_resume_body(device_id, key, challenge, nonce))


def encode_credential(device_id: str, key: bytes) -> bytes:
    raw = device_id.encode("ascii")
    if not 1 <= len(raw) <= 255:
        raise DeviceError("cihaz kimliği 1..255 bayt olmalı")
    if len(key) != DEVICE_KEY_LEN:
        raise DeviceError(f"cihaz anahtarı {DEVICE_KEY_LEN} bayt olmalı")
    return pairing.encode_frame(
        pairing.T_CREDENTIAL, bytes([len(raw)]) + raw + key)


def parse_credential(payload: bytes) -> tuple[str, bytes]:
    """İstemci tarafı: CREDENTIAL gövdesini çözer."""
    if len(payload) < 1:
        raise DeviceError("CREDENTIAL boş")
    id_len = payload[0]
    expected = 1 + id_len + DEVICE_KEY_LEN
    if id_len == 0 or len(payload) != expected:
        raise DeviceError(
            f"CREDENTIAL uzunluğu tutmuyor: {len(payload)}, beklenen {expected}")
    return (payload[1:1 + id_len].decode("ascii", errors="replace"),
            payload[1 + id_len:])


# ── Kayıt bileti ─────────────────────────────────────────────────────

class Ticket:
    """Tek kullanımlık kayıt bileti — QR'da taşınan şey.

    "Tek kullanımlık" burada **bir cihaz kaydı** demek: ilk başarılı
    kayıttan sonra bilet ölür ve aynı QR ikinci bir cihaz kaydettiremez.
    Ekranda unutulan ya da fotoğraflanan QR'ın penceresi böyle kapanıyor.
    """

    # Kod kaç yanlış denemeden sonra kilitlenir. 6 hane = 10^6, ve saldırgan
    # host'a bağlanmadan deneyemiyor; asıl kilit bu sayaç. Beş, kullanıcının
    # ekrandan yanlış okuyup düzeltmesine yer bırakıyor, saldırgana bilet
    # başına milyonda beş şans veriyor.
    MAX_CODE_ATTEMPTS = 5

    __slots__ = ("token", "code", "_spent", "_failures", "_code_locked",
                 "_lock")

    def __init__(self, token: bytes | None = None, code: str | None = None):
        import threading

        self.token = token if token is not None else pairing.generate_token()
        # Kamerası olmayan cihazın tek girişi. QR ile aynı bilete bağlı:
        # biri harcanınca ikisi de ölüyor.
        self.code = code if code is not None else pairing.generate_code()
        self._spent = False
        self._failures = 0
        self._code_locked = False
        # "Harcanmış mı" bakıp sonra harcamak bileşik bir işlem. İki telefon
        # aynı QR'ı aynı anda okutursa —dört oyunculu kurulumda gayet olası—
        # ikisi de kontrolü geçip ikisi de kaydolurdu; tek kullanımlık
        # biletin tek güvencesi tam da bunun olmaması.
        self._lock = threading.Lock()

    @property
    def spent(self) -> bool:
        with self._lock:
            return self._spent

    def try_spend(self) -> bool:
        """Bileti harcamayı dener. **Yalnızca ilk çağıran True alır.**"""
        with self._lock:
            if self._spent:
                return False
            self._spent = True
            return True

    @property
    def code_locked(self) -> bool:
        with self._lock:
            return self._code_locked

    def note_code_failure(self) -> bool:
        """Yanlış denemeyi sayar. Kod bu denemeyle kilitlendiyse True döner.

        **Kilit yalnızca KODU kapatıyor, bileti değil.** İlk tasarım bileti
        yakıyordu ve `test_e2e_host.test_failed_pairing_does_not_consume_a_slot`
        bunu yakaladı: LAN'daki biri beş yanlış deneme yapıp meşru telefonun
        QR'ını da öldürebiliyordu. Yani kaba kuvvete karşı koyarken elimizle
        bir hizmet dışı bırakma açmışız.

        Şimdi QR token'ı denemelerden etkilenmiyor; kamerası olan kullanıcı
        hiçbir şey fark etmiyor. Kilitlenen kod için çıkış yolu var ve
        kullanıcı zaten ekranın başında: bilgisayarda "New code".
        """
        with self._lock:
            self._failures += 1
            if self._failures >= self.MAX_CODE_ATTEMPTS and not self._code_locked:
                self._code_locked = True
                return True
            return False


# ── Defter ───────────────────────────────────────────────────────────

@dataclass
class DeviceRecord:
    device_id: str
    key: bytes
    name: str
    created: float
    last_seen: float


def default_store_path() -> str:
    """Platforma uygun defter yolu."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "VPad", "devices.json")
    return os.path.join(os.path.expanduser("~"), ".vpad", "devices.json")


class DeviceStore:
    """Kayıtlı cihazlar. Dosyaya yazar; saat ve yol dışarıdan verilebilir.

    Eşzamanlılık: `vpad_host` bağlantı başına bir thread çalıştırıyor ve
    hepsi aynı defteri görüyor. Bütün genel metotlar bu yüzden tek bir
    kilidin altında — `SlotPool` ile aynı gerekçe.
    """

    def __init__(self, path: str | None = None, ttl_days: int = TTL_DAYS,
                 clock=time.time):
        import threading

        self.path = path if path is not None else default_store_path()
        self.ttl_seconds = max(0, int(ttl_days)) * _SECONDS_PER_DAY
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, DeviceRecord] = {}
        with self._lock:
            self._load_locked()

    # -- kalıcılık ----------------------------------------------------

    def _load_locked(self) -> None:
        """Defteri dosyadan **yeniden** okur. `_lock` tutulurken çağrılır.

        Neden her işlemde: defter tek bir sürecin malı değil. Kullanıcı
        daemon çalışırken `--forget` çalıştırdığında o komut ayrı bir
        süreçte, ayrı bir `DeviceStore` ile dosyayı düzenliyor. Bellekteki
        kopyayı yeniden okumadan yazsaydık, daemon'ın bir sonraki `touch`u
        **silinen kaydı geri diriltirdi** — ve tam olarak bu oldu: iptal
        edilen cihaz yeniden bağlanabildi. (Birim testleri kaçırmıştı,
        gerçek CLI denemesi yakaladı.)

        Kalan risk, dürüst not: okuma ile yazma arasında dosya kilidi yok.
        İki süreç tam aynı anda yazarsa biri kaybeder. Kullanıcının elle
        `--forget` yazdığı senaryoda bu pencere pratikte yok; gerçek
        çok-süreçli kullanım gerekirse dosya kilidi eklenmeli.
        """
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            # Dosya gerçekten yok (ilk açılış ya da elle silinmiş):
            # bellekteki kopya da geçersiz.
            self._records.clear()
            return
        except (OSError, ValueError) as exc:
            # Bozuk ya da okunamayan defter: bellektekini **koruyoruz**.
            # Çalışan daemon'ın bütün cihazları bir anda unutması, bozuk
            # dosyadan çok daha kötü; bir sonraki yazım dosyayı onarır.
            print(f"!! cihaz defteri okunamadı ({exc}) — bellekteki kopya "
                  f"korunuyor", file=sys.stderr)
            return

        if not isinstance(raw, dict) or raw.get("version") != STORE_VERSION:
            print("!! cihaz defteri sürümü tanınmadı — bellekteki kopya "
                  "korunuyor", file=sys.stderr)
            return

        parsed: dict[str, DeviceRecord] = {}
        for entry in raw.get("devices", []):
            try:
                record = DeviceRecord(
                    device_id=str(entry["id"]),
                    key=bytes.fromhex(entry["key"]),
                    name=str(entry.get("name", "")),
                    created=float(entry.get("created", 0.0)),
                    last_seen=float(entry.get("last_seen", 0.0)),
                )
            except (KeyError, TypeError, ValueError):
                continue        # tek bozuk satır defteri düşürmesin
            if len(record.key) == DEVICE_KEY_LEN and record.device_id:
                parsed[record.device_id] = record
        self._records = parsed

    def _save_locked(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "devices": [
                {
                    "id": r.device_id,
                    "key": r.key.hex(),
                    "name": r.name,
                    "created": r.created,
                    "last_seen": r.last_seen,
                }
                for r in self._records.values()
            ],
        }
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        # Aynı klasöre geçici dosya + atomik yer değiştirme: yazma
        # ortasında elektrik giderse defter yarım kalmasın. Aynı klasör
        # şart, `os.replace` sürücüler arasında atomik değil.
        temp = f"{self.path}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            # Anahtarlar burada; başka kullanıcı okumasın.
            os.chmod(temp, 0o600)
        os.replace(temp, self.path)

    # -- sorgular -----------------------------------------------------

    def _expired(self, record: DeviceRecord, now: float) -> bool:
        if self.ttl_seconds <= 0:
            return False        # ttl_days=0 → hiç sönmesin
        return (now - record.last_seen) > self.ttl_seconds

    def devices(self) -> list[DeviceRecord]:
        """Süresi dolmamış kayıtlar, en son görülen önce."""
        with self._lock:
            self._load_locked()
            now = self._clock()
            alive = [r for r in self._records.values()
                     if not self._expired(r, now)]
        return sorted(alive, key=lambda r: r.last_seen, reverse=True)

    def sweep(self) -> int:
        """Süresi dolmuş kayıtları siler; silinen sayısını döner."""
        with self._lock:
            self._load_locked()
            now = self._clock()
            dead = [k for k, r in self._records.items()
                    if self._expired(r, now)]
            for key in dead:
                del self._records[key]
            if dead:
                self._save_locked()
        return len(dead)

    # -- değişiklikler ------------------------------------------------

    def enroll(self, name: str) -> DeviceRecord:
        """Yeni cihaz kaydeder ve kimliğini döner."""
        with self._lock:
            self._load_locked()
            now = self._clock()
            record = DeviceRecord(
                device_id=generate_device_id(),
                key=generate_device_key(),
                name=name,
                created=now,
                last_seen=now,
            )
            self._records[record.device_id] = record
            self._save_locked()
            return record

    def resolve(self, device_id: str) -> DeviceRecord | None:
        """Kimliği çözer. Yoksa **veya süresi dolmuşsa** None.

        Süresi dolmuş kayıt burada silinir: sönmüş bir anahtarın defterde
        durup bir sonraki `devices()` çağrısına kadar yaşaması anlamsız.
        """
        with self._lock:
            self._load_locked()
            record = self._records.get(device_id)
            if record is None:
                return None
            if self._expired(record, self._clock()):
                del self._records[device_id]
                self._save_locked()
                return None
            return record

    def touch(self, device_id: str) -> None:
        """`last_seen`'i tazeler — sönme sayacı buradan sıfırlanıyor."""
        with self._lock:
            self._load_locked()
            record = self._records.get(device_id)
            if record is None:
                return
            record.last_seen = self._clock()
            self._save_locked()

    def revoke(self, device_id: str) -> bool:
        """Kaydı siler. Gerçek iptal yolu bu."""
        with self._lock:
            self._load_locked()
            if device_id not in self._records:
                return False
            del self._records[device_id]
            self._save_locked()
            return True

    def revoke_all(self) -> int:
        with self._lock:
            self._load_locked()
            count = len(self._records)
            self._records.clear()
            self._save_locked()
            return count


# ── Kapı ─────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    """`AccessGate.accept` sonucu. Soketi çağıran yönetir."""
    ok: bool
    reject: bytes | None = None
    credential: bytes | None = None     # kayıt olduysa gönderilecek çerçeve
    record: DeviceRecord | None = None
    enrolled: bool = False              # True = yeni kayıt, False = süreklilik
    # Elle giriş kodu bu denemeyle kilitlendi. Host yalnız günlüğe yazıyor;
    # bilet ve QR yaşamaya devam ediyor. Sinyal olarak dönüyor çünkü
    # `AccessGate` I/O yapmıyor ve günlüğü de tanımıyor.
    code_locked: bool = False


class AccessGate:
    """CHALLENGE gönderir; **AUTH (kayıt)** veya **RESUME (süreklilik)** kabul eder.

    `pairing.PairingGate`'in yerini alır ve onunla aynı disiplini korur:
    **hiç I/O yapmaz.** Çerçeve üretir, gelen çerçeveyi değerlendirir;
    soket çağıranın işi.

    Kapı kapalıyken (`ticket is None and store is None`) hiçbir şey
    yapmaz — eşleşmesiz daemon davranışı birebir korunur.
    """

    __slots__ = ("_ticket", "_store", "_challenge", "_settled")

    def __init__(self, ticket: Ticket | None, store: DeviceStore | None):
        self._ticket = ticket
        self._store = store
        self._challenge: bytes | None = None
        self._settled = False

    @property
    def enabled(self) -> bool:
        return self._ticket is not None or self._store is not None

    @property
    def challenge(self) -> bytes | None:
        return self._challenge

    def opening_frame(self) -> bytes | None:
        if not self.enabled:
            return None
        self._challenge = pairing.make_challenge()
        self._settled = False
        return pairing.encode_challenge(self._challenge)

    def accept(self, msg_type: int, payload: bytes) -> Outcome:
        if not self.enabled:
            return Outcome(ok=True)
        if self._settled:
            raise DeviceError("kapı zaten karara bağlandı")
        if self._challenge is None:
            raise DeviceError("opening_frame() çağrılmadan accept() çağrıldı")
        self._settled = True

        if msg_type == pairing.T_RESUME:
            return self._resume(payload)
        if msg_type == pairing.T_AUTH:
            return self._enroll(payload)

        return Outcome(ok=False, reject=pairing.encode_reject(
            pairing.R_AUTH_REQUIRED,
            "bu host QR ile eşleşme istiyor; uygulamada QR'ı tarayın"))

    # -- süreklilik ---------------------------------------------------

    def _resume(self, payload: bytes) -> Outcome:
        if self._store is None:
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_DEVICE_UNKNOWN,
                "bu host cihaz kaydı tutmuyor; QR'ı tarayın"))
        try:
            device_id, nonce, mac = parse_resume_body(payload)
        except DeviceError:
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_AUTH_FAILED, "RESUME çerçevesi bozuk"))

        record = self._store.resolve(device_id)
        if record is None:
            # "Bilinmiyor" ve "yanlış anahtar" AYRI sebepler: istemci
            # birincide QR ekranına düşmeli, ikincide düşmemeli.
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_DEVICE_UNKNOWN,
                "cihaz kaydı yok veya süresi doldu; QR'ı yeniden tarayın"))

        try:
            expected = compute_resume(record.key, self._challenge, nonce)
        except DeviceError:
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_AUTH_FAILED, "RESUME doğrulaması başarısız"))
        if not hmac.compare_digest(mac, expected):
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_AUTH_FAILED, "RESUME doğrulaması başarısız"))

        self._store.touch(device_id)
        return Outcome(ok=True, record=record, enrolled=False)

    # -- kayıt --------------------------------------------------------

    def _enroll(self, payload: bytes) -> Outcome:
        ticket = self._ticket
        if ticket is None:
            # `vpad_host` her kayıttan sonra hemen yeni bilet açtığı için
            # buraya normalde düşülmez; düşülürse mesaj GERÇEKTEN yapılabilir
            # olanı söylemeli. (Eskiden "host'ta yeni QR üretin" diyordu ama
            # üretmenin yolu yoktu — kullanıcıyı çıkmaza sokuyordu.)
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_AUTH_REQUIRED,
                "host şu an kayıt kabul etmiyor; bilgisayarda yeni QR isteyin"))
        if ticket.spent:
            # Tek kullanımlık biletin tüm anlamı burada. Fotoğraflanmış
            # QR ikinci cihazı kaydettiremiyor.
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_AUTH_REQUIRED,
                "bu QR kullanıldı; host'ta yeni QR üretin"))
        # İki sır da aynı bilete ait: QR'daki token ve ekrandaki 6 haneli
        # kod. Hangisinin denendiğini istemci söylemiyor, host ikisini de
        # deniyor — tel protokolü böylece hiç değişmedi ve yayındaki
        # istemciler etkilenmedi.
        #
        # Token ÖNCE: kod kilitliyken bile QR çalışmaya devam etmeli.
        ok = pairing.verify_auth(ticket.token, self._challenge, payload)
        if not ok and not ticket.code_locked:
            ok = pairing.verify_auth(
                pairing.secret_from_code(ticket.code), self._challenge, payload)
        if not ok:
            # 6 hane tek başına zayıf (10^6); onu kapatan şey bu sayaç.
            if ticket.note_code_failure():
                return Outcome(ok=False, code_locked=True,
                               reject=pairing.encode_reject(
                                   pairing.R_AUTH_FAILED,
                                   "kod çok kez yanlış girildi; "
                                   "bilgisayardan yeni kod isteyin"))
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_AUTH_FAILED, "eşleşme doğrulaması başarısız"))

        # MAC'ten SONRA harcanıyor: yanlış token'lı bir istemci meşru
        # telefonun biletini yakamamalı. Yarışı `try_spend` kesiyor —
        # yukarıdaki `spent` kontrolü hızlı yol, karar burada.
        if not ticket.try_spend():
            return Outcome(ok=False, reject=pairing.encode_reject(
                pairing.R_AUTH_REQUIRED,
                "bu QR az önce başka bir cihaz tarafından kullanıldı"))
        if self._store is None:
            # Defter yoksa kayıt da yok: bilet doğrulandı, oturum açılır
            # ama süreklilik verilmez. `--pair` + `--no-remember` yolu.
            return Outcome(ok=True, enrolled=True)

        record = self._store.enroll(name="")
        return Outcome(ok=True, credential=encode_credential(
            record.device_id, record.key), record=record, enrolled=True)
