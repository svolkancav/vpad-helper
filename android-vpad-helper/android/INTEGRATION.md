# `gamepad_universal`'a bağlama kılavuzu

Bu paket **hiçbir mevcut dosyaya dokunmaz.** Aşağıdakiler, WiFi + QR
eşleşmesini uygulamaya eklemek için yapılacakların tam listesidir.

Ön koşul: `core/` altındaki dört dosya **JVM'de derlenmiş ve 45 testle
doğrulanmış** durumda (`jvm-verify/`). `ui/` altındakiler Android'e özgü
olduğu için ancak uygulama içinde derlenir.

---

## 1. Dosyalar nereye gider

Kotlin paket adı `com.dfnmondo.gamepad.app.wifi` — mevcut ağaçla uyumlu,
yani dosyalar doğrudan yerine düşer:

```
android/app/src/main/kotlin/com/dfnmondo/gamepad/app/wifi/
├── PairingPayload.kt        ← core/  (test edildi)
├── PairingCrypto.kt         ← core/  (test edildi)
├── WifiFrameCodec.kt        ← core/  (test edildi)
├── WifiGamepadClient.kt     ← core/  (test edildi)
├── WifiConnectionState.kt   ← core/  (test edildi)
├── CodeScannerPairing.kt    ← ui/    (QR tarama — Android'e özgü)
└── WifiDiagnosticsActivity.kt ← ui/  (tanı ekranı, yalnız debug)
```

`core/` dosyalarını **değiştirmeden** kopyalayın. Değiştirmeniz gerekirse
`jvm-verify/` projesini de yanınıza alın ve testleri koşturun — o dosyaların
tek güvencesi bu.

---

## 2. QR tarayıcı — karar ve gerekçesi

**Karar: `play-services-code-scanner`.** Diğer seçenekler için yazılmış kod
(CameraX + ML Kit, ZXing) bilinçli olarak depodan çıkarıldı; aşağıdaki
karşılaştırma kararın gerekçesi olarak duruyor.

Bu proje APK boyutuna duyarlı (Unity Ads 4,08 MB yüzünden çıkarılmıştı), o
yüzden aşağıdaki rakamlar **tahmin değil, ölçüm**: Maven'daki artefaktların
`Content-Length`'i, paket içi ML Kit için de AAR açılıp ABI başına ayrıştırıldı
(2026-08-10).

| Seçenek | arm64 cihaza inen | CameraX | CAMERA izni | Play Services |
|---|---|---|---|---|
| **`play-services-code-scanner`** | **315 KB** | gerekmez | **gerekmez** | gerekir |
| **ZXing** (`core` + `android-embedded`) | **742 KB** | gerekmez | gerekir | **gerekmez** |
| `play-services-mlkit-barcode-scanning` | ~2,0 MB | gerekir | gerekir | gerekir |
| `mlkit:barcode-scanning` (paket içi model) | **~7,6 MB** | gerekir | gerekir | gerekmez |

Paket içi ML Kit'in dökümü (AAR 9,7 MB, ABI'ler bölününce):

```
arm64-v8a yerel kod   4832 KB   ← yalnız bir ABI cihaza iner
assets (tflite model)  872 KB   ← her cihaza iner
classes.jar            388 KB
                     ─────────
                      ~6,1 MB  + CameraX 1,5 MB = ~7,6 MB
```

**Bu, projeden çıkarılan Unity Ads'ten (4,08 MB) daha büyük.** Aynı gerekçeyle
elenmesi tutarlı olur.

### Neden code-scanner

315 KB ile en küçüğü ve **kamera izni istemeyen tek seçenek**. Sebep, tarama
işinin sizin sürecinizde olmaması: `startScan()` çağrısı Binder üzerinden
Google Play services'e gidiyor, kamerayı **o** kendi izniyle açıyor, kareleri
kendi belleğinde çözüyor ve size yalnızca sonuç metnini döndürüyor. Android
izinleri UID başına verildiği için uygulamanın izne ihtiyacı kalmıyor —
`ACTION_IMAGE_CAPTURE` ile fotoğraf çektirmenin aynısı.

