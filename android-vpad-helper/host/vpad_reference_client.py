#!/usr/bin/env python3
"""
V-Pad referans istemcisi — QR eşleşmesi + rapor gönderimi.

İki işi var:

1. **Çalışan araç.** Telefon olmadan daemon'ı sınamak için. QR payload'ını
   verirsiniz, eşleşir, istediğiniz tuşa basar.
2. **Yürütülebilir şartname.** `WifiGamepadClient.kt` bunun birebir
   çevirisidir. Kotlin tarafında bir davranış tartışmalı hale gelirse
   doğru cevap burada.

Kullanım:

    # Daemon'ın bastığı payload'ı yapıştırın
    python vpad_reference_client.py "vpad://192.168.1.34:53124?t=...&v=1"

    # Bağlan, A'ya bas, 1.5 sn tut
    python vpad_reference_client.py "vpad://..." --press A --hold 1.5

    # Sol çubuğu sağa it
    python vpad_reference_client.py "vpad://..." --lx 255 --hold 2

Üçüncü parti bağımlılık yok.
"""
from __future__ import annotations

import argparse
import select
import socket
import sys
import time

import vpad_pairing as pairing

PROTO_VER = 1

T_REPORT = 0x02
T_PING = 0x03
T_BYE = 0x04
T_PONG = 0x12

# HidDescriptor.kt ile birebir aynı maskeler.
BUTTONS = {
    "A": 0x0001, "B": 0x0002, "X": 0x0004, "Y": 0x0008,
    "L1": 0x0010, "R1": 0x0020, "SELECT": 0x0040, "START": 0x0080,
    "L3": 0x0100, "R3": 0x0200, "HOME": 0x0400,
}
HAT_CENTER = 8

REJECT_REASONS = {
    0x01: "sürüm uyuşmazlığı",
    0x02: "daemon başka bir telefonla meşgul",
    0x03: "desteklenmeyen skin",
    pairing.R_AUTH_REQUIRED: "eşleşme gerekli — QR taranmadı",
    pairing.R_AUTH_FAILED: "eşleşme doğrulaması başarısız — yanlış token",
    0xFF: "iç hata",
}


class ProtocolError(RuntimeError):
    pass


class Rejected(RuntimeError):
    def __init__(self, reason: int, message: str):
        self.reason = reason
        self.message = message
        label = REJECT_REASONS.get(reason, f"bilinmeyen sebep 0x{reason:02x}")
        super().__init__(f"{label}{': ' + message if message else ''}")


class FrameReader:
    """`[u16 LE uzunluk][u8 tip][gövde]` çözücüsü.

    **Neden generator değil sınıf:** generator'da `recv` zaman aşımına
    uğrarsa istisna generator'ı kapatır ve tampon kaybolur; sonraki
    `next()` çağrısı `StopIteration` verir. Eşleşme kapısını beklerken
    bilinçli olarak kısa zaman aşımı kullandığımız için bu ölümcül olurdu.
    Sınıf hâlinde tampon istisnadan sağ çıkar ve okuyucu yeniden
    kullanılabilir.

    Kotlin karşılığı: `WifiFrameCodec.Decoder`.
    """

    __slots__ = ("_sock", "_buf")

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = bytearray()

    def take_buffered(self) -> tuple[int, bytes] | None:
        """Tamponda tamamlanmış bir çerçeve varsa döner — **soketi okumaz**.

        Bloklamayan tahliyenin ilk adımı. Tek bir `recv` birden fazla çerçeve
        getirebilir (host `HELLO_ACK + SLOT`'u tek `sendall` ile yolluyor), o
        yüzden "gelecek bir şey var mı" diye sokete bakmadan ÖNCE burada
        birikeni almak gerekir. Kotlin karşılığı: `WifiFrameCodec.Decoder.poll`.
        """
        return self._take()

    def next_frame(self) -> tuple[int, bytes]:
        while True:
            frame = self._take()
            if frame is not None:
                return frame
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ProtocolError("karşı taraf soketi kapattı")
            self._buf.extend(chunk)

    def _take(self) -> tuple[int, bytes] | None:
        if len(self._buf) < 3:
            return None
        length = self._buf[0] | (self._buf[1] << 8)
        if length < 3:
            raise ProtocolError(f"eksik boyutlu çerçeve: {length}")
        if length > pairing.MAX_FRAME:
            raise ProtocolError(f"aşırı büyük çerçeve: {length}")
        if len(self._buf) < length:
            return None
        msg_type = self._buf[2]
        payload = bytes(self._buf[3:length])
        del self._buf[:length]
        return msg_type, payload


