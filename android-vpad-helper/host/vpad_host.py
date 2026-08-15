#!/usr/bin/env python3
"""
V-Pad host — kendi companion daemon'ımız.

`vpad_daemon.py`'nin yerine geçer. Fark tek satırda: **çoklu oyuncu ve QR
eşleşmesi yamayla eklenen değil, doğuştan var olan davranış.** Artık başka
bir depoya yama göndermeden slot mantığını değiştirebiliyoruz.

Neden ayrı bir dosya
────────────────────
Referans daemon tek telefonu modül düzeyinde bir `threading.Lock()` ile
zorluyordu (`_active_lock`). Slot havuzuna geçmek o kilidi, `_active_peer`
global'ini, `handle_client` imzasını ve `finally` bloğunu birden
değiştirmeyi gerektiriyor — yani dosyanın omurgasını. Bunu başkasının
deposunda yama olarak taşımak iki kez denendi ve ikisinde de yama gerçek
dosyaya uymadı. Kendi dosyamızda bu bir tasarım kararı, taşınacak bir
fark değil.

Telde hiçbir şey değişmiyor
───────────────────────────
Yayındaki Android 2.0.0 istemcisi bu host'la konuşur; protokol
`docs/companion-daemon.md` proto_ver=1'dir. İstemci T_SLOT ve
CHALLENGE/AUTH çerçevelerini zaten tanıyor (`WifiGamepadClient.kt`), bu
yüzden host tarafı açıldığı anda çalışır.

⚠ Eksen biçimi — belge değil kod haklı
──────────────────────────────────────
`docs/companion-daemon.md` §4.4 çubukları "signed int8, +Y = yukarı" diye
tarif ediyor. **Bu belge bayat.** 2026-06-13'teki signed→unsigned
dönüşünden beri tel şöyle:

    çubuklar/tetikler = işaretsiz 0..255, merkez 128, +Y = AŞAĞI

Hakem `HidReportSender.kt` (satır 14-21 + 97) — WiFi taşıyıcısı aynı 8
baytı gönderiyor. Yeni bir host yazan herkes bu tuzağa düşer; XInput'a
çevirirken Y **negatiflenmek** zorunda.

Enjeksiyon arka uçları
──────────────────────
    vigem   Windows. ViGEmBus üzerinden gerçek sanal Xbox 360 pad'i,
            **slot başına bir tane**. Gerekli: ViGEmBus sürücüsü +
            `pip install vgamepad`.
    log     Her OS. Çözer ve basar, enjekte etmez.

macOS klavye/fare öykünmesi bilinçli olarak DIŞARIDA: dört oyuncunun tek
klavyeye basması anlamsız ve elimizde test edecek Mac yok. macOS'ta
`svolkancav/vpad-helper` referans daemon'ı tek oyuncu için çalışmaya
devam ediyor.

Gerekli:
    pip install zeroconf          # mDNS yayını
    pip install vgamepad          # yalnız Windows vigem arka ucu
    pip install qrcode            # opsiyonel, --pair QR çizimi

Kullanım:
    python3 vpad_host.py                        # tek oyuncu, eşleşme kapalı
    python3 vpad_host.py --players 4            # dört telefon
    python3 vpad_host.py --players 2 --pair     # QR zorunlu
    python3 vpad_host.py --inject log -v        # yalnız çöz ve bas

────────────────────────────────────────────────────────────────────────
Bu dosyanın bir bölümü `svolkancav/vpad-helper` (MIT) içindeki
`vpad_daemon.py`'den uyarlanmıştır: `short_hostname`, `sanitize_instance`,
`lan_addresses`, `frame_reader`, HAT eşlemesi ve ViGEm eksen matematiği.
Sahadan öğrenilmiş şeyler; yeniden keşfetmek için sebep yok.
"""
from __future__ import annotations

import argparse
import dataclasses
import socket
import struct
import sys
import threading
from contextlib import closing
from datetime import datetime

# zeroconf yalnızca `main()` için gerekli. İçe aktarma başarısızlığı burada
# süreci öldürmüyor ki protokol ve slot mantığı mDNS kurulu olmadan da içe
# aktarılıp test edilebilsin (`test_e2e_host.py` tam olarak bunu yapıyor).
try:
    from zeroconf import IPVersion, ServiceInfo, Zeroconf
except ImportError:  # pragma: no cover — kurulumda var, testte gereksiz
    IPVersion = ServiceInfo = Zeroconf = None

import vpad_devices as devices
import vpad_pairing as pairing
import vpad_slots as slots

# Protokol sabitleri tek kaynaktan geliyor: vpad_pairing. İki yerde
# tanımlamak, birini güncelleyip diğerini unutmanın kısa yolu.
from vpad_pairing import (
    MAX_FRAME,
    R_AUTH_FAILED,
    R_AUTH_REQUIRED,
    R_DEVICE_UNKNOWN,
    T_HELLO,
    T_HELLO_ACK,
    encode_frame,
    encode_reject,
)

PROTO_VER = 1

T_REPORT = 0x02
T_MOUSE = 0x05
T_PING = 0x03
T_PONG = 0x12
T_BYE = 0x04

R_VERSION_MISMATCH = 0x01
R_IN_USE = 0x02
R_INTERNAL_ERROR = 0xFF

# RFC 6763 §7.2 hizmet adı etiketini 15 karakterle sınırlıyor; özgün
# `_universalgamepad` 16'ydı ve zeroconf yayınlamayı reddediyordu.
# Info.plist ve Kotlin istemcisiyle birlikte 2026-07-27'de değişti.
SERVICE_TYPE = "_vpad-bridge._tcp.local."

# Bayt [0] bitleri 0..7
BTN_A, BTN_B, BTN_X, BTN_Y = 0x01, 0x02, 0x04, 0x08
BTN_L1, BTN_R1, BTN_SELECT, BTN_START = 0x10, 0x20, 0x40, 0x80
# Bayt [1] bitleri 0..2 (4-7 hat nibble'ı)
BTN_L3, BTN_R3, BTN_HOME = 0x01, 0x02, 0x04