Ayrıca `play-services-base`/`basement` bağımlılıkları AdMob üzerinden zaten
projede, yani gerçek marjinal maliyet 315 KB'den de az. Uygulama zaten GMS'e
bağımlı (AdMob + RevenueCat Play services olmadan çalışmaz), dolayısıyla
ZXing'in "GMS gerektirmez" avantajının bu projede karşılığı yok.

### Kabul edilen iki bedel

**1. Tarama ekranı Google'ın.** Sizin tasarımınız, metinleriniz ve 13 dilde
çevirileriniz orada geçmez; Play services kendi arayüzünü ve kendi
yerelleştirmesini kullanır. Eşleşme akışının uygulamanın geri kalanı gibi
görünmesi bir gün öncelik olursa, karar CameraX veya ZXing lehine
değiştirilmeli — ikisinde de kamera sizin sürecinizde çalışır ve ekranın
tamamı sizindir.

**2. Tarama tek atışlık.** Geçersiz QR okunduğunda Play services ekranı
kapanır ve sonuç `Result.Rejected` olarak döner. "Ekran açık kalsın, sebep
satır içinde yazsın" akışı mümkün değil — kullanıcıya mesajı gösterip **tek
dokunuşla tekrar denenebilen** bir düğme bırakın.

### İleriye dönük tuzak

Uygulama `CAMERA` iznini **başka bir sebeple** manifest'e eklerse bu muafiyet
kaybolur: Android, kullanıcının reddettiği bir izni vekil üzerinden aşmayı
engeller, o yüzden "beyan edilmiş ama verilmemiş" durumunda devretme yolu da
kapanır. Mevcut manifest'te `CAMERA` yok (doğrulandı). İleride kamera
gerektiren bir özellik eklenirse burası yeniden değerlendirilmeli;
`CodeScannerPairing` bu durumu `CODE_SCANNER_CAMERA_PERMISSION_NOT_GRANTED`
dalıyla ele alıyor ve sebebi mesajda söylüyor.

### Değişmeyen kural

Tarayıcı hangisi olursa olsun ham metin **daima** `PairingPayload.parse`
üzerinden geçer. LAN kısıtı ve şema doğrulaması orada; tarayıcı kütüphanesi
hiçbir güvenlik sağlamaz, yalnızca metin getirir.

---

## 3. Gradle bağımlılıkları

`android/app/build.gradle.kts` → `dependencies` bloğuna **tek satır**:

```kotlin
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
```

Hepsi bu. Doğrulandı: yedi dosyanın tamamı yalnızca bu bağımlılıkla (artı
projede zaten bulunan `androidx.core:core-ktx` ve `androidx.activity:activity-ktx`)
AGP 8.13.2 / compileSdk 36 / minSdk 31 / Java 17 altında derleniyor. CameraX,
ML Kit ve ZXing'e ihtiyaç **yok**.

`core/` dosyaları **hiçbir ek bağımlılık istemez** — `javax.crypto` ve
`java.net` JDK'da. Yani eşleşme mantığı APK'ya sıfır bayt bağımlılık ekler;
315 KB'ın tamamı tarayıcıdan geliyor.

---

## 4. Manifest

Sürüm derlemesi için `android/app/src/main/AndroidManifest.xml`'e eklenecek
**tek satır**:

```xml
    <!-- Aynı WiFi ağındaki host'a TCP bağlantısı -->
    <uses-permission android:name="android.permission.INTERNET" />
```

Eklenmeyecekler ve sebepleri:

| | |
|---|---|
| `CAMERA` | **eklemeyin** — kamera Play services sürecinde açılır. Eklemek muafiyeti bozar (bkz. §2 "tuzak"). |
| Tarama Activity kaydı | gerekmez — ekran bizim değil |
| `ACCESS_NETWORK_STATE` | zaten birleşik manifest'te |

> **`INTERNET` neden yine de açıkça yazılmalı.** Birleşik manifest'te
> `INTERNET` ve `ACCESS_NETWORK_STATE` **zaten var** — AdMob'un kütüphane
> manifest'inden geliyor (doğrulandı). Yani teknik olarak eklemeseniz de
> çalışır. Ama o zaman WiFi özelliğinin izni AdMob'un varlığına bağlı kalır;
> reklamlar bir gün çıkarılırsa özellik sessizce ölür. Kullanıcıya görünen
> hiçbir şey değişmiyor (kurulum zamanı izni, listede zaten var), o yüzden
> açıkça beyan etmek bedavaya sağlamlık.

