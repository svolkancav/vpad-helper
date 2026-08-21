# `vpad_daemon.py` yaması — QR eşleşmesini açmak

> ⚠ **2026-08-14: bu belge artık ana yol değil.** Kendi host'umuz var:
> `vpad_host.py`. Çoklu oyuncu ve QR eşleşmesi orada yamayla eklenen değil,
> doğuştan var olan davranış — başka bir depoya yama göndermeden slot
> mantığını değiştirebiliyoruz.
>
> Bu belge yalnızca `svolkancav/vpad-helper` içindeki daemon'ı da aynı
> hizaya getirmek istenirse geçerli. Not: §7.2'deki "dört pad önden
> yaratılıyor" yaklaşımı `vpad_host.py`'de bilinçli olarak terk edildi —
> pad'ler ilk bağlantıda yaratılıyor, yoksa tek telefon bağlıyken bile
> oyunlara dört kumanda görünüyor.

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

## 7. (Opsiyonel) Çoklu oyuncu — 4 telefon

Tasarım ve gerekçeler: `docs/wifi-transport-architecture.md` §10.

**Önkoşul: §1'deki `import vpad_pairing as pairing` satırı.** Slot çerçevesi
`pairing.encode_slot()` ile üretiliyor. Eşleşmeyi açmak zorunda değilsiniz
(`--pair` vermeyin, kapalı kalır) ama modül içe aktarılmış olmalı.

**Varsayılan 1.** `--players` verilmezse davranış bugünküyle birebir aynıdır —
`--pair` ile aynı disiplin.

### 7.1 İçe aktarma ve bayrak

```python
import vpad_slots as slots
```

```python
    parser.add_argument("--players", type=int, default=1,
                        choices=range(1, slots.MAX_SLOTS + 1),
                        help="aynı anda kaç telefon bağlanabilir "
                             "(XInput tavanı 4; varsayılan 1)")
```

### 7.2 Modül düzeyindeki kilit yerine havuz

`_active_lock`, `_active_peer` ve `handle_client`'ın ilk satırındaki
`global _active_peer` **üçü birden** silinir.

Havuz modül değişkeni **yapılmaz**. Sebep: `injector` bugün `main()` içinde
kurulup thread argümanı olarak geçiriliyor; havuzu global yapmak hem bu
desenle çelişir hem de `main()` içindeki atama modül değişkenini değil yerel
bir adı yazar (Python'un kapsam kuralı) — `handle_client` eski havuzu görürdü.
Aynı yoldan geçir:

```python
    # main(): mevcut `injector = build_injector(...)` satırının yerine
    pool = slots.SlotPool(args.players)
    injectors = [injector]                       # zaten kurulmuş olan ilki
    for _ in range(args.players - 1):            # players=1 ise hiç dönmez
        injectors.append(build_injector(args.inject, args.verbose,
                                        args.mouse_speed))
```

> İlk enjektörü yeniden kurmuyoruz: `build_injector` tanı satırları basıyor ve
> ViGEmBus yoksa uyarı veriyor; dört kez çağırmak aynı mesajı dörtlüyor.

`accept()` döngüsündeki thread argümanlarına ikisi de eklenir:

```python
                args=(client, addr, injectors, args.verbose, pool),
```

ve imza buna göre genişler:

```python
def handle_client(client: socket.socket, addr, injectors: list[Injector],
                  verbose: bool, pool: "slots.SlotPool") -> None:
```

> **macOS istisnası.** `MacKbmInjector` klavye/fare öykünmesi yapıyor; dört
> oyuncunun tek klavyeye basması anlamsız. `sys.platform == "darwin"` ise
> `args.players` 1'e sabitlenmeli ve sebebi ekrana yazılmalı.

### 7.3 `handle_client` — kilit yerine slot

`_active_lock.acquire(...)` bloğunun yerine:

```python
        lease = pool.acquire(f"{pad_name} @ {addr[0]}")
        if lease is None:
            client.sendall(encode_reject(
                R_IN_USE,
                f"all {pool.size} player slots are in use"))
            print(f"[{ts()}] ✗ refused — {pool.size} slot dolu (in_use)")
            return
        injector = injectors[lease.index]
```

`HELLO_ACK`'ten **hemen sonra**, tek yazımda:

```python
        client.sendall(encode_hello_ack(PROTO_VER, accept=True)
                       + pairing.encode_slot(lease.index))
        print(f"[{ts()}] ◂ HELLO_ACK + SLOT={lease.index}")
```

Tek yazım bilinçli: iki çağrı da doğru çalışır ama aynı segmentte gitmek
istemcinin **bloklamayan** slot tahliyesine denk düşer, yani "Oyuncu 2" rozeti
ilk karede görünür. Gecikirse de kayıp değil — istemci onu sonraki okumada
yakalar.

Mevcut `holding_lock = False` satırı (try'dan önce) şununla değişir:

```python
    lease = None
```

**Atlanamaz:** `lease` `try` içinde atanıyor ama `finally` içinde okunuyor.
Sürüm uyuşmazlığı gibi erken dönüşlerde atama hiç çalışmaz ve `finally`
`NameError` verir — üstelik asıl hatayı da maskeleyerek. Daemon'ın kendi
`holding_lock = False` satırı zaten tam bu yüzden orada.

`finally` bloğunun tamamı şu olur (mevcut `injector.reset()` ve
`if holding_lock: …` blokları birlikte kalkar):

```python
    finally:
        if lease is not None:
            try:
                injectors[lease.index].reset()   # önce nötr
            except Exception:
                pass
            pool.release(lease)                  # sonra serbest
```

İki ayrıntı atlanamaz:

- **Sıra:** önce nötr, sonra slot serbest. Tersi, o slotu devralan sonraki
  oyuncunun basılı bir tuşla başlaması demek.
- **`injector` artık `finally`'de kullanılamaz:** eskiden fonksiyon
  parametresiydi, hep tanımlıydı. Şimdi `try` içinde
  (`injector = injectors[lease.index]`) atanıyor, yani erken dönüşlerde hiç
  var olmaz. Nötrleme bu yüzden lease üzerinden yapılıyor.

### 7.4 Neden slot'u host atıyor

`Technosaurus8/Magic-Gamepad-Windows` slot'u mesajın içine koyuyor (`p1`…`p4`
öneki) ve host o indeksi kullanıyor. İki telefon da `p1` derse birbirinin
girdisini sessizce ezer. Burada istemci kendi numarasını beyan edemez;
yalnızca `T_SLOT` ile öğrenir.

ViGEm tarafının nasıl yazılacağını aynı depo çalışan kodla gösteriyor: dört
pad önden yaratılıyor, `AutoSubmitReport = false`, `Connect()` talep üzerine,
Win32 hatasında pad yeniden yaratılıyor. `vgamepad` aynı API'nin Python
sarmalı.

### 7.5 Doğrulama

`SlotPool` **hiç I/O yapmaz**, dolayısıyla dört telefon olmadan sınanır:
`python -m unittest test_vpad_slots -v` (15 test — dağıtım, yapışkanlık,
çift bırakma, 5. istemcinin reddi).

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
