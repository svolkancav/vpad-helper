package com.dfnmondo.gamepad.app.wifi

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.annotation.OptIn
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScanner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * QR tarama ekranı — kamera ile eşleşme.
 *
 * **Bu dosya Android'e özgüdür ve `jvm-verify` projesinde derlenmez.** Asıl
 * mantık (payload doğrulama) `PairingPayload`'da ve o JVM'de test ediliyor;
 * burada kalan yalnızca kamera boru hattı.
 *
 * Ekranı bilinçli olarak **programatik** kuruldu — XML layout, `res/values`
 * dizesi veya çizim kaynağı YOK. Sebep: bu dosya mevcut bir uygulamaya
 * bırakılacak; kaynak birleştirme ne kadar az olursa entegrasyon o kadar
 * az yerde çakışır. Uygulamanın kendi tasarım diline geçirmek isterseniz
 * [buildContentView] tek dokunma noktası.
 *
 * ## Sonuç sözleşmesi
 *
 * Başarıda `RESULT_OK` ve intent'te [EXTRA_PAYLOAD] (doğrulanmış `vpad://`
 * metni). Çağıran taraf onu tekrar [PairingPayload.parse] ile çözer —
 * doğrulama tek yerde kalsın diye ayrıştırılmış alanlar taşınmıyor.
 *
 * İptal veya izin reddinde `RESULT_CANCELED` ve [EXTRA_ERROR].
 */
class QrScanActivity : ComponentActivity() {

    private lateinit var previewView: PreviewView
    private lateinit var statusText: TextView
    private lateinit var analysisExecutor: ExecutorService
    private var scanner: BarcodeScanner? = null

    /** İlk geçerli QR'dan sonra ikinci sonucu işlemeyi durdurur. */
    private val finished = AtomicBoolean(false)