Tanı ekranı **yalnızca debug**: `WifiDiagnosticsActivity` kaydını
`src/debug/AndroidManifest.xml`'e koyun (o dosya projede zaten var), sürüm
derlemesine hiç girmesin.

```xml
        <activity
            android:name=".wifi.WifiDiagnosticsActivity"
            android:exported="false" />
```

> **Gizlilik beyanı.** Mevcut "hiçbir yere veri göndermez" iddiası korunuyor:
> bağlantı yalnızca kullanıcının kendi LAN'ındaki, QR ile kendi onayladığı
> makineye kuruluyor ve `PairingPayload` genel IP'lere bağlanmayı zaten
> reddediyor. Play Console'daki Veri Güvenliği formunda bunu böyle açıklamak
> yeterli. Kamera verisi hiç toplanmıyor — tarama başka bir süreçte oluyor ve
> uygulamaya yalnızca çözülmüş metin dönüyor.

---

## 5. Rapor hunisini paylaşmak — işin özü

`HidReportSender.sendGamepadReportTo` şu an 8 baytı üretip Bluetooth'a
veriyor. WiFi yolu **aynı 8 baytı** ister. `WifiFrameCodec.encodeReport`
imzası bilinçli olarak `sendGamepadReport` ile birebir aynı:

```kotlin
// HidReportSender.kt — mevcut
fun sendGamepadReport(buttons: Int, hat: Int, lx: Int, ly: Int,
                      rx: Int, ry: Int, lt: Int, rt: Int)

// WifiFrameCodec.kt — yeni, aynı alanlar aynı sırada
fun encodeReport(buttons: Int, hat: Int, lx: Int, ly: Int,
                 rx: Int, ry: Int, lt: Int, rt: Int): ByteArray
```