HAT_NAMES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
BTN_LOW_NAMES = ["A", "B", "X", "Y", "L1", "R1", "Sel", "Sta"]
BTN_HIGH_NAMES = ["L3", "R3", "Home"]

# Hat yönü → (yukarı, sağ, aşağı, sol). 8 = bırakıldı.
HAT_VECTORS = {
    0: (True, False, False, False),
    1: (True, True, False, False),
    2: (False, True, False, False),
    3: (False, True, True, False),
    4: (False, False, True, False),
    5: (False, False, True, True),
    6: (False, False, False, True),
    7: (True, False, False, True),
}


# ── Günlük ───────────────────────────────────────────────────────────
#
# Dört bağlantı dört thread demek. `print` tek çağrıda iki yazma yapar
# (metin + satır sonu), yani kilitsiz bırakılırsa satırlar birbirine
# girer. Tek kilit, tek yazma.

_log_lock = threading.Lock()


def _force_utf8_console() -> None:
    """stdout/stderr'i UTF-8'e sabitler.

    **Bu bir kozmetik ayar değil, çökme önlemi.** Windows'ta Python konsol
    akışlarını sistemin eski ANSI kod sayfasıyla açıyor (Türkçe kurulumda
    cp1254). Aşağıdaki günlük satırları ◇ ▤ ⇄ gibi karakterler taşıyor;
    cp1254 bunları kodlayamıyor ve `print` `UnicodeEncodeError` fırlatıyor.
    O istisna `handle_client`'ın ilk satırında, `try` bloğuna GİRMEDEN
    önce patlıyor — yani bağlantı thread'i el sıkışmaya varamadan ölüyor
    ve telefon zaman aşımı görüyor.

    Türkçe bir Windows'ta `python vpad_host.py` ile bu, ilk bağlantıda
    %100 tekrarlanıyordu.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            # Yeniden yönlendirilmiş ya da sarmalanmış akış. `log()`
            # içindeki ikinci savunma yine de tutuyor.
            pass


_force_utf8_console()


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _emit(line: str) -> None:
    """Tek yazma, kilit altında; kodlanamazsa da ASLA yükselmez.

    Günlüğe satır basamamak, oyunun ortasındaki bir oturumu düşürmek için
    geçerli bir sebep değil. `_force_utf8_console` işini yaptıysa buraya
    hiç düşülmüyor; düşülürse sembolleri kaybederiz, oturumu değil.
    """
    with _log_lock:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(sys.stdout, "encoding", None) or "ascii"
            safe = line.encode(encoding, "replace").decode(encoding, "replace")
            try:
                print(safe, flush=True)
            except Exception:
                pass
        except Exception:
            pass


def log(message: str) -> None:
    _emit(f"[{ts()}] {message}")


def log_raw(message: str = "") -> None:
    _emit(message)


# ── mDNS ad hijyeni ──────────────────────────────────────────────────

def short_hostname() -> str:
    """`socket.gethostname()`, alan adı kısmı atılmış hâlde.

    Yönlendirici sık sık `unknown5a84b66a8be3.home` gibi bir ad dağıtır ve
    macOS bunu olduğu gibi bildirir. DNS'te nokta etiket ayırıcısı olduğu
    için iki şey birden bozulur: `server=` alanı var olmayan bir host'a
    döner, hizmet adı ikinci bir etiket kazanır. Windows'ta hiç
    görülmedi — bilgisayar adlarında nokta olamıyor.
    """
    return socket.gethostname().split(".")[0] or "computer"


def sanitize_instance(name: str) -> str:
    """Adı DNS-SD örnek adı olarak kullanılabilir hâle getirir.

    RFC 6763 §4.1.1 örnek adını **tek bir DNS etiketi** sayar. İçindeki
    nokta fazladan etiket üretir, kayıt artık
    `<Ad>._vpad-bridge._tcp.local` ile eşleşmez ve tarayan istemci —
    iOS'un NWBrowser'ı dâhil — onu sessizce atar. Daemon sağlıklı görünür
    ama görünmez olur.

    Nokta silinmiyor, tireye çevriliyor: adları yalnız noktadan sonra
    ayrılan iki makine yine ayrı örnek alsın. Sonuç 63 baytla sınırlanıyor
    (varsayılan ad em dash taşıdığı için sayım UTF-8 üzerinden).
    """
    cleaned = name.replace(".", "-").strip() or "V-Pad host"
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= 63:
        return cleaned
    return encoded[:63].decode("utf-8", "ignore").rstrip("- ")


# ── Adres keşfi ──────────────────────────────────────────────────────

# Sanal noktadan-noktaya bağlar (VPN, tünel). Adresleri en sona konur ki
# sırayla deneyen telefon önce gerçek LAN'a ulaşsın.
_TUNNEL_PREFIXES = ("utun", "tun", "tap", "ppp", "wg", "ipsec", "gpd", "zt")


def default_route_ip() -> str:
    """Varsayılan rotanın kaynak adresi — yalnızca son çare.

    *LAN adresi* olarak güvenilmez: VPN açıkken rota tünelden geçer ve bu
    fonksiyon tünel adresini döndürür (10.8.0.2 gibi), telefon ise
    192.168.1.x'tedir. Bu uyuşmazlık, hiçbir telefonun ulaşamayacağı bir
    Bonjour A kaydı yayınlar.
    """
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
        try:
            # UDP soketini connect etmek hiçbir şey göndermez; yalnızca
            # rotanın kaynak adresini bağlar. Hedef IP keyfî.
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def lan_addresses(override: str | None = None) -> list[str]:
    """Loopback olmayan tüm IPv4 adresleri, gerçek LAN arayüzleri önce.

    Hepsi Bonjour A kayıtlarına girer: host telefonun hangi subnet'te
    olduğunu bilemez, istemci hizmet adını tüm adres kümesine çözer ve
    ulaşılabilir olanı seçer. Tek bir adres tahmin edip yayınlamak,
    VPN arkasında keşfi bozan şeyin ta kendisi.

    `ifaddr` zeroconf'un zaten sert bağımlılığı — ek kurulum yok.
    """
    if override:
        return [override]

    try:
        import ifaddr  # noqa: PLC0415 — zeroconf ile birlikte geliyor
    except ImportError:
        return [default_route_ip()]

    physical: list[str] = []
    tunnels: list[str] = []
    for adapter in ifaddr.get_adapters():
        name = (adapter.name or "").lower()
        bucket = tunnels if name.startswith(_TUNNEL_PREFIXES) else physical
        for addr in adapter.ips:
            if not addr.is_IPv4:
                continue
            ip = addr.ip
            if not isinstance(ip, str) or ip.startswith("127."):
                continue
            # 169.254/16 = APIPA: DHCP kirası hiç gelmemiş adaptör.
            # Windows'ta Hyper-V, WSL ve VPN kalıntıları birkaç tane
            # bırakır. Hiçbir telefon ulaşamaz; yayınlamak istemciye
            # yalnızca ölü adresleri önce denetir.
            if ip.startswith("169.254."):
                continue
            if ip not in bucket:
                bucket.append(ip)
    ordered = physical + tunnels
    return ordered or [default_route_ip()]


# ── Çerçeve okuma ────────────────────────────────────────────────────

def encode_hello_ack(server_ver: int = PROTO_VER, accept: bool = True) -> bytes:
    return encode_frame(T_HELLO_ACK,
                        struct.pack("BB", server_ver, 1 if accept else 0))


def encode_pong() -> bytes:
    return encode_frame(T_PONG)


def encode_bye() -> bytes:
    return encode_frame(T_BYE)


def frame_reader(sock: socket.socket):
    """Tamamlanan çerçeveleri (tip, payload) olarak üretir.

    Kısmi okumaları tamponlar ve tek recv içinde gelen birden fazla
    çerçeveyi ayırır. TCP mesaj sınırı vermez (spec §3).
    """
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("karşı taraf soketi kapattı")
        buf.extend(chunk)

        while len(buf) >= 3:
            length = buf[0] | (buf[1] << 8)
            if length < 3:
                raise ConnectionError(f"küçük çerçeve: length={length}")
            if length > MAX_FRAME:
                raise ConnectionError(f"büyük çerçeve: length={length}")
            if len(buf) < length:
                break
            msg_type = buf[2]
            payload = bytes(buf[3:length])
            del buf[:length]
            yield msg_type, payload


def decode_hello(payload: bytes) -> tuple[int, str, str]:
    """HELLO'yu çözer (spec §4.1)."""
    if len(payload) < 2:
        raise ValueError("HELLO çok kısa")
    proto_ver = payload[0]
    name_len = payload[1]
    if len(payload) < 2 + name_len + 1:
        raise ValueError("HELLO adı kesik")
    name = payload[2:2 + name_len].decode("utf-8", errors="replace")
    skin_idx = 2 + name_len
    skin_len = payload[skin_idx]
    if len(payload) < skin_idx + 1 + skin_len:
        raise ValueError("HELLO skin'i kesik")
    skin = payload[skin_idx + 1:skin_idx + 1 + skin_len].decode(
        "utf-8", errors="replace")
    return proto_ver, name, skin