    /**
     * Aynı geçersiz QR kameradan saniyede onlarca kez geçer; mesajı her
     * karede güncellemek ekranı titretir ve logu doldurur.
     */
    private var lastRejection: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        analysisExecutor = Executors.newSingleThreadExecutor()
        setContentView(buildContentView())

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED
        ) {
            startCamera()
        } else {
            permissionLauncher.launch(Manifest.permission.CAMERA)
        }
    }

    private val permissionLauncher = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) {
            startCamera()
        } else {
            // Kamera olmadan QR eşleşmesi mümkün değil. Sessizce boş ekran
            // göstermek yerine sebebi çağırana taşı.
            failAndFinish("camera_permission_denied")
        }
    }

    /** Tek dokunma noktası: ekranı uygulamanın tasarımına geçirmek isterseniz. */
    private fun buildContentView(): ViewGroup {
        val root = FrameLayout(this)
        previewView = PreviewView(this).apply {
            layoutParams = FrameLayout.LayoutParams(MATCH, MATCH)
        }
        statusText = TextView(this).apply {
            layoutParams = FrameLayout.LayoutParams(MATCH, WRAP).apply {
                gravity = Gravity.BOTTOM
            }
            setPadding(48, 32, 48, 96)
            setBackgroundColor(Color.argb(160, 0, 0, 0))
            setTextColor(Color.WHITE)
            textSize = 16f
            gravity = Gravity.CENTER
            text = STATUS_AIM
        }
        root.addView(previewView)
        root.addView(statusText)
        return root
    }

    private fun startCamera() {
        scanner = BarcodeScanning.getClient(
            BarcodeScannerOptions.Builder()
                // Yalnızca QR: diğer barkod biçimlerini aramak her karede
                // gereksiz iş ve yanlış pozitif demek.
                .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
                .build(),
        )

        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            val provider = try {
                providerFuture.get()
            } catch (e: Exception) {
                Log.e(TAG, "kamera saglayicisi alinamadi", e)
                failAndFinish("camera_unavailable")
                return@addListener
            }

            val preview = Preview.Builder().build().also {
                it.surfaceProvider = previewView.surfaceProvider
            }
            val analysis = ImageAnalysis.Builder()
                // Kare biriktirmek gecikme yaratır ve QR taramada bir işe
                // yaramaz — en güncel kare her zaman en iyi kare.
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
                .also { it.setAnalyzer(analysisExecutor, ::analyze) }

            try {
                provider.unbindAll()
                provider.bindToLifecycle(
                    this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis,
                )
            } catch (e: Exception) {
                Log.e(TAG, "kamera baglanamadi", e)
                failAndFinish("camera_bind_failed")
            }
        }, ContextCompat.getMainExecutor(this))
    }

    @OptIn(ExperimentalGetImage::class)
    private fun analyze(proxy: ImageProxy) {
        val media = proxy.image
        val client = scanner
        if (media == null || client == null || finished.get()) {
            proxy.close()
            return
        }
        val image = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
        client.process(image)
            .addOnSuccessListener { barcodes ->
                for (barcode in barcodes) {
                    val raw = barcode.rawValue ?: continue
                    if (handleScanned(raw)) return@addOnSuccessListener
                }
            }
            .addOnFailureListener { Log.w(TAG, "barkod cozulemedi", it) }
            // İki `close` çağrısı olmaması için tek yerde: her iki dal da
            // buradan geçer.
            .addOnCompleteListener { proxy.close() }
    }

    /** @return true → geçerli QR bulundu, tarama bitti. */
    private fun handleScanned(raw: String): Boolean {
        val info = try {
            PairingPayload.parse(raw)
        } catch (e: PairingException) {
            showRejection(raw, e.message ?: "geçersiz")
            return false
        }

        // `compareAndSet`: ML Kit geri çağrıları farklı karelerden art arda
        // gelebilir; ekranı yalnızca ilk geçerli sonuç kapatmalı.
        if (!finished.compareAndSet(false, true)) return true

        Log.i(TAG, "eslesme QR'i okundu: $info")
        runOnUiThread {
            setResult(
                Activity.RESULT_OK,
                Intent().putExtra(EXTRA_PAYLOAD, raw.trim()),
            )
            finish()
        }
        return true
    }

    private fun showRejection(raw: String, reason: String) {
        // Kullanıcıya teknik mesajı değil, ne yapması gerektiğini söyle.
        val message = when {
            !raw.trimStart().startsWith(PairingPayload.SCHEME) -> STATUS_NOT_VPAD
            reason.contains("LAN") -> STATUS_NOT_LAN
            else -> STATUS_INVALID
        }
        if (message == lastRejection) return
        lastRejection = message
        Log.w(TAG, "QR reddedildi: $reason")
        runOnUiThread { statusText.text = message }
    }

    private fun failAndFinish(error: String) {
        if (!finished.compareAndSet(false, true)) return
        runOnUiThread {
            setResult(
                Activity.RESULT_CANCELED,
                Intent().putExtra(EXTRA_ERROR, error),
            )
            finish()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        analysisExecutor.shutdown()
        scanner?.close()
        scanner = null
    }

    companion object {
        private const val TAG = "VPadQrScan"

        const val EXTRA_PAYLOAD = "vpad_payload"
        const val EXTRA_ERROR = "vpad_error"

        private const val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
        private const val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT

        // Bu metinler bilinçli olarak sabit: dosya kaynak birleştirme
        // gerektirmesin. Uygulamanın `Strings.kt`'sine taşımak için buradan
        // alın (13 dile çevrilmeleri gerekir).
        private const val STATUS_AIM =
            "Bilgisayardaki QR kodunu çerçeveye alın"
        private const val STATUS_NOT_VPAD =
            "Bu bir V-Pad eşleşme kodu değil"
        private const val STATUS_NOT_LAN =
            "Bu kod yerel ağ dışını gösteriyor — güvenlik için reddedildi"
        private const val STATUS_INVALID =
            "Kod okundu ama geçersiz. Bilgisayarda yeni kod üretin."

        /** Çağıran taraf için hazır intent. */
        fun intent(context: Context): Intent =
            Intent(context, QrScanActivity::class.java)
    }
}