Önerilen bağlama noktası `ConnectionManager` (veya raporları
`HidReportSender`'a veren her neresiyse), çünkü `HidReportSender`'ın KDoc'u
onu "gamepad raporlarının tek hunisi" olarak tanımlıyor — o huniye ikinci bir
çıkış eklemek doğru yer:

```kotlin
    private var wifi: WifiGamepadClient? = null

    fun sendGamepadReport(buttons: Int, hat: Int, lx: Int, ly: Int,
                          rx: Int, ry: Int, lt: Int, rt: Int) {
        hidSender.sendGamepadReport(buttons, hat, lx, ly, rx, ry, lt, rt)
        // Ağ yazımı bloklar → ana iş parçacığında ÇAĞIRMAYIN.
        wifi?.let { client ->
            netHandler.post {
                try {
                    client.sendReport(buttons, hat, lx, ly, rx, ry, lt, rt)
                } catch (e: IOException) {
                    // Tek başarısız rapor oturumu öldürmemeli; bir sonraki
                    // rapor zaten üzerine yazacak. Bluetooth tarafında da
                    // düşen rapor ölümcül değil.
                    Log.v(TAG, "wifi rapor düştü", e)
                }
            }
        }
    }
```

### Dikkat edilecek üç şey

1. **İş parçacığı.** `WifiGamepadClient` senkron ve bloklayıcıdır. Ana iş
   parçacığından çağrılırsa `NetworkOnMainThreadException` atar — bu bilinçli
   (sessiz ANR yerine gürültülü hata). Tek bir `HandlerThread` veya
   `Dispatchers.IO` üzerinde sıraya koyun; sınıf tek iş parçacığından
   kullanılmayı bekler.

2. **Nötr rapor.** Bluetooth tarafında takılı tuş koruması
   `broadcastNeutral()` ile zaten var. WiFi tarafında `close()` bunu kendisi
   yapıyor, ama uygulama arka plana atılırken (`ProcessLifecycleOwner`
   `ON_STOP` gözlemcisi) WiFi istemcisine de `sendNeutral()` göndermek
   gerekir — aksi hâlde ekran kapanınca host'ta tuş basılı kalır.

3. **Kadans.** BT tarafında rapor ~4–8 ms'de bir gidiyor. Aynı kadansı
   TCP'ye vermek saniyede ~250 çerçeve demek; LAN'da sorun değil ama
   `TCP_NODELAY` şart (istemci zaten açıyor). Değer değişmediğinde rapor
   göndermemek (BT tarafındaki gibi durum farkı) ağ trafiğini ciddi düşürür.

---

## 6. Flutter katmanı

`WifiConnectionState.wireTag()`, mevcut `ConnectionState.wireTag()` ile aynı
deseni izliyor; EventChannel sözleşmesi aynı şekilde kurulabilir:

```kotlin
// HidChannelHandler yanına, aynı desenle
eventSink?.success(mapOf(
    "transport" to "wifi",
    "state" to state.wireTag(),
    "paired" to (state as? WifiConnectionState.Connected)?.paired,
))
```

Dart tarafında taşıyıcı seçimi (`bluetooth` / `wifi`) tek bir enum'a
bağlanabilir; iki durum modeli bilinçli olarak birleştirilmedi, gerekçesi
`WifiConnectionState.kt` KDoc'unda.

`rejectReasonKey(reason)` sabit anahtarlar döndürür
(`pairing_failed`, `host_busy`, …) — bunlar `Strings.kt`'ye eklenip 13 dile
çevrilecek metinlerin anahtarlarıdır.

---

## 7. R8 / ProGuard

`core/` sınıfları yansıma (reflection) kullanmaz, özel kural gerektirmez.

`CodeScannerPairing` bir `object` ve doğrudan çağrılıyor; `WifiDiagnosticsActivity`
manifest'te adıyla anıldığı için R8 onu zaten korur. `play-services-code-scanner`
kendi consumer kurallarını taşır.

Tek dikkat: `WifiDiagnosticsActivity`'yi **sürüm derlemesine almayın** —
tanı amaçlı bir ekran, son kullanıcıya gösterilmemeli. Manifest kaydını
`src/debug/AndroidManifest.xml`'e koymak en temizi (o dosya projede zaten
var).

---

## 8. Doğrulama sırası

Bu sırayla ilerleyin; her adım bir öncekini varsayar:

1. **Host'u hazırla.** `host/DAEMON_PATCH.md`'yi `vpad_daemon.py`'ye uygula,
   `python vpad_daemon.py --pair --inject log` ile çalıştır. QR basılmalı.

2. **Telefon olmadan sına.** `python host/vpad_reference_client.py "<payload>"
   --press A --hold 2` — host'ta A tuşuna basılı raporlar görünmeli. Bu
   adım host tarafının doğru olduğunu kanıtlar.

3. **Kotlin çekirdeğini sına.** `jvm-verify/` içinde `gradle test` — 45 test
   geçmeli. Bu adım Kotlin tarafının Python ile aynı teli konuştuğunu
   kanıtlar (altın vektör testi).

4. **Tanı ekranıyla gerçek telefon.** `WifiDiagnosticsActivity`'yi debug
   derlemesine ekleyip QR'ı tara. Ekranda `durum → connected` ve host'ta
   düğmelere karşılık gelen raporlar görünmeli.

5. **Rapor hunisini bağla.** Ancak 4. adım çalıştıktan sonra §5'teki
   entegrasyonu yap.

Adım 4 çalışmıyorsa sorun kamera/izin/ağ katmanındadır — protokol katmanı
adım 2 ve 3 ile zaten doğrulanmış olur.

---

## 9. Bilinçli olarak yapılmayanlar

- **Şifreli girdi kanalı.** El sıkışma replay'e kapalı ama sonraki REPORT
  trafiği şifresiz. Gerekçe ve kapsam: `../DESIGN.md` §4.
- **Rumble.** Protokolde output yolu yok; BT descriptor'ında da yok.
- **Çoklu telefon.** Daemon tasarım gereği tek istemci.
- **mDNS keşfi.** Daemon'da zaten var ve korunuyor; QR onun yerine değil
  yanına geliyor.