@dataclasses.dataclass
class Report:
    """Çözülmüş tek REPORT çerçevesi (spec §4.4).

    Çubuklar ve tetikler **işaretsiz 0..255**, çubuklar 128'de dinlenir.
    Bu, telin birebir yansıttığı Android HID rapor düzeni
    (`HidReportSender.kt`, signed→unsigned dönüşü 2026-06-13). Y **aşağı**
    büyür (ekran yönü) — XInput tarzı bir pad'e giden arka uçlar onu
    negatiflemek zorunda.
    """
    btn_low: int
    btn_high: int
    lx: int
    ly: int
    rx: int
    ry: int
    lt: int
    rt: int

    @property
    def hat(self) -> int:
        return (self.btn_high >> 4) & 0x0F

    def pressed(self, mask: int, high: bool = False) -> bool:
        return bool((self.btn_high if high else self.btn_low) & mask)

    @classmethod
    def decode(cls, payload: bytes) -> "Report":
        if len(payload) != 8:
            raise ValueError(f"REPORT 8 bayt olmalı, {len(payload)} geldi")
        return cls(*struct.unpack("BBBBBBBB", payload))

    def pretty(self) -> str:
        low = [n for i, n in enumerate(BTN_LOW_NAMES) if self.btn_low & (1 << i)]
        high = [n for i, n in enumerate(BTN_HIGH_NAMES)
                if self.btn_high & (1 << i)]
        names = "+".join(low + high) if (low or high) else "—"
        hat = self.hat
        hat_label = HAT_NAMES[hat] if 0 <= hat <= 7 else "·"
        return (f"btn=[{names:<22s}] hat={hat_label:<3s} "
                f"L=({self.lx - 128:+4d},{self.ly - 128:+4d}) "
                f"R=({self.rx - 128:+4d},{self.ry - 128:+4d}) "
                f"LT={self.lt:3d} RT={self.rt:3d}")


# ── Enjeksiyon ───────────────────────────────────────────────────────

class Injector:
    """Çözülmüş raporu host girdi olayına çevirir."""

    name = "yok"
    tag = "none"

    def apply(self, report: Report) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        """Her şeyi bırakır. İstemci koptuğunda çağrılır — olmazsa basılı
        tuşla kopan telefon host'ta tuşu basılı bırakır."""

    def close(self) -> None:
        self.reset()


class LogInjector(Injector):
    """Yalnız çöz ve bas. `--inject log`."""

    name = "yalnız günlük (enjeksiyon yok)"
    tag = "test"

    def __init__(self, verbose: bool = False, slot: int = 0):
        self.verbose = verbose
        self.slot = slot
        self.count = 0

    def apply(self, report: Report) -> None:
        self.count += 1
        if self.verbose or self.count % 60 == 1:
            log(f"▤ P{self.slot + 1} REPORT #{self.count}: {report.pretty()}")


