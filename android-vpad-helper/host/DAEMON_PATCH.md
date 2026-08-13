# `vpad_daemon.py` yaması — QR eşleşmesini açmak

Bu belge, mevcut `vpad_daemon.py`'ye uygulanacak **tam değişiklik listesidir**.
Beş küçük düzenleme; hiçbiri mevcut akışı bozmuyor.

Buradaki akışın doğruluğu `test_e2e_pairing.py` içindeki `TestDaemon` ile
kanıtlanmış durumda — o sınıf tam olarak aşağıdaki adımları uygular ve gerçek
soket üzerinden 9 test onu doğruluyor.

**Tasarım kararı:** eşleşme varsayılan olarak **KAPALI**. `--pair` verilmeden
daemon bugünkü davranışını birebir korur, mevcut iPhone istemcisi etkilenmez.

---

## 1) İçe aktarma

Dosyanın başındaki `zeroconf` bloğunun hemen altına:

```python
import vpad_pairing as pairing
```

`vpad_pairing.py` daemon ile aynı klasöre konur. Üçüncü parti bağımlılığı
yoktur, yani `requirements.txt` değişmez (QR çizimi için `qrcode` opsiyonel —
bkz. §5).

---

## 2) CLI bayrağı

`main()` içindeki argparse bloğuna:

```python
    parser.add_argument("--pair", action="store_true",
                        help="QR ile eşleşme zorunlu kıl: telefon, ekranda "
                             "gösterilen QR'ı taramadan bağlanamaz")
```

---

## 3) `handle_client` — eşleşme kapısı

İmzaya `token` eklenir:

```python
def handle_client(client: socket.socket, addr, injector: Injector,
                  verbose: bool, token: bytes | None = None) -> None:
```

`reader = frame_reader(client)` satırının **hemen altına**, ilk `next(reader)`
çağrısından **önce** şu blok girer:

```python
        # ── QR eşleşme kapısı ──────────────────────────────────────
        # Açıkken: bağlantı kabul edilir edilmez CHALLENGE gider ve
        # istemcinin İLK çerçevesi geçerli bir AUTH olmak zorundadır.
        # Kapalıyken bu blok hiçbir şey yapmaz — eski akış aynen sürer.
        gate = pairing.PairingGate(token)
        opening = gate.opening_frame()
        if opening is not None:
            client.sendall(opening)
            msg_type, payload = next(reader)
            stats.bytes_in += 3 + len(payload)
            ok, reject = gate.accept(msg_type, payload)
            if not ok:
                client.sendall(reject)
                print(f"[{ts()}] ✗ eşleşme reddedildi (reason=0x"
                      f"{reject[3]:02x}) — {addr[0]}")
                return
            print(f"[{ts()}] 🔒 QR eşleşmesi doğrulandı — {addr[0]}")
```

Dikkat: bu blok **`_active_lock` alınmadan önce** çalışır. Böylece token'ı
olmayan bir istemci tek-telefon kilidini hiç meşgul edemez — aksi hâlde
LAN'daki biri sadece bağlanıp kilidi tutarak meşru telefonu dışarıda
bırakabilirdi.

---

## 4) `main()` — token üretimi ve thread'e aktarma

Soket `listen()` edildikten ve `port` öğrenildikten **sonra** (QR'ın portu
içermesi gerekiyor, `--port 0` ile port ancak bind sonrası belli oluyor):

```python
    token = pairing.generate_token() if args.pair else None
```

`accept()` döngüsündeki thread argümanlarına `token` eklenir:

```python
            threading.Thread(
                target=handle_client,
                args=(client, addr, injector, args.verbose, token),
                daemon=True,
            ).start()
```

---

## 5) Açılış çıktısı — QR

Mevcut açılış banner'ının sonuna (`One phone at a time…` satırından önce):

```python
    if token is not None:
        payload = pairing.build_payload(addresses[0], port, token)
        print()
        print(f"[{ts()}] Telefonla aşağıdaki QR'ı tarayın:")
        print()
        print(pairing.render_qr_terminal(payload))
        print(f"[{ts()}] Eşleştirme : {payload}")
        print(f"[{ts()}] Güvenli mod: yalnızca bu QR'ı tarayan cihaz bağlanabilir")
```

`addresses[0]` seçimi bilinçli: `lan_addresses()` zaten gerçek LAN
arayüzlerini VPN tünellerinden önce sıralıyor, yani ilk adres telefonun
ulaşabileceği en olası adres. Birden fazla arayüz varsa ve QR yanlış subnet'i
gösterirse, kullanıcı `--host-ip` ile sabitleyebilir.

QR çizimi için `qrcode` paketi gerekir. Kurulu değilse `render_qr_terminal`
hata **atmaz**, elle girilebilecek metni döndürür. Kalıcı çözüm için
`requirements.txt`'e eklenebilir:

```
# QR eşleşmesi (--pair) için. Yoksa daemon yine açılır, QR yerine
# adres metni basılır.
qrcode>=7.4
```

Pillow'a **ihtiyaç yok**: `render_qr_terminal` yalnızca `get_matrix()` çağırıp
ANSI metin basıyor, hiç görüntü üretmiyor. (Kütüphanenin kendi README'si:
standart kurulum PNG için `pypng` kullanır, Pillow `qrcode[pil]` ekstrasıdır.)

---

## 6) (Opsiyonel) mDNS TXT kaydı

İstemcinin, bağlanmadan önce eşleşme gerekip gerekmediğini bilmesi için
`ServiceInfo` içindeki `properties` sözlüğüne:

```python
            "pair": "1" if token is not None else "0",
```

Bu tamamen bilgilendirme amaçlı — güvenlik buna dayanmıyor. Kapı yine
sunucu tarafında zorunlu.

---

## Doğrulama

Yama uygulandıktan sonra, telefon olmadan:

```bash
# 1. Terminal: eşleşme açık daemon
python vpad_daemon.py --pair --inject log

# 2. Terminal: basılan payload'ı yapıştır
python vpad_reference_client.py "vpad://192.168.1.34:53124?t=...&v=1" --press A --hold 2
```

Beklenen: birinci terminalde `🔒 QR eşleşmesi doğrulandı` ve ardından A
tuşuna basılı REPORT satırları.

Yanlış token'ın reddedildiğini görmek için payload'daki `t=` değerinin son
karakterini değiştirip tekrar deneyin — `REDDEDILDI: eşleşme doğrulaması
başarısız` görmelisiniz.