def encode_hello(name: str, skin: str = "") -> bytes:
    """HELLO gövdesi — `vpad_daemon.decode_hello`'nun beklediği düzen."""
    name_bytes = name.encode("utf-8")[:255]
    skin_bytes = skin.encode("utf-8")[:255]
    body = (
        bytes([PROTO_VER, len(name_bytes)])
        + name_bytes
        + bytes([len(skin_bytes)])
        + skin_bytes
    )
    return pairing.encode_frame(pairing.T_HELLO, body)


def encode_report(buttons: int = 0, hat: int = HAT_CENTER,
                  lx: int = 128, ly: int = 128, rx: int = 128, ry: int = 128,
                  lt: int = 0, rt: int = 0) -> bytes:
    """8 baytlık REPORT — `HidReportSender.sendGamepadReportTo` ile BİREBİR.

    Bu fonksiyonun çıktısı, uygulamanın Bluetooth'a yazdığı 8 baytın
    aynısıdır. WiFi yolunun tüm hilesi bu: aynı baytlar, farklı taşıyıcı.
    """
    hat_value = hat if 0 <= hat <= 7 else HAT_CENTER
    body = bytes([
        buttons & 0xFF,
        ((buttons >> 8) & 0x07) | ((hat_value & 0x0F) << 4),
        max(0, min(255, lx)),
        max(0, min(255, ly)),
        max(0, min(255, rx)),
        max(0, min(255, ry)),
        max(0, min(255, lt)),
        max(0, min(255, rt)),
    ])
    return pairing.encode_frame(T_REPORT, body)