class VigemInjector(Injector):
    """Windows: ViGEmBus üzerinden gerçek sanal Xbox 360 pad'i."""

    name = "ViGEmBus sanal Xbox 360 pad"
    tag = "vigem"

    def __init__(self):
        import vgamepad as vg  # noqa: PLC0415 — opsiyonel, yalnız Windows

        self._vg = vg
        self._pad = vg.VX360Gamepad()
        self._buttons = {
            BTN_A: vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
            BTN_B: vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
            BTN_X: vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
            BTN_Y: vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
            BTN_L1: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
            BTN_R1: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
            BTN_SELECT: vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
            BTN_START: vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        }
        self._buttons_high = {
            BTN_L3: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
            BTN_R3: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
            BTN_HOME: vg.XUSB_BUTTON.XUSB_GAMEPAD_GUIDE,
        }
        self._dpad = (
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
            vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        )

    @staticmethod
    def _to_short(value: int) -> int:
        """İşaretsiz 0..255 (merkez 128) → işaretli 16 bit.

        258 ≈ 65535/254, yani 0 → -32768, 128 → 0, 255 → 32766. Üst uçtaki
        1 birimlik eksik kasıtlı değil, ölçeğin artığı — 32767 yerine
        32766, tam bastırılmış çubukta %0,003 fark demek. Katsayıyı
        büyütmek merkeze yakın her değeri bozar; bırakıldı.
        """
        return max(-32768, min(32767, (value - 128) * 258))

    @staticmethod
    def _to_short_inverted(value: int) -> int:
        # X360 çubuklarında +Y = yukarı; telde +Y = aşağı. Önce negatifle,
        # sonra kırp: -(-32768) SHORT aralığını taşırırdı.
        return max(-32768, min(32767, -VigemInjector._to_short(value)))

    def apply(self, report: Report) -> None:
        pad = self._pad
        pad.reset()

        for mask, button in self._buttons.items():
            if report.pressed(mask):
                pad.press_button(button=button)
        for mask, button in self._buttons_high.items():
            if report.pressed(mask, high=True):
                pad.press_button(button=button)

        hat = HAT_VECTORS.get(report.hat)
        if hat:
            for active, button in zip(hat, self._dpad):
                if active:
                    pad.press_button(button=button)

        pad.left_joystick(
            x_value=self._to_short(report.lx),
            y_value=self._to_short_inverted(report.ly))
        pad.right_joystick(
            x_value=self._to_short(report.rx),
            y_value=self._to_short_inverted(report.ry))
        pad.left_trigger(value=report.lt)
        pad.right_trigger(value=report.rt)
        pad.update()

    def reset(self) -> None:
        try:
            self._pad.reset()
            self._pad.update()
        except Exception:
            pass


class PadSet:
    """Slot başına bir enjektör; **ilk ihtiyaçta** yaratılır.

    Neden önden dört tane değil: `VX360Gamepad()` yaratmak pad'i o anda
    Windows'a takar. Dördünü açılışta kurmak, tek telefon bağlıyken bile
    oyunlara dört kumanda gösterir — bir kısmı ilk pad'i değil ilk *boş*
    pad'i seçer, kullanıcı da neden hiçbir şeyin çalışmadığını anlamaz.

    Kopan istemcinin pad'i **yok edilmez**: `SlotPool` yapışkan, yani aynı
    telefon dönünce aynı slota oturur ve aynı pad'i bulur. Yeniden takıp
    çıkarmak oyunlarda "kumanda çıkarıldı" iletisi tetikler.
    """

    def __init__(self, backend: str, verbose: bool):
        self._backend = backend
        self._verbose = verbose
        self._pads: dict[int, Injector] = {}
        # ViGEm hedefi tahsisi tek bir sanal veriyoluna dokunuyor; iki
        # telefon aynı anda el sıkışırsa iki thread aynı anda yaratır.
        self._lock = threading.Lock()

    def acquire(self, slot: int) -> Injector:
        with self._lock:
            existing = self._pads.get(slot)
            if existing is not None:
                return existing
            injector = self._build(slot)
            self._pads[slot] = injector
            return injector

    def _build(self, slot: int) -> Injector:
        if self._backend != "vigem":
            return LogInjector(self._verbose, slot)
        try:
            injector = VigemInjector()
            log(f"⊕ P{slot + 1} için sanal pad yaratıldı")
            return injector
        except Exception as exc:
            # Buraya düşmek genelde "ViGEmBus sürücüsü kurulu değil"
            # demektir. Açılışta bir kez denendiği için burada tek bir
            # slot düşüyor; oturum günlükle sürer, oyun kumanda görmez.
            log(f"⚠ P{slot + 1} sanal pad yaratılamadı ({exc}) — günlüğe düşüldü")
            return LogInjector(self._verbose, slot)

    def close(self) -> None:
        with self._lock:
            for injector in self._pads.values():
                try:
                    injector.close()
                except Exception:
                    pass
            self._pads.clear()


def resolve_backend(preference: str) -> str:
    """`--inject` seçimini somut arka uca indirger, sesli şekilde düşerek."""
    choice = preference
    if choice == "auto":
        choice = "vigem" if sys.platform == "win32" else "log"

    if choice != "vigem":
        return "log"

    if sys.platform != "win32":
        log_raw("!! --inject vigem yalnız Windows'ta çalışır — günlüğe düşüldü.")
        return "log"

    try:
        import vgamepad  # noqa: F401, PLC0415 — yalnızca varlık sınaması
    except ImportError:
        log_raw("!! vgamepad kurulu değil — yalnız günlük.")
        log_raw("   Düzeltilene kadar oyunlarda HİÇBİR KUMANDA görünmez.")
        log_raw("   Kaynaktan: pip install vgamepad")
        return "log"
    except Exception as exc:
        log_raw(f"!! vgamepad içe aktarılamadı ({exc}) — yalnız günlük.")
        return "log"
    return "vigem"


# ── Bağlantı yaşam döngüsü ───────────────────────────────────────────

@dataclasses.dataclass
class SessionStats:
    reports: int = 0
    pings: int = 0
    dropped: int = 0
    bytes_in: int = 0


