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
├── QrScanActivity.kt        ← ui/    (Android'e özgü)
└── WifiDiagnosticsActivity.kt ← ui/  (Android'e özgü, opsiyonel)
```

`core/` dosyalarını **değiştirmeden** kopyalayın. Değiştirmeniz gerekirse
`jvm-verify/` projesini de yanınıza alın ve testleri koşturun — o dosyaların
tek güvencesi bu.

---

## 2. QR tarayıcı seçimi — önce bunu kararlaştırın

Bu proje APK boyutuna duyarlı (Unity Ads 4,08 MB yüzünden çıkarılmıştı), o
yüzden seçenekleri boyutlarıyla veriyorum:

| Seçenek | Ek boyut | CAMERA izni | Not |
|---|---|---|---|
| **`play-services-code-scanner`** | **~70 KB** | **gerekmez** | Google Play services'in kendi tarayıcı arayüzü; model cihaza talep üzerine iner. **Önerilen.** |
| `mlkit:barcode-scanning` (paket içi model) | ~2,8 MB | gerekir | Çevrimdışı ilk taramada bile çalışır |
| `play-services-mlkit-barcode-scanning` | ~200 KB | gerekir | Model Play services'ten iner; kendi kamera arayüzünüzü yazarsınız |

`ui/QrScanActivity.kt` **ikinci/üçüncü seçenek** içindir (CameraX + ML Kit,
kendi kamera ekranı). Birinci seçeneği tercih ederseniz o dosyaya hiç
ihtiyacınız yok — yerine şu kadarı yeter:

```kotlin
// build.gradle.kts
implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
```

```kotlin
// Çağıran yer — kamera izni YOK, kendi ekranınız YOK
GmsBarcodeScanning.getClient(
    context,
    GmsBarcodeScannerOptions.Builder()
        .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
        .build(),
).startScan()
    .addOnSuccessListener { barcode ->
        val raw = barcode.rawValue ?: return@addOnSuccessListener
        // Doğrulama HER ZAMAN buradan geçer — tarayıcı hangisi olursa olsun.
        val info = try {
            PairingPayload.parse(raw)
        } catch (e: PairingException) {
            showError(e.message); return@addOnSuccessListener
        }
        connectOverWifi(info)
    }
    .addOnFailureListener { showError(it.message) }
```

**Kritik nokta:** tarayıcı hangisi olursa olsun ham metin daima
`PairingPayload.parse` üzerinden geçmelidir. LAN kısıtı ve şema doğrulaması
orada; tarayıcının kendisi hiçbir güvenlik sağlamaz.

---

## 3. Gradle bağımlılıkları

`android/app/build.gradle.kts` → `dependencies` bloğuna.

**Seçenek 1 (önerilen — code scanner):**

```kotlin
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
```

**Seçenek 2 (CameraX + ML Kit, `QrScanActivity.kt` kullanılacaksa):**

```kotlin
    val cameraX = "1.4.2"
    implementation("androidx.camera:camera-core:$cameraX")
    implementation("androidx.camera:camera-camera2:$cameraX")
    implementation("androidx.camera:camera-lifecycle:$cameraX")
    implementation("androidx.camera:camera-view:$cameraX")
    implementation("com.google.mlkit:barcode-scanning:17.3.0")
    // QrScanActivity ComponentActivity + ActivityResult API kullanıyor
    implementation("androidx.activity:activity-ktx:1.9.3")
```

`core/` dosyaları **hiçbir ek bağımlılık istemez** — `javax.crypto` ve
`java.net` JDK'da. Yani eşleşme mantığı APK'ya sıfır bayt bağımlılık ekler.

Mevcut `minSdk = 31` ve `compileSdk = 36` bu sürümlerle uyumlu; Java 17
hedefi de yeterli.

---

## 4. Manifest

`android/app/src/main/AndroidManifest.xml`:

```xml
    <!-- Aynı WiFi ağındaki host'a TCP bağlantısı -->
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />

    <!-- YALNIZCA Seçenek 2'de (kendi kamera ekranınız) gerekir.
         play-services-code-scanner kullanıyorsanız BU SATIRI EKLEMEYİN —
         gereksiz izin, mağaza incelemesinde açıklama ister. -->
    <uses-permission android:name="android.permission.CAMERA" />
    <uses-feature android:name="android.hardware.camera.any" android:required="false" />
```

`<application>` içine (yalnızca kullandıklarınızı):

```xml
        <activity
            android:name=".wifi.QrScanActivity"
            android:exported="false"
            android:screenOrientation="portrait" />
        <activity
            android:name=".wifi.WifiDiagnosticsActivity"
            android:exported="false" />
```

`android:exported="false"` önemli: bu ekranların dışarıdan başlatılması için
hiçbir sebep yok.

> **Not:** `INTERNET` izni, uygulamanın gizlilik beyanını etkiler. Mevcut
> "hiçbir yere veri göndermez" iddiası korunuyor — bağlantı yalnızca
> kullanıcının kendi LAN'ındaki, QR ile kendi onayladığı makineye kuruluyor
> ve `PairingPayload` genel IP'lere bağlanmayı zaten reddediyor. Play
> Console'daki Veri Güvenliği formunda bunu böyle açıklamak yeterli.

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

`QrScanActivity` ve `WifiDiagnosticsActivity` manifest'te adlarıyla
anıldıkları için R8 onları zaten korur. ML Kit ve CameraX kendi consumer
kurallarını taşır.

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
