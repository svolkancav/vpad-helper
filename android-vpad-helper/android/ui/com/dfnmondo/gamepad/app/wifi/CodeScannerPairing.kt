package com.dfnmondo.gamepad.app.wifi

import android.content.Context
import android.util.Log
import com.google.android.gms.common.moduleinstall.ModuleInstall
import com.google.android.gms.common.moduleinstall.ModuleInstallRequest
import com.google.mlkit.common.MlKitException
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScanner
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning

/**
 * QR eşleşmesi — **Google Play services code scanner** sürümü.
 *
 * Üç tarayıcı seçeneğinden en küçüğü (315 KB) ve tek kamera izni
 * istemeyeni. Ölçümler ve karşılaştırma: `../INTEGRATION.md` §2.
 *
 * ## Neden Activity yok
 *
 * [QrScanActivity] ve [ZxingQrScanActivity]'nin aksine burada ekran yazmıyoruz:
 * tarama arayüzü **Google Play services sürecinde** çalışıyor. Kamera oraya
 * açılıyor, bize yalnızca sonuç metni dönüyor. Manifest'e `CAMERA` izni
 * koymuyoruz, çalışma zamanı izin diyaloğu göstermiyoruz, izin reddi yolunu
 * kodlamıyoruz.
 *
 * > **Muhtemel tuzak (Google belgelemiyor):** uygulama `CAMERA` iznini başka
 * > bir sebeple manifest'te beyan eder ve izin verilmemiş olursa bu muafiyetin
 * > kaybolması beklenir. Dayanak, aynı devretme kalıbının belgelenmiş hâli:
 * > `MediaStore.ACTION_IMAGE_CAPTURE` için Android, *"if your app targets M
 * > and above and declares as using the CAMERA permission which is not
 * > granted, then attempting to use this action will result in a
 * > SecurityException"* diyor. Code scanner için Google aynı kuralı hiçbir
 * > yerde yazmıyor, yani bu **analojiye dayalı bir tahmin** — ama ucuz bir
 * > tedbir. `gamepad_universal`'ın manifest'inde `CAMERA` yok (doğrulandı);
 * > ileride kamera gerektiren bir özellik eklenirse burası cihazda yeniden
 * > sınanmalı.
 *
 * ## Bunun karşılığında kaybedilen
 *
 * Tarama **tek atışlıktır**: geçersiz bir QR okunduğunda Play services ekranı
 * kapanır ve sonuç bize döner. Diğer iki sürümdeki "ekran açık kalsın, sebep
 * durum çubuğunda yazsın" akışı burada mümkün değil — kullanıcıya mesajı
 * gösterip taramayı yeniden başlatmak gerekir. Akış tasarımınızı buna göre
 * kurun: [Result.Rejected] geldiğinde tek dokunuşla tekrar denenebilen bir
 * düğme bırakın.
 */
object CodeScannerPairing {

    private const val TAG = "VPadCodeScanner"

    /**
     * Eşleşme payload'ını bir intent üzerinden geçirmek için anahtar.
     *
     * Tarama akışında kullanılmıyor (sonuç doğrudan geri çağrıyla dönüyor);
     * telefonsuz/kamerasız test için var — bkz. [WifiDiagnosticsActivity].
     */
    const val EXTRA_PAYLOAD = "vpad_payload"

    /** Tarama sonucu. Her dal kullanıcıya farklı bir şey söylemeyi hak ediyor. */
    sealed class Result {
        /** QR okundu, doğrulandı, bağlanmaya hazır. */
        data class Success(val info: PairingInfo, val raw: String) : Result()

        /**
         * QR okundu ama **geçerli bir eşleşme kodu değil** — yanlış kod, LAN
         * dışını gösteren kod, bozuk token. [userMessageKey] arayüzde
         * gösterilecek metnin anahtarı.
         */
        data class Rejected(val userMessageKey: String, val detail: String) : Result()

        /** Kullanıcı tarama ekranını kapattı. Hata değil; sessizce dön. */
        data object Cancelled : Result()

        /**
         * Tarayıcı modülü cihazda yok ve indirilemedi.
         *
         * **Bu senaryo bu uygulamada sıradan değil:** eşleşme yerel WiFi
         * üzerinde oluyor ve WiFi ≠ internet. Telefon hotspot'unda veya
         * internetsiz bir ev ağında modül inemez. [preWarm] tam olarak bunu
         * önlemek için var.
         */
        data class Unavailable(val detail: String) : Result()

        /** Beklenmeyen hata. */
        data class Failed(val detail: String) : Result()
    }

    private fun scanner(context: Context): GmsBarcodeScanner =
        GmsBarcodeScanning.getClient(
            context,
            GmsBarcodeScannerOptions.Builder()
                // Yalnızca QR: diğer biçimleri aramak yanlış pozitif demek.
                .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
                // Uzaktaki veya küçük QR'a otomatik yakınlaştırma. ZXing'de
                // karşılığı yok; kullanıcının telefonu yaklaştırmasını
                // gerektirmiyor.
                .enableAutoZoom()
                .build(),
        )