class TicketDesk:
    """Tek kullanımlık kayıt biletlerini yönetir ve QR'ı ekrana basar.

    Bilet **tek cihaz** kaydeder (user kararı 2026-08-14): ekranda unutulmuş
    ya da fotoğraflanmış bir kare ikinci cihazı içeri alamaz. Bir bilet
    harcandığında masa **hemen yenisini açar** — güvence "bir QR = bir
    cihaz", "toplamda N cihaz" değil.

    ⚠️ **Bütçe kaldırıldı (2026-08-15).** Önce auto-mint `--players` ile
    sınırlıydı; o sınıra varınca açık bilet kalmıyordu. Sahada kilitledi:
    istemci `RESUME` bilmediği için her yeniden bağlanışta YENİDEN
    kaydoluyor ve bir bilet daha yakıyor, yani `--players 2` iki
    bağlantıdan sonra kapıyı kapatıyordu. Üstelik ret mesajı "host'ta yeni
    QR üretin" diyordu ama üretmenin bir yolu yoktu — kullanıcıyı olmayan
    bir düğmeye yönlendiren çıkmaz. Sayaç yalnızca günlük için duruyor.

    Kayıtlı cihazlar bundan etkilenmez — onlar bilet değil `RESUME`
    kullanıyor.
    """

    def __init__(self, address: str, port: int, max_enrollments: int = 0):
        self._address = address
        self._port = port
        # Yalnızca bilgilendirme amaçlı; kapıyı kapatmıyor (bkz. sınıf notu).
        self._players = max(1, max_enrollments)
        self._enrolled = 0
        self._ticket: devices.Ticket | None = devices.Ticket()
        self._lock = threading.Lock()

    def current(self) -> devices.Ticket | None:
        with self._lock:
            return self._ticket

    def announce(self) -> None:
        """Açık biletin QR'ını basar."""
        ticket = self.current()
        if ticket is None:                      # savunma; normalde olmaz
            log("Açık kayıt bileti yok — yeni QR için Enter'a basın.")
            return
        payload = pairing.build_payload(self._address, self._port, ticket.token)
        log_raw()
        log("Telefonla aşağıdaki QR'ı tarayın (TEK cihaz kaydeder):")
        log_raw()
        log_raw(pairing.render_qr_terminal(payload))
        log(f"Eşleştirme    : {payload}")

    def mint(self) -> None:
        """Elle yeni bilet: eskisi ölür, yenisi basılır.

        Kullanıcının "yeni QR üret" isteğinin gerçek karşılığı. Kaybolan
        telefonu iptal ettikten sonra ya da ekranı başkası gördüğünde
        biletin tazelenmesi için.
        """
        with self._lock:
            self._ticket = devices.Ticket()
        log("◈ yeni kayıt bileti üretildi — önceki QR öldü")
        self.announce()

    def note_enrollment(self) -> None:
        """Bir cihaz kaydoldu: bileti kapat, hemen yenisini aç."""
        with self._lock:
            self._enrolled += 1
            self._ticket = devices.Ticket()
            count = self._enrolled
        log(f"◈ kayıt tamamlandı ({count}. cihaz) — bu QR öldü, yenisi altta")
        self.announce()


