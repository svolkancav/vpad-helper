# android-vpad-helper

Android telefonu, **aynı WiFi ağındaki** bilgisayara **QR kodu ile kamera
doğrulamalı** olarak bağlayan eşleşme katmanı.

İki taraf var ve ikisi de burada:

- **Host (Python)** — `vpad_daemon.py`'nin yanına gelen eşleşme modülü.
  Çalışır durumda, bu makinede test edildi.
- **Android (Kotlin)** — `gamepad_universal` uygulamasına bırakılacak
  dosyalar. Güvenlik-kritik çekirdeği burada gerçekten derlendi ve test edildi.

`gamepad_universal` deposunda **hiçbir dosya değiştirilmedi**; Android tarafı
bırakılacak dosya + bağlama kılavuzu olarak teslim ediliyor.

---

## Neden

Uygulama şu an bilgisayara yalnızca Bluetooth HID ile bağlanıyor. WiFi yolu,
Bluetooth'un üç somut sınırını aşıyor: sistem genelinde tekil olan
`BluetoothHidDevice` profil çakışması, rumble'ın mimari olarak kapalı olması,
ve eşleşmenin uygulama dışına çıkması.

Ama WiFi'nin, Bluetooth'ta bedava gelen bir şeyi yok: **fiziksel yakınlık
kanıtı.** TCP'de aynı ağdaki herkes bağlanabilir. QR kodu tam olarak bunu geri
getiriyor — bilgisayarın ekranını gören kişi eşleşebilir, görmeyen eşleşemez.

Tasarımın tamamı, gerekçeleri ve tehdit modeli: **[DESIGN.md](DESIGN.md)**

---

## Ne yapılmış

| | |
|---|---|
| Eşleşme | QR payload `vpad://<ip>:<port>?t=<32 hex>&v=1` |
| Kimlik doğrulama | HMAC-SHA256 challenge-response — **replay'e kapalı** |
| Adres kısıtı | yalnızca LAN aralıkları; genel IP ve alan adı **reddedilir** |
| Rapor uyumu | 8 baytlık REPORT, `HidReportSender`'ın ürettiğiyle **birebir** |
| Geriye uyum | `--pair` verilmezse daemon bugünkü davranışını korur |
| Boşta kalma | 2 sn'de bir kalp atışı — host'un 10 sn'lik zaman aşımına karşı |
| Test | **30 Python + 9 uçtan uca + 49 Kotlin = 88**, hepsi geçiyor |

Kritik ayrıntı: `vpad_daemon.py`'nin 8 baytlık REPORT gövdesi zaten
`HidReportSender.kt`'nin Bluetooth'a yazdığı 8 baytın aynısıydı — buton
maskeleri, hat nibble'ı, merkez-128 eksenler, hepsi. Yani WiFi yolu, girdi
işleme koduna hiç dokunmadan aynı baytları farklı bir taşıyıcıya vermekten
ibaret.

---

## Dizin yapısı

```
android-vpad-helper/
├── DESIGN.md                  tasarım, protokol, tehdit modeli
├── host/                      ── Python tarafı (çalışır ve test edilir)
│   ├── vpad_pairing.py        token · payload · QR · HMAC · LAN kuralı
│   ├── vpad_reference_client.py  çalışan istemci + yürütülebilir şartname
│   ├── test_vpad_pairing.py   28 birim testi
│   ├── test_e2e_pairing.py    9 uçtan uca test (gerçek soket)
│   └── DAEMON_PATCH.md        vpad_daemon.py'ye uygulanacak değişiklikler
├── android/
│   ├── core/                  ── saf JVM: jvm-verify'da DERLENİR ve TEST EDİLİR
│   │   └── …/wifi/PairingPayload · PairingCrypto · WifiFrameCodec
│   │             · WifiGamepadClient · WifiConnectionState
│   ├── ui/                    ── Android'e özgü: AGP ile derlendi, testi yok
│   │   └── …/wifi/CodeScannerPairing · WifiDiagnosticsActivity
│   └── INTEGRATION.md         gamepad_universal'a bağlama kılavuzu
└── jvm-verify/                Kotlin çekirdeğini derleyip test eden Gradle projesi
```

`core/` dosyaları **hiçbir Android API'si kullanmaz** — bu bilinçli. Eşleşmenin
güvenlik-kritik parçası, cihaz veya emülatör olmadan JVM'de test edilebilsin
diye. Android'e özgü olan tek şey kamera boru hattı.

`jvm-verify` kaynak kodu **kopyalamaz**, `android/core/`'u doğrudan gösterir:
teslim edilen dosyalarla test edilen dosyalar aynı, sürüklenme imkânsız.

---

## Çalıştırma

### Host testleri

```bash
cd host
python -m unittest discover -s . -v      # 30 + 9 test
```

### Kotlin çekirdek testleri

```bash
cd jvm-verify
gradle test                               # 49 test
```

Gradle 8.5+ yeterli; wrapper bilinçli olarak eklenmedi (ikili dosya taşımamak
için). Kurulu değilse `gradle wrapper` ile üretebilir ya da IDE'nizin kendi
Gradle'ını kullanabilirsiniz.

### Telefon olmadan uçtan uca deneme

```bash
# 1. terminal — eşleşme açık daemon (DAEMON_PATCH.md uygulandıktan sonra)
python vpad_daemon.py --pair --inject log

# 2. terminal — basılan payload'ı yapıştır
python host/vpad_reference_client.py "vpad://192.168.1.34:53124?t=...&v=1" \
    --press A --hold 2
```

Yanlış token'ın reddedildiğini görmek için payload'daki `t=` değerinin son
karakterini değiştirin.

---

## Sırada ne var

`android/INTEGRATION.md` §8'deki doğrulama sırası. Özetle: host'u yamala →
telefonsuz sına → Kotlin testlerini koştur → tanı ekranıyla gerçek telefonda
dene → ancak sonra rapor hunisini bağla.

---

## Doğrulanmamış olanlar — dürüst not

| Katman | Durum |
|---|---|
| Host eşleşme mantığı | ✅ 28 birim testi |
| Host uçtan uca (gerçek soket) | ✅ 9 test |
| Kotlin çekirdek | ✅ 45 test, gerçek soket dahil |
| Python ↔ Kotlin protokol sözleşmesi | ✅ altın vektör (aynı HMAC baytları) |
| Android tarafı (`ui/`) | ✅ **derlendi** — AGP 8.13.2, SDK 36, minSdk 31 |
| Gerçek telefon → gerçek host | ❌ **denenmedi** — fiziksel cihaz gerekir |

Yani protokolün doğruluğu kanıtlanmış, Android kodu derleniyor; kanıtlanmamış
olan yalnızca gerçek cihaz davranışı — kamera taraması, izin akışı, gerçek ağ
üzerinde gecikme. `WifiDiagnosticsActivity` tam olarak o boşluğu kapatmak için
var.

Not: `ui/` altındaki iki dosyanın **birim testi yok**. `core/` bilinçli olarak
Android'den arındırıldığı için test edilebiliyor; `ui/` ise kamera ve Activity
yaşam döngüsüne bağlı, orada değer üretecek test ancak cihaz üstünde koşar.