    /**
     * Tarayıcı modülünü **önceden** indirtir.
     *
     * Uygulama açılışında, internet varken çağırın. Modül zaten yüklüyse
     * anında döner; değilse arka planda iner ve kullanıcı eşleşmeye geldiğinde
     * hazır olur.
     *
     * Bunu atlamak, "WiFi'ye bağlıyım ama internet yok" durumundaki kullanıcıyı
     * [Result.Unavailable] ile karşı karşıya bırakır — ve o kullanıcı için
     * yapılacak hiçbir şey kalmaz.
     */
    fun preWarm(context: Context, onDone: (Boolean) -> Unit = {}) {
        val client = ModuleInstall.getClient(context)
        val request = ModuleInstallRequest.newBuilder()
            .addApi(scanner(context))
            .build()
        client.installModules(request)
            .addOnSuccessListener {
                Log.i(TAG, "tarayici modulu hazir (zaten yuklu: ${it.areModulesAlreadyInstalled()})")
                onDone(true)
            }
            .addOnFailureListener {
                // Başarısızlık ölümcül değil: kullanıcı internete çıktığında
                // tekrar denenir, ya da tarama anında Play services kendi
                // indirmesini yapar.
                Log.w(TAG, "tarayici modulu onceden indirilemedi", it)
                onDone(false)
            }
    }

    /** Modül şu an kullanılabilir mi? Arayüzde uyarı göstermek için. */
    fun isAvailable(context: Context, onResult: (Boolean) -> Unit) {
        ModuleInstall.getClient(context)
            .areModulesAvailable(scanner(context))
            .addOnSuccessListener { onResult(it.areModulesAvailable()) }
            .addOnFailureListener { onResult(false) }
    }

    /**
     * Tarama ekranını açar ve sonucu [onResult] ile döner.
     *
     * `onResult` **ana iş parçacığında** çağrılır. Dönen [PairingInfo] ile
     * bağlanmak için [WifiGamepadClient.connect]'i arka planda çalıştırın.
     */
    fun scan(context: Context, onResult: (Result) -> Unit) {
        scanner(context).startScan()
            .addOnSuccessListener { barcode ->
                val raw = barcode.rawValue
                if (raw.isNullOrBlank()) {
                    onResult(Result.Rejected("invalid_code", "boş QR içeriği"))
                    return@addOnSuccessListener
                }
                // Doğrulama HER ZAMAN buradan geçer. Tarayıcı kütüphanesi
                // hiçbir güvenlik sağlamaz; yalnızca metin getirir.
                val info = try {
                    PairingPayload.parse(raw)
                } catch (e: PairingException) {
                    val detail = e.message ?: "geçersiz"
                    Log.w(TAG, "QR reddedildi: $detail")
                    onResult(Result.Rejected(rejectionKey(raw, detail), detail))
                    return@addOnSuccessListener
                }
                Log.i(TAG, "eslesme QR'i okundu: $info")
                onResult(Result.Success(info, raw.trim()))
            }
            .addOnFailureListener { error ->
                onResult(toFailure(error))
            }
    }

    /** Kullanıcıya teknik mesajı değil, ne yapması gerektiğini söyleyecek anahtar. */
    private fun rejectionKey(raw: String, detail: String): String = when {
        !raw.trimStart().startsWith(PairingPayload.SCHEME) -> "not_a_vpad_code"
        detail.contains("LAN") -> "code_points_outside_lan"
        else -> "invalid_code"
    }

    private fun toFailure(error: Exception): Result {
        if (error !is MlKitException) {
            return Result.Failed(error.message ?: error.toString())
        }
        return when (error.errorCode) {
            MlKitException.CODE_SCANNER_CANCELLED -> Result.Cancelled
            MlKitException.CODE_SCANNER_UNAVAILABLE ->
                Result.Unavailable("tarayıcı modülü cihazda yok")
            MlKitException.CODE_SCANNER_GOOGLE_PLAY_SERVICES_VERSION_TOO_OLD ->
                Result.Unavailable("Google Play services sürümü eski")
            MlKitException.CODE_SCANNER_CAMERA_PERMISSION_NOT_GRANTED ->
                // Google bu kodu (202) şöyle tanımlıyor: "Camera permission is
                // not granted to Google Play Service." Yani asıl beklenen
                // sebep, kullanıcının PLAY SERVICES'in kamera iznini
                // kapatmış olması — bizim manifest'imizde hiçbir şey
                // olmasa bile gelir. Sınıf KDoc'undaki "beyan edilmiş CAMERA"
                // tahmini ikinci bir ihtimal; mesaj ikisini de karşılasın.
                Result.Failed(
                    "kamera izni verilmedi — Ayarlar'da Google Play services'in " +
                        "kamera iznini açın",
                )
            else -> Result.Failed("tarama hatası (kod ${error.errorCode})")
        }
    }
}