def handle_client(client: socket.socket, addr, pads: PadSet, pool: slots.SlotPool,
                  desk: "TicketDesk | None", store: "devices.DeviceStore | None",
                  verbose: bool) -> None:
    # Spec §1: iki uçta da TCP_NODELAY — 4 ms / 8 ms rapor temposu Nagle
    # tarafından birleştirilmemeli.
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    client.settimeout(5.0)   # el sıkışma penceresi

    log(f"◇ bağlantı: {addr[0]}:{addr[1]}")
    stats = SessionStats()

    # `lease` ve `injector` `try` içinde atanıyor ama `finally` içinde
    # okunuyor. Sürüm uyuşmazlığı gibi erken dönüşlerde atama hiç
    # çalışmaz; önceden bağlamak `NameError`'ın asıl hatayı maskelemesini
    # önlüyor.
    lease: slots.SlotLease | None = None
    injector: Injector | None = None

    try:
        reader = frame_reader(client)

        # ── Erişim kapısı ────────────────────────────────────────────
        # Slot ALINMADAN önce çalışır. Aksi hâlde LAN'daki biri sadece
        # bağlanıp slotu tutarak meşru telefonu dışarıda bırakabilirdi.
        #
        # İki yol var: **AUTH** (QR bileti ile ilk kayıt) ve **RESUME**
        # (kayıtlı cihazın QR'sız girişi). İkincisi "oturum sürekliliği".
        gate = devices.AccessGate(
            desk.current() if desk is not None else None, store)
        opening = gate.opening_frame()
        if opening is not None:
            client.sendall(opening)
            msg_type, payload = next(reader)
            stats.bytes_in += 3 + len(payload)
            outcome = gate.accept(msg_type, payload)
            if not outcome.ok:
                client.sendall(outcome.reject)
                reason = outcome.reject[3]
                label = ("kayıt bileti gerekli" if reason == R_AUTH_REQUIRED
                         else "doğrulama başarısız" if reason == R_AUTH_FAILED
                         else "cihaz tanınmıyor" if reason == R_DEVICE_UNKNOWN
                         else f"0x{reason:02x}")
                log(f"✗ erişim reddedildi ({label}) — {addr[0]}")
                return

            if outcome.credential is not None:
                # Kimliği HEMEN gönderiyoruz. İstemci HELLO'yu henüz
                # göndermedi; çerçeve tamponda bekler ve `readUntil` onu
                # yan kanal olarak toplar. Yayındaki 2.0.0 tanımadığı için
                # sessizce atar — kırılmaz, sadece süreklilik kazanmaz.
                client.sendall(outcome.credential)

            if outcome.enrolled:
                short = (outcome.record.device_id[:8] if outcome.record
                         else "—")
                log(f"🔒 YENİ CİHAZ kaydedildi (id={short}…) — {addr[0]}")
                if desk is not None:
                    desk.note_enrollment()
            else:
                short = outcome.record.device_id[:8] if outcome.record else "—"
                log(f"🔓 kayıtlı cihaz girdi (id={short}…) — {addr[0]}")

        msg_type, payload = next(reader)
        stats.bytes_in += 3 + len(payload)
        if msg_type != T_HELLO:
            client.sendall(encode_reject(
                R_INTERNAL_ERROR,
                f"önce HELLO bekleniyordu, type=0x{msg_type:02x} geldi"))
            log(f"✗ ilk çerçeve type=0x{msg_type:02x}, HELLO değil — reddedildi")
            return

        proto_ver, pad_name, skin = decode_hello(payload)
        log(f"▸ HELLO proto_ver={proto_ver} name={pad_name!r} skin={skin!r}")

        if proto_ver != PROTO_VER:
            client.sendall(encode_reject(
                R_VERSION_MISMATCH,
                f"host v{PROTO_VER} konuşuyor, istemci v{proto_ver}"))
            log("✗ sürüm uyuşmazlığı — reddedildi")
            return

        # Slot ACK'ten önce alınır: havuz doluysa telefon net bir
        # REJECT(in_use) görsün, ACK'in ardından sessiz bir kopuş değil.
        lease = pool.acquire(f"{pad_name} @ {addr[0]}")
        if lease is None:
            client.sendall(encode_reject(
                R_IN_USE, f"{pool.size} oyuncu slotunun tamamı dolu"))
            log(f"✗ reddedildi — {pool.size} slot dolu (in_use)")
            return
        injector = pads.acquire(lease.index)

        # SLOT yalnızca ÇOKLU oyuncuda gönderiliyor.
        #
        # Tek oyuncuda göndermemek bilinçli bir uyumluluk kararı: referans
        # daemon bu çerçeveyi hiç göndermiyor ve `docs/companion-daemon.md`
        # §4.9 da "tek oyunculu daemon'lar hiç göndermez" diyor. Denetimimiz
        # dışında bir istemci —App Store'daki iOS uygulaması gibi— hiç
        # karşılaşmadığı bir çerçeveyle sınanmamış olabilir. Varsayılan
        # `--players 1` yapılandırmasında telde referans daemon'la
        # **birebir aynı** baytları üretmek en güvenli davranış.
        #
        # Tek yazım da bilinçli: ayrı ayrı da doğru çalışır, ama aynı
        # segmentte gitmek istemcinin bloklamayan slot tahliyesine denk
        # düşer — "Oyuncu 2" rozeti ilk karede görünür.
        ack = encode_hello_ack(PROTO_VER, accept=True)
        if pool.size > 1:
            ack += pairing.encode_slot(lease.index)
        client.sendall(ack)
        log(f"◂ HELLO_ACK{f' + SLOT={lease.index}' if pool.size > 1 else ''} "
            f"(P{lease.index + 1}) → {pool.free}/{pool.size} slot boş")
        log(f"▶ P{lease.index + 1} enjeksiyon: {injector.name}")

        # El sıkışma sonrası: oynarken REPORT, boştayken 2 sn'de bir PING.
        # 10 sn ikisini de kapsar, Wi-Fi kesintisine de pay bırakır.
        client.settimeout(10.0)

        for msg_type, payload in reader:
            stats.bytes_in += 3 + len(payload)

            if msg_type == T_REPORT:
                stats.reports += 1
                try:
                    report = Report.decode(payload)
                except ValueError as exc:
                    stats.dropped += 1
                    log(f"⚠ bozuk REPORT atıldı: {exc}")
                    continue
                if isinstance(injector, LogInjector):
                    injector.apply(report)
                else:
                    if verbose or stats.reports % 120 == 1:
                        log(f"▤ P{lease.index + 1} #{stats.reports}: "
                            f"{report.pretty()}")
                    try:
                        injector.apply(report)
                    except Exception as exc:
                        # Tek bir başarısız enjeksiyon oturumu öldürmemeli;
                        # sonraki rapor zaten onun yerine geçiyor.
                        stats.dropped += 1
                        if stats.dropped <= 3:
                            log(f"⚠ enjeksiyon başarısız: {exc!r}")
            elif msg_type == T_PING:
                stats.pings += 1
                client.sendall(encode_pong())
                if verbose or stats.pings <= 3 or stats.pings % 15 == 0:
                    log(f"⇄ P{lease.index + 1} PING → PONG (#{stats.pings})")
            elif msg_type == T_MOUSE:
                # 0x05 spec'te var ama uygulama göndermiyor (dokunmatik
                # yüzey kaldırıldı). Say, yoksay.
                stats.dropped += 1
            elif msg_type == T_BYE:
                log("▣ BYE — temiz kapanış")
                return
            else:
                log(f"⚠ bilinmeyen çerçeve type=0x{msg_type:02x} "
                    f"len={len(payload)}")

    except StopIteration:
        log("✗ el sıkışma tamamlanmadan karşı taraf kapattı")
    except (ConnectionError, socket.timeout, OSError) as exc:
        log(f"✗ bağlantı koptu: {exc}")
    except Exception as exc:
        log(f"✗ işleyici hatası: {exc!r}")
    finally:
        # Sıra atlanamaz: ÖNCE nötrle, SONRA slotu bırak. Tersi, o slotu
        # devralan sonraki oyuncunun basılı bir tuşla başlaması demek.
        if injector is not None:
            try:
                injector.reset()
            except Exception:
                pass
        if lease is not None:
            pool.release(lease)
            log(f"⊘ P{lease.index + 1} slotu bırakıldı → "
                f"{pool.free}/{pool.size} boş")
        log(f"◈ oturum: {stats.reports} rapor, {stats.pings} ping, "
            f"{stats.dropped} atılan, {stats.bytes_in} bayt")
        try:
            client.sendall(encode_bye())
        except OSError:
            pass
        _linger_close(client)


# Reddedilen istemcinin soketini kapatmadan önce ne kadar beklenir.
# LAN'da bir RTT fazlasıyla yeter; bu süre yalnızca kopan/reddedilen
# bağlantının kendi thread'ini tutuyor.
LINGER_TIMEOUT = 0.5


