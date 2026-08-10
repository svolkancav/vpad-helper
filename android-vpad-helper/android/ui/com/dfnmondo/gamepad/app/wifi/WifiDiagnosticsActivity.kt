package com.dfnmondo.gamepad.app.wifi

import android.app.Activity
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * WiFi eşleşmesi tanı ekranı.
 *
 * Amacı **doğrulama**, oynanabilirlik değil: QR tara → eşleş → birkaç test
 * girdisi gönder → ne olduğunu satır satır gör. Gerçek oyun arayüzü
 * uygulamanın kendi Flutter katmanında; bu ekran o bağlantı yapılmadan önce
 * "boru hattı gerçekten çalışıyor mu" sorusunu cevaplar.
 *
 * **Android'e özgüdür, `jvm-verify`'da derlenmez.**
 *
 * ## İş parçacığı
 *
 * [WifiGamepadClient] senkron ve bloklayıcıdır; Android'de ana iş
 * parçacığından çağrılırsa `NetworkOnMainThreadException` atar. Burada tüm
 * ağ işi tek bir [HandlerThread] üzerinde sıraya girer — böylece hem ana iş
 * parçacığı serbest kalır hem de istemciye tek iş parçacığından erişilir
 * (sınıfın sözleşmesi bu).
 */
class WifiDiagnosticsActivity : ComponentActivity() {

    private lateinit var statusView: TextView
    private lateinit var logView: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var actionRow: LinearLayout

    private lateinit var netThread: HandlerThread
    private lateinit var net: Handler
    private val ui = Handler(Looper.getMainLooper())

    private var client: WifiGamepadClient? = null

