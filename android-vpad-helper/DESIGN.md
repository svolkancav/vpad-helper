# Android ↔ V-Pad Helper — QR ile kamera doğrulamalı WiFi eşleşmesi

**Tasarım belgesi.** Uygulama bu belgeye göre yapıldı; sapma varsa belge değil kod
haklıdır, ama sapmanın buraya işlenmesi beklenir.

---

## 1. Problem

`gamepad_universal` (Play Store'daki uygulama) bilgisayara **yalnızca Bluetooth HID**
ile bağlanıyor. Bluetooth'un üç somut sınırı var:

- `BluetoothHidDevice` profili **sistem genelinde tekil** — başka bir HID uygulaması
  kayıtlıysa `registerApp()` başarısız oluyor ve hata kimin engellediğini söylemiyor
  (bu, `HidDescriptor.kt` KDoc'unda zaten belgelenmiş bir saha sorunu).
- Descriptor'da **Output report yok** → rumble mimari olarak kapalı.
- Eşleşme, işletim sisteminin BT ekranından geçiyor; kullanıcı akışı uygulamanın
  dışına çıkıyor.

WiFi yolu bunların üçünü de aşıyor. Ama WiFi'nin Bluetooth'ta bedava gelen bir şeyi
yok: **fiziksel yakınlık kanıtı**. BT'de eşleşmek için cihaza fiziksel erişim veya
onay gerekir; TCP'de aynı ağdaki herkes bağlanabilir.

**Bu tasarımın çözdüğü şey tam olarak bu:** QR kodu, fiziksel yakınlığı WiFi'ye geri
getiriyor. Bilgisayarın ekranını gören kişi eşleşebilir; görmeyen eşleşemez.

---

## 2. Neden mDNS değil de QR

Mevcut `vpad_daemon.py` mDNS/Bonjour ile kendini duyuruyor. Bu **keşif** çözüyor ama
**kimlik doğrulama** çözmüyor — daemon şu an bağlanan herkesi kabul ediyor, tek
koruması "aynı anda bir telefon" kilidi.

| | mDNS | QR |
|---|---|---|
| Kullanıcı IP yazar mı | hayır | hayır |
| Kurumsal ağda çalışır mı | çoğu zaman **hayır** (mDNS bloklu) | evet |
| Kimlik doğrular mı | **hayır** | evet (token) |
| Fiziksel yakınlık kanıtı | yok | **var** |

QR, mDNS'in **yerine değil yanına** geliyor. mDNS olduğu gibi kalıyor — iPhone
istemcisi bozulmuyor. Eşleşme zorunluluğu yalnızca yeni `--pair` bayrağıyla açılıyor.

---

## 3. QR payload

```
vpad://<ipv4>:<port>?t=<32 hex>&v=1
```

Örnek: `vpad://192.168.1.34:53124?t=9f3c1a8b6e40d27f5b0c9a1e4d8f2367&v=1`

| Alan | Kural |
|---|---|
| şema | tam olarak `vpad://` — başka şema reddedilir |
| host | **IPv4 literal zorunlu**, DNS adı kabul edilmez |
| port | 1–65535 |
| `t` | tam 32 onaltılık karakter (16 bayt token) |
| `v` | payload sürümü; bilinmeyen sürüm reddedilir |

### Neden DNS adı yasak

Bir alan adına izin vermek iki kapı açar: kötü niyetli bir QR telefonu internetteki
bir sunucuya yönlendirebilir, ve DNS rebinding ile LAN kontrolü baypas edilebilir.
IPv4 literal zorunluluğu ikisini de kökten kapatır. (NetPad Host'un `QrPairing`
tarafında aynı karar verilmiş; burada da aynısı yapılıyor.)

### Telefon tarafı adres kısıtı — LAN dışı reddedilir

Ayrıştırılan adres şu aralıklardan birinde **değilse** eşleşme reddedilir:

```
127.0.0.0/8      loopback
10.0.0.0/8       RFC1918
172.16.0.0/12    RFC1918
192.168.0.0/16   RFC1918
169.254.0.0/16   link-local (yönlendiricisiz ağ)
100.64.0.0/10    operatör NAT (CGNAT)
```

Yani `vpad://8.8.8.8:80?t=...` yazan bir QR taransa bile telefon bağlanmaz. Bu kural
host tarafında da simetrik uygulanır ve **iki tarafta aynı sınır değerleriyle** test
edilir (172.15 / 172.16 / 172.32 ve 100.63 / 100.64 / 100.128).

---

## 4. El sıkışma — HMAC challenge-response

### Neden düz token değil

En basit tasarım "token'ı ilk mesajda gönder" olurdu. Bunun bilinen zaafı var ve
NetPad Host'un `SECURITY.md`'sinde açıkça yazılı: *token düz metin gidiyor, aynı ağı
pasif dinleyen biri yakalayıp kullanabilir.* Aynı hatayı bilerek tekrarlamanın
anlamı yok — challenge-response'un maliyeti birkaç düzine satır.

### Akış

```
istemci                                        host
   │                                            │
   │────────────── TCP connect ────────────────>│
   │                                            │
   │<──────── CHALLENGE (0x20) ─────────────────│   16 rastgele bayt
   │                                            │   (her bağlantıda YENİ)
   │                                            │
   │───────── AUTH (0x21) ─────────────────────>│   nonce(16) ‖ mac(32)
   │                                            │
   │                                       doğrula:
   │                                       compare_digest(mac, beklenen)
   │                                            │
   │<──── HELLO_ACK (0x10) │ REJECT (0x11) ─────│
   │                                            │
   │──────── HELLO / REPORT / PING ────────────>│   (mevcut protokol)
```

### MAC hesabı

```
mac = HMAC-SHA256(
        key  = token (16 ham bayt),
        msg  = b"vpad-auth-v1" ‖ challenge(16) ‖ client_nonce(16)
      )                                              → 32 bayt, tamamı gönderilir
```

Üç tasarım kararı:

1. **Alan ayracı olarak sabit etiket** (`vpad-auth-v1`). Aynı token ileride başka bir
   amaçla kullanılırsa (örn. bir oturum anahtarı türetimi) MAC'ler birbirine
   karışmasın diye. Sürüm etiketi de burada, böylece şema değişirse eski MAC yeni
   şemada geçerli olmaz.
2. **İstemci nonce'u da karışıma giriyor.** Host'un rastgeleliği bir gün zayıflarsa
   (kötü tohumlanmış RNG) istemci kendi entropisini katmış olur.
3. **Sabit zamanlı karşılaştırma** (`hmac.compare_digest` / `MessageDigest.isEqual`).
   LAN'da zamanlama saldırısı zor, ama bedava bir sertleştirme.

### Replay koruması

Challenge her TCP bağlantısında yeniden üretiliyor ve tek kullanımlık. Yakalanan bir
`AUTH` çerçevesi başka bir bağlantıda geçersiz, çünkü oradaki challenge farklı.

### Kalan risk — dürüst not

El sıkışma sonrası **REPORT trafiği hâlâ şifresiz ve bütünlük korumasız.** Aktif bir
saldırgan kurulu TCP oturumuna paket enjekte edebilirse girdi sürebilir. Bunu kapatmak
el sıkışmadan bir oturum anahtarı türetip her çerçeveyi AEAD ile korumayı gerektirir —
bu sürümün kapsamı dışında, bilinçli olarak belgeleniyor.

Yani bu tasarımın durdurduğu saldırgan: **aynı ağda olup QR'ı görmemiş olan** ve
trafiği pasif dinleyen saldırgan. Durdurmadığı: kurulu oturuma aktif olarak paket
enjekte edebilen saldırgan.

---

## 5. Çerçeve formatı

Mevcut `vpad_daemon.py` formatı korunuyor:

```
[u16 LE toplam uzunluk (başlık dahil)][u8 tip][gövde…]      MAX_FRAME = 4096
```

Eklenen tipler ve sebep kodları:

| Sabit | Değer | Yön | Gövde |
|---|---|---|---|
| `T_CHALLENGE` | `0x20` | host → istemci | 16 bayt challenge |
| `T_AUTH` | `0x21` | istemci → host | 16 bayt nonce + 32 bayt mac |
| `R_AUTH_REQUIRED` | `0x04` | REJECT sebebi | eşleşme açık ama AUTH gelmedi |
| `R_AUTH_FAILED` | `0x05` | REJECT sebebi | MAC uyuşmadı |

**Geriye uyum:** `--pair` verilmezse host CHALLENGE göndermez ve akış bugünkünün
birebir aynısı kalır. Mevcut iPhone istemcisi etkilenmez.

---

## 6. Rapor uyumu — `gamepad_universal` ile birebir

Bu, tasarımın en şanslı kısmı: `vpad_daemon.py`'nin 8 baytlık `REPORT` gövdesi,
`HidReportSender.kt`'nin Bluetooth'a yazdığı 8 baytın **birebir aynısı**.

| Bayt | `HidReportSender` | `vpad_daemon.Report` |
|---|---|---|
| 0 | `buttons & 0xFF` | `btn_low` |
| 1 | `((buttons>>8) & 0x07) \| (hat<<4)` | `btn_high` (`hat = btn_high>>4`) |
| 2–5 | lx, ly, rx, ry — u8, merkez 128 | aynı |
| 6–7 | lt, rt — u8 0..255 | aynı |

Buton maskeleri de aynı: `A=0x01 B=0x02 X=0x04 Y=0x08 L1=0x10 R1=0x20 Select=0x40
Start=0x80`, yüksek baytta `L3=0x01 R3=0x02 Home=0x04`. Hat null durumu iki tarafta
da **8**.

**Sonuç:** WiFi yolu, uygulamanın zaten ürettiği rapor baytlarını almak ve
`hid.sendReport(...)` yerine TCP çerçevesine sarmaktan ibaret. Girdi işleme, deadzone,
eğri, hepsi olduğu gibi kalıyor. Entegrasyonun tek dokunması gereken yer, raporların
tek hunisi olan `HidReportSender` çağrı noktası.

---

## 7. Bileşenler

```
android-vpad-helper/
├── DESIGN.md                       bu belge
├── README.md                       ne var, nasıl çalıştırılır
├── host/                           ── Python tarafı (burada çalışır ve test edilir)
│   ├── vpad_pairing.py             token, payload, QR, HMAC, LAN kuralı
│   ├── test_vpad_pairing.py        birim testleri
│   ├── test_e2e_pairing.py         gerçek soket üzerinden uçtan uca
│   └── DAEMON_PATCH.md             vpad_daemon.py'ye uygulanacak değişiklikler
├── android/kotlin/                 ── gamepad_universal'a bırakılacak dosyalar
│   ├── PairingPayload.kt           vpad:// ayrıştırma + LAN doğrulama
│   ├── PairingCrypto.kt            HMAC-SHA256 challenge-response
│   ├── WifiFrameCodec.kt           çerçeve encode/decode
│   ├── WifiGamepadClient.kt        TCP istemci + el sıkışma + REPORT
│   ├── WifiConnectionState.kt      durum modeli
│   ├── CodeScannerPairing.kt       QR tarama (Play services code scanner)
│   └── INTEGRATION.md              bağlama adımları
└── jvm-verify/                     ── saf-JVM Gradle projesi: Kotlin çekirdeği
                                       gerçekten derler ve test eder
```

**Kritik ayrım:** `PairingPayload`, `PairingCrypto`, `WifiFrameCodec`,
`WifiGamepadClient` **hiçbir Android API'si kullanmaz** — yalnızca `java.*` ve Kotlin
stdlib. Bu bilinçli: böylece bu makinede gerçekten derlenip test edilebiliyorlar.
Android'e özgü olanlar yalnızca `CodeScannerPairing.kt` (kamera) ve
`WifiDiagnosticsActivity.kt` (tanı ekranı).

---

## 8. Doğrulama planı

| Katman | Nasıl doğrulanır | Bu makinede mümkün mü |
|---|---|---|
| Host eşleşme mantığı | Python birim testleri | ✅ |
| Host uçtan uca | gerçek soket + referans istemci | ✅ |
| Kotlin çekirdek | saf-JVM Gradle projesinde derleme + test | ✅ |
| **Protokol sözleşmesi** | **altın vektörler** — Python'un ürettiği MAC ve çerçeve baytları Kotlin testinde sabit olarak doğrulanır | ✅ |
| QR tarama katmanı | AGP + Android SDK ile derleme | ✅ derlendi |
| Gerçek kamera ile tarama | Play services tarayıcısı, fiziksel cihaz | ❌ denenmedi |
| Gerçek telefon → gerçek host | fiziksel cihaz | ❌ |

Altın vektör yaklaşımı, iki ayrı dilde yazılmış iki tarafın birbirini gerçekten
anladığını kanıtlamanın en ucuz yolu: Python bir MAC üretir, o bayt dizisi Kotlin
testine sabit olarak gömülür, Kotlin aynı girdiden aynı MAC'i üretmek zorundadır.

---

## 9. Kapsam dışı (bilinçli)

- **Şifreli girdi kanalı.** Bkz. §4 "Kalan risk".
- **Rumble.** Protokolde output yolu yok; BT tarafında da yok (`HidDescriptor` KDoc).
- **Çoklu telefon.** Daemon tasarım gereği tek istemci; bu değişmiyor.
- **Linux host.** `vpad_daemon.py` Linux'ta `--inject log` ile çalışır, gerçek
  enjeksiyon yolu yok. Eşleşme katmanı platformdan bağımsız olduğu için Linux
  enjeksiyonu eklendiğinde bu tasarım aynen geçerli.
- **`gamepad_universal` deposunda değişiklik.** Bu paket hiçbir dosyaya dokunmaz;
  Android tarafı bırakılacak dosya + kılavuz olarak teslim edilir.