def _linger_close(client: socket.socket) -> None:
    """Önce FIN, sonra kalanı oku, en son kapat.

    **Neden düz `close()` yetmiyor.** Reddedilen istemci REJECT'i henüz
    okumadan HELLO yazıyor. Soketi doğrudan kapatırsak o HELLO kapalı bir
    sokete düşer, çekirdek RST gönderir ve **RST istemcinin okuma
    tamponunu siler** — yani gönderdiğimiz REJECT yok olur. İstemci de
    ham "connection reset" görüp sebebi tahmin etmek zorunda kalır.

    Sonuç sahada şuydu: host `R_DEVICE_UNKNOWN` ("kaydın yok, QR'ı tara")
    diyor, telefon `R_AUTH_FAILED` ("yanlış anahtar") görüyordu. İkisi
    farklı ekran açıyor; kullanıcı QR ekranına hiç ulaşamıyordu. Testlerde
    12 turun 2'sinde düşen kararsızlık tam olarak buydu.

    `SHUT_WR` yarı kapanış yapıyor: FIN gider, istemcinin yazdığı HELLO
    hâlâ kabul edilir, biz onu okuyup atarız ve tampon boş kaldığı için
    `close()` RST üretmez.
    """
    try:
        client.shutdown(socket.SHUT_WR)
    except OSError:
        pass                      # zaten kopmuş
    try:
        client.settimeout(LINGER_TIMEOUT)
        while client.recv(4096):
            pass                  # istemcinin son sözleri; okunup atılıyor
    except OSError:
        pass                      # zaman aşımı da dahil: yeterince bekledik
    try:
        client.close()
    except OSError:
        pass


# ── Defter yönetimi ──────────────────────────────────────────────────

def _manage_devices(args) -> int:
    """`--list-devices` / `--forget` — sunucu hiç açılmadan biten işler.

    Gerçek iptal yolu bu. TTL unutulmuş cihazları temizler; **bilerek
    silmek** istediğinizde kullanılacak olan `--forget`.
    """
    store = devices.DeviceStore(args.store, ttl_days=args.device_ttl)

    if args.forget:
        if args.forget == "all":
            count = store.revoke_all()
            print(f"{count} cihaz kaydı silindi. Hepsi yeniden QR taramalı.")
            return 0
        if store.revoke(args.forget):
            print(f"Silindi: {args.forget}")
            return 0
        print(f"Böyle bir kayıt yok: {args.forget}", file=sys.stderr)
        return 1

    expired = store.sweep()
    records = store.devices()
    print(f"Defter: {store.path}")
    if expired:
        print(f"(süresi dolmuş {expired} kayıt silindi)")
    if not records:
        print("Kayıtlı cihaz yok — ilk bağlantı QR ister.")
        return 0

    print(f"\n{'KİMLİK':<18} {'SON GÖRÜLME':<20} {'KAYIT':<20}")
    for record in records:
        seen = datetime.fromtimestamp(record.last_seen).strftime(
            "%Y-%m-%d %H:%M")
        made = datetime.fromtimestamp(record.created).strftime(
            "%Y-%m-%d %H:%M")
        print(f"{record.device_id:<18} {seen:<20} {made:<20}")
    print(f"\nSilmek için: --forget <KİMLİK>   (hepsi için: --forget all)")
    return 0