    private val scanLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val payload = result.data?.getStringExtra(QrScanActivity.EXTRA_PAYLOAD)
            if (payload != null) connect(payload) else log("QR sonucu boş geldi")
        } else {
            val error = result.data?.getStringExtra(QrScanActivity.EXTRA_ERROR)
            log("tarama iptal edildi${if (error != null) " ($error)" else ""}")
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        netThread = HandlerThread("vpad-wifi").apply { start() }
        net = Handler(netThread.looper)
        setContentView(buildContentView())
        setActionsEnabled(false)

        // Doğrudan payload ile de açılabilsin (adb ile test için):
        //   adb shell am start -n <paket>/.wifi.WifiDiagnosticsActivity \
        //     -e vpad_payload "vpad://192.168.1.34:53124?t=...&v=1"
        val direct = intent?.getStringExtra(QrScanActivity.EXTRA_PAYLOAD)
        if (direct != null) connect(direct) else log("Hazır. QR taramak için düğmeye basın.")
    }

    // ── Arayüz ──────────────────────────────────────────────────────

    private fun buildContentView(): View {
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(32, 48, 32, 32)
            setBackgroundColor(Color.parseColor("#101418"))
        }

        statusView = TextView(this).apply {
            textSize = 18f
            setTextColor(Color.WHITE)
            setTypeface(null, Typeface.BOLD)
            text = "Bağlı değil"
        }
        root.addView(statusView)

        root.addView(Button(this).apply {
            text = "QR tara ve eşleş"
            setOnClickListener { scanLauncher.launch(QrScanActivity.intent(context)) }
        })

        actionRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
        }
        addAction("A") { it.sendReport(buttons = BTN_A) }
        addAction("B") { it.sendReport(buttons = BTN_B) }
        addAction("↑") { it.sendReport(hat = 0) }
        addAction("→") { it.sendReport(hat = 2) }
        addAction("Çubuk") { it.sendReport(lx = 255, ly = 0) }
        addAction("Nötr") { it.sendNeutral() }
        root.addView(actionRow)

        root.addView(Button(this).apply {
            text = "Bağlantıyı kes"
            setOnClickListener { disconnect() }
        })

        logView = TextView(this).apply {
            typeface = Typeface.MONOSPACE
            textSize = 12f
            setTextColor(Color.parseColor("#B8C4CC"))
        }
        logScroll = ScrollView(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f,
            )
            addView(logView)
        }
        root.addView(logScroll)
        return root
    }

    /** Düğmeye basıldığında ilgili raporu AĞ iş parçacığında gönderir. */
    private fun addAction(label: String, action: (WifiGamepadClient) -> Unit) {
        actionRow.addView(
            Button(this).apply {
                text = label
                layoutParams = LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f,
                )
                setOnClickListener {
                    val c = client ?: return@setOnClickListener
                    net.post {
                        try {
                            action(c)
                            log("$label gönderildi")
                        } catch (e: IOException) {
                            log("gönderilemedi: ${e.message}")
                        }
                    }
                }
            },
        )
    }

    private fun setActionsEnabled(enabled: Boolean) {
        for (i in 0 until actionRow.childCount) {
            actionRow.getChildAt(i).isEnabled = enabled
        }
    }

    // ── Bağlantı ────────────────────────────────────────────────────

    private fun connect(payload: String) {
        val info = try {
            PairingPayload.parse(payload)
        } catch (e: PairingException) {
            log("payload reddedildi: ${e.message}")
            return
        }
        log("bağlanılıyor: ${info.host}:${info.port}")

        net.post {
            // Önceki oturum varsa kapat — çift istemci host'ta "meşgul"
            // reddine yol açar.
            client?.close()
            val c = WifiGamepadClient(deviceLabel()) { state -> onState(state) }
            client = c
            try {
                c.connect(info)
            } catch (e: WifiRejectedException) {
                log("REDDEDİLDİ (${rejectReasonKey(e.reason)}): ${e.detail}")
                client = null
            } catch (e: Exception) {
                log("bağlantı hatası: ${e.message}")
                client = null
            }
        }
    }

    private fun disconnect() {
        net.post {
            client?.close()
            client = null
        }
    }

    private fun onState(state: WifiConnectionState) {
        ui.post {
            statusView.text = when (state) {
                is WifiConnectionState.Connected ->
                    "Bağlı — ${state.host}:${state.port}" +
                        if (state.paired) " (QR eşleşmeli)" else " (eşleşmesiz)"
                is WifiConnectionState.Connecting -> "Bağlanılıyor…"
                is WifiConnectionState.Pairing -> "Eşleşiliyor…"
                is WifiConnectionState.Rejected ->
                    "Reddedildi: ${rejectReasonKey(state.reason)}"
                is WifiConnectionState.Failed -> "Hata: ${state.message}"
                else -> "Bağlı değil"
            }
            setActionsEnabled(state is WifiConnectionState.Connected)
            log("durum → ${state.wireTag()}")
        }
    }

    // ── Log ─────────────────────────────────────────────────────────

    private fun log(message: String) {
        Log.i(TAG, message)
        ui.post {
            logView.append("${stamp()}  $message\n")
            logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
        }
    }

    private fun stamp(): String =
        SimpleDateFormat("HH:mm:ss.SSS", Locale.US).format(Date())

    private fun deviceLabel(): String =
        "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}".trim()

    override fun onDestroy() {
        super.onDestroy()
        // Ekran kapanırken bağlantıyı bırak: host'ta takılı tuş kalmasın
        // (close() önce nötr rapor gönderir).
        net.post { client?.close() }
        netThread.quitSafely()
    }

    private companion object {
        const val TAG = "VPadWifiDiag"
        // HidDescriptor ile aynı maskeler.
        const val BTN_A = 0x0001
        const val BTN_B = 0x0002
    }
}

/** Tanı ekranını açmak için hazır intent. */
fun wifiDiagnosticsIntent(context: android.content.Context, payload: String? = null): Intent =
    Intent(context, WifiDiagnosticsActivity::class.java).apply {
        if (payload != null) putExtra(QrScanActivity.EXTRA_PAYLOAD, payload)
    }