class VPadClient:
    """Eşleşen ve rapor gönderen istemci."""

    def __init__(self, info: pairing.PairingInfo, name: str = "Referans Istemci"):
        self.info = info
        self.name = name
        self._sock: socket.socket | None = None
        self._reader: FrameReader | None = None
        self.paired = False
        # Host'un atadığı oyuncu indeksi (0..3); None = tek oyunculu host.
        self.slot: int | None = None

    def connect(self, timeout: float = 10.0, pair_wait: float = 2.0) -> None:
        """Bağlan, gerekiyorsa eşleş, HELLO_ACK'e kadar git.

        **Sıralama neden böyle.** Eşleşme açıkken host, bağlantıyı kabul
        eder etmez CHALLENGE gönderir ve istemcinin ilk çerçevesinin AUTH
        olmasını bekler. Eşleşme kapalıyken host hiçbir şey göndermez,
        doğrudan HELLO bekler.

        İstemci hangi durumda olduğunu baştan bilemez (QR'da token var ama
        host `--pair` olmadan yeniden başlatılmış olabilir). Bu yüzden ilk
        çerçeveyi KISA bir zaman aşımıyla bekliyoruz:

          * CHALLENGE geldi  → AUTH gönder, eşleşmiş moda geç
          * zaman aşımı      → host sessiz, eşleşmesiz moda düş
          * REJECT geldi     → sebebi ile birlikte yükselt

        İki tarafın da birbirini beklediği kilitlenme bu şekilde imkânsız.
        """
        sock = socket.create_connection((self.info.host, self.info.port), timeout)
        # Rapor kadansı Nagle tarafından birleştirilmemeli.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._reader = FrameReader(sock)

        # --- 1) Eşleşme kapısı ---
        sock.settimeout(pair_wait)
        first: tuple[int, bytes] | None
        try:
            first = self._reader.next_frame()
        except (TimeoutError, socket.timeout):
            first = None  # host sessiz → eşleşme kapalı
        finally:
            sock.settimeout(timeout)

        if first is not None:
            msg_type, payload = first
            if msg_type == pairing.T_CHALLENGE:
                if len(payload) != pairing.CHALLENGE_LEN:
                    raise ProtocolError(
                        f"challenge {pairing.CHALLENGE_LEN} bayt olmalı, "
                        f"{len(payload)} geldi")
                sock.sendall(pairing.encode_auth(self.info.token, payload))
                self.paired = True
                # Token yanlışsa host REJECT gönderip soketi KAPATIR. Bu
                # noktada HELLO göndermek, kapanmakta olan sokete yazmak
                # demektir: karşı taraf RST ile cevap verir ve RST, bizim
                # tamponumuzda bekleyen REJECT'i de SİLER. Sonuç, kullanıcının
                # "yanlış QR" yerine ham "bağlantı sıfırlandı" görmesi.
                # Bu yüzden HELLO'dan ÖNCE, bloklamadan bakıyoruz.
                early = self._drain()
                if early is not None:
                    self._raise_reject(early)
            elif msg_type == pairing.T_REJECT:
                self._raise_reject(payload)
            else:
                raise ProtocolError(
                    f"ilk çerçeve CHALLENGE olmalıydı, 0x{msg_type:02x} geldi")

        # --- 2) HELLO / HELLO_ACK ---
        try:
            sock.sendall(encode_hello(self.name))
            msg_type, payload = self._read_until(pairing.T_HELLO_ACK)
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError) as exc:
            # Yukarıdaki yarışın kalan payı: REJECT henüz gelmemişken HELLO
            # yazdıysak RST'yi burada yeriz ve REJECT kaybolur. AUTH
            # gönderdikten sonra host'un bağlantıyı kapatmasının pratikteki
            # tek sebebi eşleşmenin reddedilmesidir; kullanıcıya ham soket
            # hatası yerine bunu söylemek hem daha doğru hem eyleme
            # dönüştürülebilir.
            if self.paired:
                raise Rejected(
                    pairing.R_AUTH_FAILED,
                    "host AUTH sonrasi baglantiyi kapatti") from exc
            raise
        if msg_type == pairing.T_REJECT:
            self._raise_reject(payload)

        # --- 3) Slot (çoklu oyuncu) ---
        # Çoklu oyuncu modundaki host, HELLO_ACK ile SLOT'u aynı `sendall`da
        # gönderiyor; yani ACK okunduğunda SLOT çoktan tamponda. `_read_until`
        # ACK'te durduğu için onu ALMAZ — bloklamadan burada topluyoruz.
        # Beklemiyoruz: tek oyunculu host hiç göndermez, beklemek herkese
        # bedava gecikme yazdırırdı. Kotlin karşılığı: `connect()` §3.
        self._drain()

    def _poll_frame(self) -> tuple[int, bytes] | None:
        """**Bloklamadan**: hâlihazırda gelmiş bir çerçeve varsa döner.

        `select` sıfır zaman aşımıyla yalnızca "okunacak bir şey var mı" diye
        bakar; yoksa anında None. Kotlin karşılığı:
        `WifiGamepadClient.drainBuffered`.
        """
        reader = self._reader
        if reader is None:
            return None
        # ÖNCE tampon: tek bir `recv` birden fazla çerçeve getirmiş olabilir
        # ve o çerçeveler için sokette okunacak bayt KALMAZ — `select` boş
        # döner, çerçeve de tamponda unutulurdu. (Bu sırayı ters yazmıştım;
        # host'a karşı koşturunca SLOT hiç yakalanmadı.)
        buffered = reader.take_buffered()
        if buffered is not None:
            return buffered
        sock = self._require_socket()
        if not select.select([sock], [], [], 0)[0]:
            return None
        try:
            return reader.next_frame()
        except (OSError, ProtocolError):
            return None

    def _drain(self) -> bytes | None:
        """Bloklamadan gelmiş çerçeveleri tüketir; ilk REJECT'in gövdesini döner.

        `T_SLOT` yakalanır, tanınmayan tipler atılır. Kotlin karşılığı:
        `WifiGamepadClient.drainBuffered`.
        """
        while True:
            frame = self._poll_frame()
            if frame is None:
                return None
            msg_type, payload = frame
            if msg_type == pairing.T_REJECT:
                return payload
            if msg_type == pairing.T_SLOT and payload:
                self.slot = payload[0]

    def _read_until(self, wanted: int) -> tuple[int, bytes]:
        """`wanted` (ya da REJECT) gelene kadar okur.

        Aradaki çerçeveler **yan kanal**: `T_SLOT` yakalanır, tanınmayan her
        tip sessizce atılır. Spec §9 istemcilerden tam olarak bunu istiyor —
        aksi hâlde host bir gün SLOT ya da RUMBLE gönderdiğinde bağlantı
        kırılırdı.

        Kotlin karşılığı: `WifiGamepadClient.readUntil`.
        """
        while True:
            msg_type, payload = self._reader.next_frame()  # type: ignore[union-attr]
            if msg_type in (wanted, pairing.T_REJECT):
                return msg_type, payload
            if msg_type == pairing.T_SLOT and payload:
                self.slot = payload[0]

    def _raise_reject(self, payload: bytes) -> None:
        reason = payload[0] if payload else 0xFF
        message = payload[1:].decode("utf-8", "replace")
        raise Rejected(reason, message)

    def send_report(self, **kwargs) -> None:
        self._require_socket().sendall(encode_report(**kwargs))

    def send_neutral(self) -> None:
        """Her şey bırakılmış durum. Kopmadan önce gönderilir ki host'ta
        takılı tuş kalmasın."""
        self.send_report()

    def ping(self) -> None:
        sock = self._require_socket()
        sock.sendall(pairing.encode_frame(T_PING))
        msg_type, payload = self._read_until(T_PONG)
        if msg_type == pairing.T_REJECT:
            self._raise_reject(payload)

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self.send_neutral()
            self._sock.sendall(pairing.encode_frame(T_BYE))
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise ProtocolError("bağlantı yok")
        return self._sock

    def __enter__(self) -> "VPadClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="V-Pad referans istemcisi — QR payload'i ile eslesir.")
    parser.add_argument("payload", help="daemon'in bastigi vpad://... metni")
    parser.add_argument("--name", default="Referans Istemci")
    parser.add_argument("--press", default=None,
                        help=f"basilacak tus: {', '.join(BUTTONS)}")
    parser.add_argument("--hat", type=int, default=HAT_CENTER,
                        help="D-pad yonu 0..7 (8 = birakildi)")
    parser.add_argument("--lx", type=int, default=128)
    parser.add_argument("--ly", type=int, default=128)
    parser.add_argument("--rx", type=int, default=128)
    parser.add_argument("--ry", type=int, default=128)
    parser.add_argument("--lt", type=int, default=0)
    parser.add_argument("--rt", type=int, default=0)
    parser.add_argument("--hold", type=float, default=1.0,
                        help="kac saniye boyunca rapor gonderilsin")
    parser.add_argument("--rate", type=float, default=60.0,
                        help="saniyedeki rapor sayisi")
    args = parser.parse_args(argv)

    try:
        info = pairing.parse_payload(args.payload)
    except pairing.PairingError as exc:
        print(f"HATA: payload reddedildi - {exc}", file=sys.stderr)
        return 2

    buttons = 0
    if args.press:
        key = args.press.upper()
        if key not in BUTTONS:
            print(f"HATA: bilinmeyen tus {args.press!r}", file=sys.stderr)
            return 2
        buttons = BUTTONS[key]

    print(f"-> {info.host}:{info.port} baglaniliyor "
          f"(token {info.token_hex[:8]}...)")
    client = VPadClient(info, args.name)
    try:
        client.connect()
    except Rejected as exc:
        print(f"REDDEDILDI: {exc}", file=sys.stderr)
        return 3
    except (OSError, ProtocolError) as exc:
        print(f"HATA: baglanilamadi - {exc}", file=sys.stderr)
        return 4

    slot_note = "" if client.slot is None else f", oyuncu {client.slot + 1}"
    print(f"[ok] eslesti "
          f"({'QR token' if client.paired else 'eslesmesiz mod'}{slot_note})")

    period = 1.0 / max(1.0, args.rate)
    deadline = time.monotonic() + args.hold
    sent = 0
    try:
        while time.monotonic() < deadline:
            client.send_report(
                buttons=buttons, hat=args.hat,
                lx=args.lx, ly=args.ly, rx=args.rx, ry=args.ry,
                lt=args.lt, rt=args.rt)
            sent += 1
            time.sleep(period)
    except (OSError, ProtocolError) as exc:
        print(f"HATA: gonderim kesildi - {exc}", file=sys.stderr)
        return 5
    finally:
        client.close()

    print(f"[ok] {sent} rapor gonderildi, baglanti kapatildi")
    return 0


if __name__ == "__main__":
    sys.exit(main())