# ── Giriş noktası ────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="V-Pad host — çoklu oyuncu companion daemon'ı.",
        epilog="Tel protokolü: docs/companion-daemon.md",
    )
    parser.add_argument("--port", type=int, default=0,
                        help="TCP portu (varsayılan 0 = işletim sistemi "
                             "seçsin, mDNS ile yayınlanır)")
    parser.add_argument("--name", default=None,
                        help="uygulamada görünecek mDNS adı "
                             "(varsayılan 'V-Pad host — <hostname>')")
    parser.add_argument("--inject", default="auto",
                        choices=["auto", "vigem", "log"],
                        help="enjeksiyon arka ucu (varsayılan auto)")
    parser.add_argument("--host-ip", default=None,
                        help="mDNS'te yalnız bu IPv4 adresini yayınla "
                             "(varsayılan: loopback olmayan tüm adresler, "
                             "gerçek LAN arayüzleri VPN tünellerinden önce)")
    parser.add_argument("--players", type=int, default=1,
                        choices=range(1, slots.MAX_SLOTS + 1),
                        help="aynı anda kaç telefon bağlanabilir "
                             f"(XInput tavanı {slots.MAX_SLOTS}; varsayılan 1)")
    parser.add_argument("--pair", action="store_true",
                        help="QR ile eşleşme zorunlu kıl: telefon, ekranda "
                             "gösterilen QR'ı taramadan bağlanamaz")
    parser.add_argument("--device-ttl", type=int, default=devices.TTL_DAYS,
                        metavar="GÜN",
                        help=f"kayıtlı cihaz kaç gün kullanılmazsa sönsün "
                             f"(varsayılan {devices.TTL_DAYS}; 0 = hiç sönmesin)")
    parser.add_argument("--store", default=None, metavar="YOL",
                        help="cihaz defteri dosyası "
                             f"(varsayılan {devices.default_store_path()})")
    parser.add_argument("--no-remember", action="store_true",
                        help="cihazları hatırlama: her bağlantı QR ister, "
                             "diske hiçbir şey yazılmaz")
    parser.add_argument("--list-devices", action="store_true",
                        help="kayıtlı cihazları listele ve çık")
    parser.add_argument("--forget", metavar="KİMLİK",
                        help="bir cihaz kaydını sil ('all' hepsini siler), çık")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="her REPORT çerçevesini bas")
    args = parser.parse_args(argv)

    # Defter işleri sunucuyu hiç açmadan biter.
    if args.list_devices or args.forget:
        return _manage_devices(args)

    if Zeroconf is None:
        print("HATA: zeroconf kurulu değil. Çalıştır: pip install zeroconf",
              file=sys.stderr)
        return 4

    backend = resolve_backend(args.inject)
    pads = PadSet(backend, args.verbose)
    pool = slots.SlotPool(args.players)

    hostname = short_hostname()
    service_name = sanitize_instance(args.name or f"V-Pad host — {hostname}")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", args.port))
    except OSError as exc:
        print(f"HATA: {args.port} portuna bağlanılamadı: {exc}", file=sys.stderr)
        return 2
    # Kuyruk oyuncu sayısından bağımsız olarak biraz geniş: dolu havuzda
    # gelen istemci de REJECT alabilmek için accept edilmek zorunda.
    server.listen(8)
    port = server.getsockname()[1]

    addresses = lan_addresses(args.host_ip)

    # ── Erişim modeli (user kararı 2026-08-14) ───────────────────────
    #
    # QR bir erişim anahtarı DEĞİL, **tek kullanımlık kayıt bileti**.
    # Kaydolan cihaz kendi kimliğini alır ve sonraki açılışlarda QR'sız
    # girer; kayıt kullanılmazsa `--device-ttl` gün sonra söner.
    #
    # Reddedilen iki uç: (a) her açılışta yeni QR ve başka hiçbir şey →
    # telefon her seferinde tarar; (b) tek kalıcı token → QR'ı bir kez
    # gören sonsuza kadar girer, iptal yolu yok. Gerekçe:
    # `docs/wifi-transport-architecture.md` §5 başındaki not.
    #
    # Bilet portu öğrendikten SONRA üretiliyor: QR'ın içinde port var ve
    # `--port 0` ile port ancak bind sonrası belli oluyor.
    desk: TicketDesk | None = None
    store: devices.DeviceStore | None = None
    if args.pair:
        desk = TicketDesk(addresses[0], port, args.players)
        if not args.no_remember:
            store = devices.DeviceStore(args.store, ttl_days=args.device_ttl)
            expired = store.sweep()
            if expired:
                log(f"⌛ süresi dolmuş {expired} cihaz kaydı silindi")
    os_tag = {"win32": "win", "darwin": "mac",
              "linux": "linux"}.get(sys.platform, "other")
    inj_tag = VigemInjector.tag if backend == "vigem" else LogInjector.tag
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{service_name}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip) for ip in addresses],
        port=port,
        properties={
            "v": str(PROTO_VER),
            "os": os_tag,
            "inj": inj_tag,
            "caps": "",
            # Bilgilendirme amaçlı. Güvenlik buna dayanmıyor — kapı yine
            # sunucu tarafında zorunlu.
            "players": str(args.players),
            "pair": "1" if desk is not None else "0",
        },
        server=f"{hostname}.local.",
    )

    zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
    try:
        zeroconf.register_service(info)
    except Exception as exc:
        print(f"HATA: mDNS yayını başarısız: {exc}", file=sys.stderr)
        print("İpucu: başka bir süreç aynı hizmet adını yayınlıyor olabilir. "
              "--name 'başka-bir-ad' deneyin.", file=sys.stderr)
        return 3

    log("══ V-Pad host hazır ══")
    log(f"Dinleniyor    : 0.0.0.0:{port}")
    log(f"Yayınlanan IP : {', '.join(addresses)}")
    log("                (telefon bu subnet'lerden birinde olmalı)")
    log(f"mDNS adı      : {service_name!r}")
    log(f"Enjeksiyon    : "
        f"{VigemInjector.name if backend == 'vigem' else LogInjector.name}")
    log(f"TXT           : v={PROTO_VER}, os={os_tag}, inj={inj_tag}, "
        f"players={args.players}, pair={'1' if desk else '0'}")

    if desk is not None:
        if store is not None:
            known = store.devices()
            ttl = ("sönmüyor" if args.device_ttl <= 0
                   else f"{args.device_ttl} gün kullanılmazsa söner")
            log(f"Cihaz defteri : {store.path}")
            log(f"Kayıtlı cihaz : {len(known)} — {ttl}")
            for record in known:
                log(f"                • {record.device_id}")
            if known:
                log("                (bunlar QR'sız bağlanır)")
        else:
            log("Cihaz defteri : KAPALI (--no-remember) — her bağlantı QR ister")
        # `addresses[0]` bilinçli: lan_addresses zaten gerçek LAN
        # arayüzlerini VPN tünellerinden önce sıralıyor. Yanlış subnet
        # görünürse kullanıcı --host-ip ile sabitleyebilir.
        desk.announce()

    if args.players == 1:
        log("Aynı anda tek telefon. Dört oyuncu için: --players 4")
    else:
        log(f"Aynı anda {args.players} telefon. Sanal pad'ler ilk "
            f"bağlantıda yaratılır.")
    # "Yeni QR üret" isteğinin gerçek karşılığı. Ret mesajı kullanıcıyı
    # buraya yönlendiriyor; yönlendirdiği şeyin var olması gerekiyor.
    #
    # `isatty` kontrolü şart: tepsi uygulamasından ya da servis olarak
    # çalıştırıldığında stdin bir konsol değil ve `for ... in sys.stdin`
    # anında EOF alıp thread'i bitirir — zararsız ama satırı da basmayalım.
    if desk is not None and sys.stdin is not None and sys.stdin.isatty():
        def _ticket_console() -> None:
            for _ in sys.stdin:          # her Enter → yeni bilet
                desk.mint()

        threading.Thread(target=_ticket_console, daemon=True).start()
        log("Yeni QR için Enter'a basın (eskisi ölür).")

    log("Durdurmak için Ctrl+C.")
    log_raw()

    try:
        while True:
            client, addr = server.accept()
            # Bağlantı başına thread: havuz doluyken gelen telefon canlı
            # oturumların arkasında kuyruğa girmek yerine anında REJECT
            # alsın.
            threading.Thread(
                target=handle_client,
                args=(client, addr, pads, pool, desk, store, args.verbose),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        log_raw()
        log("▣ kapatılıyor")
    finally:
        pads.close()
        try:
            zeroconf.unregister_service(info)
            zeroconf.close()
        except Exception:
            pass
        try:
            server.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
