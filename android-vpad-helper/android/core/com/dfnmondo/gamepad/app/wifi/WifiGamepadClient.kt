package com.dfnmondo.gamepad.app.wifi

import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/**
 * WiFi gamepad istemcisi: eşleşir, bağlanır, rapor gönderir.
 *
 * Yürütülebilir şartname: `host/vpad_reference_client.py` → `VPadClient`.
 * Davranış tartışmalı hâle gelirse doğru cevap oradadır; bu sınıf onun
 * birebir çevirisidir.
 *
 * **Bu dosya Android API'si kullanmaz** — `java.net.Socket` ve stdlib yeter.
 * Böylece JVM testlerinde gerçek soket üzerinden doğrulanabiliyor.
 *
 * ## İş parçacığı sözleşmesi
 *
 * Bu sınıf **senkron ve tek iş parçacıklıdır**. [connect] ve [sendReport]
 * bloklar; çağıranın bunları ana iş parçacığından ÇAĞIRMAMASI gerekir
 * (Android'de `NetworkOnMainThreadException` alırsınız — bu bilinçli, sessiz
 * ANR yerine gürültülü hata). Entegrasyonda bir coroutine veya
 * `HandlerThread` üzerinden sürülür; bkz. `INTEGRATION.md`.
 *
 * [close] herhangi bir iş parçacığından çağrılabilir: soketi kapatır ve
 * bekleyen okumayı `SocketException` ile düşürür.
 */
class WifiGamepadClient(
    private val deviceName: String,
    private val listener: (WifiConnectionState) -> Unit = {},
) {

    @Volatile
    private var socket: Socket? = null
    private var input: InputStream? = null
    private var output: OutputStream? = null
    private val decoder = WifiFrameCodec.Decoder()
    private val readBuffer = ByteArray(4096)

    /** Eşleşme kapısından geçildi mi (false = eşleşmesiz mod). */
    var paired: Boolean = false
        private set

    @Volatile
    var state: WifiConnectionState = WifiConnectionState.Idle
        private set

    private fun emit(next: WifiConnectionState) {
        state = next
        listener(next)
    }

    /**
     * Bağlan, gerekiyorsa eşleş, HELLO_ACK'e kadar git.
     *
     * ## Sıralama neden böyle
     *
     * Eşleşme açıkken host, bağlantıyı kabul eder etmez CHALLENGE gönderir ve
     * istemcinin ilk çerçevesinin AUTH olmasını bekler. Eşleşme kapalıyken
     * host hiçbir şey göndermez, doğrudan HELLO bekler.
     *
     * İstemci hangi durumda olduğunu baştan bilemez: QR'da token var ama host
     * `--pair` olmadan yeniden başlatılmış olabilir. Bu yüzden ilk çerçeve
     * KISA bir zaman aşımıyla beklenir:
     *
     *  - CHALLENGE geldi → AUTH gönder, eşleşmiş moda geç
     *  - zaman aşımı     → host sessiz, eşleşmesiz moda düş
     *  - REJECT geldi    → [WifiRejectedException]
     *
     * İki tarafın da birbirini beklediği kilitlenme böylece imkânsız.
     *
     * @throws WifiRejectedException host açıkça reddetti
     * @throws WifiProtocolException tel üzerinde beklenmeyen şey
     * @throws IOException ağ hatası
     */
    @Throws(IOException::class)
    fun connect(
        info: PairingInfo,
        connectTimeoutMs: Int = DEFAULT_CONNECT_TIMEOUT_MS,
        readTimeoutMs: Int = DEFAULT_READ_TIMEOUT_MS,
        pairWaitMs: Int = DEFAULT_PAIR_WAIT_MS,
    ) {
        emit(WifiConnectionState.Connecting(info.host, info.port))

        val sock = Socket()
        try {
            sock.connect(InetSocketAddress(info.host, info.port), connectTimeoutMs)
            // Rapor kadansı (~4-8 ms) Nagle tarafından birleştirilmemeli:
            // birleşme, girdi gecikmesini doğrudan artırır.
            sock.tcpNoDelay = true
            socket = sock
            input = sock.getInputStream()
            output = sock.getOutputStream()

            // ── 1) Eşleşme kapısı ──
            sock.soTimeout = pairWaitMs
            val first: WifiFrameCodec.Frame? = try {
                readFrame()
            } catch (_: SocketTimeoutException) {
                null // host sessiz → eşleşme kapalı
            }
            sock.soTimeout = readTimeoutMs

            if (first != null) {
                when (first.type) {
                    WifiFrameCodec.T_CHALLENGE -> {
                        if (first.payload.size != PairingCrypto.CHALLENGE_LEN) {
                            throw WifiProtocolException(
                                "challenge ${PairingCrypto.CHALLENGE_LEN} bayt " +
                                    "olmalı, ${first.payload.size} geldi",
                            )
                        }
                        emit(WifiConnectionState.Pairing(info.host, info.port))
                        val body = PairingCrypto.buildAuthBody(info.token, first.payload)
                        write(WifiFrameCodec.encodeFrame(WifiFrameCodec.T_AUTH, body))
                        paired = true
                    }
                    WifiFrameCodec.T_REJECT -> throwRejected(first.payload)
                    else -> throw WifiProtocolException(
                        "ilk çerçeve CHALLENGE olmalıydı, " +
                            "0x${first.type.toString(16)} geldi",
                    )
                }
            }

            // ── 2) HELLO / HELLO_ACK ──
            write(WifiFrameCodec.encodeHello(deviceName))
            val ack = readFrame()
            if (ack.type == WifiFrameCodec.T_REJECT) throwRejected(ack.payload)
            if (ack.type != WifiFrameCodec.T_HELLO_ACK) {
                throw WifiProtocolException(
                    "HELLO_ACK bekleniyordu, 0x${ack.type.toString(16)} geldi",
                )
            }

            emit(WifiConnectionState.Connected(info.host, info.port, paired))
        } catch (e: WifiRejectedException) {
            closeQuietly()
            emit(WifiConnectionState.Rejected(e.reason, e.detail))
            throw e
        } catch (e: Exception) {
            closeQuietly()
            emit(WifiConnectionState.Failed(e.message ?: e.toString()))
            throw e
        }
    }

    /**
     * Tek rapor gönder. Alan anlamları için [WifiFrameCodec.encodeReport].
     *
     * Mevcut uygulamada bu değerleri zaten `HidReportSender` üretiyor; WiFi
     * yolu aynı değerleri alır.
     */
    @Throws(IOException::class)
    fun sendReport(
        buttons: Int = 0,
        hat: Int = WifiFrameCodec.HAT_CENTER,
        lx: Int = 128,
        ly: Int = 128,
        rx: Int = 128,
        ry: Int = 128,
        lt: Int = 0,
        rt: Int = 0,
    ) {
        write(WifiFrameCodec.encodeReport(buttons, hat, lx, ly, rx, ry, lt, rt))
    }

    /**
     * Her şey bırakılmış durum.
     *
     * Kopmadan önce gönderilir: telefon basılı tuşla koparsa host'ta tuş
     * takılı kalır. Bluetooth tarafında aynı koruma `broadcastNeutral()` ile
     * zaten var; WiFi yolunun da kendi karşılığı olmalı.
     */
    @Throws(IOException::class)
    fun sendNeutral() = sendReport()

    /** PING gönderir ve PONG bekler. */
    @Throws(IOException::class)
    fun ping() {
        write(WifiFrameCodec.encodeFrame(WifiFrameCodec.T_PING))
        val frame = readFrame()
        if (frame.type != WifiFrameCodec.T_PONG) {
            throw WifiProtocolException(
                "PONG bekleniyordu, 0x${frame.type.toString(16)} geldi",
            )
        }
    }

    /** Nötr rapor + BYE gönderip soketi kapatır. Hata yutulur. */
    fun close() {
        if (socket == null) return
        try {
            sendNeutral()
            write(WifiFrameCodec.encodeFrame(WifiFrameCodec.T_BYE))
        } catch (_: IOException) {
            // Kapanışta yazma hatası beklenen bir şey; bastırılıyor.
        }
        closeQuietly()
        if (state !is WifiConnectionState.Rejected) {
            emit(WifiConnectionState.Disconnected)
        }
    }

    private fun closeQuietly() {
        try {
            socket?.close()
        } catch (_: IOException) {
        }
        socket = null
        input = null
        output = null
    }

    @Throws(IOException::class)
    private fun write(bytes: ByteArray) {
        val out = output ?: throw WifiProtocolException("bağlantı yok")
        out.write(bytes)
        out.flush()
    }

    /** Bir tam çerçeve okunana kadar bekler. */
    @Throws(IOException::class)
    private fun readFrame(): WifiFrameCodec.Frame {
        val stream = input ?: throw WifiProtocolException("bağlantı yok")
        while (true) {
            decoder.poll()?.let { return it }
            val read = stream.read(readBuffer)
            if (read < 0) throw WifiProtocolException("karşı taraf soketi kapattı")
            if (read > 0) decoder.push(readBuffer, read)
        }
    }

    private fun throwRejected(payload: ByteArray): Nothing {
        val reason = if (payload.isNotEmpty()) payload[0].toInt() and 0xFF
        else WifiFrameCodec.R_INTERNAL_ERROR
        val detail = if (payload.size > 1) {
            String(payload, 1, payload.size - 1, Charsets.UTF_8)
        } else {
            ""
        }
        throw WifiRejectedException(reason, detail)
    }

    companion object {
        const val DEFAULT_CONNECT_TIMEOUT_MS = 8_000

        /**
         * Bağlantı kurulduktan sonraki okuma zaman aşımı.
         *
         * Host boştayken 2 sn'de bir PING beklendiği için 10 sn hem onu hem
         * bir WiFi kesintisini rahat karşılar.
         */
        const val DEFAULT_READ_TIMEOUT_MS = 10_000

        /**
         * CHALLENGE bekleme süresi.
         *
         * Kısa tutuldu: eşleşmesiz host'a bağlanırken kullanıcı bu kadar
         * bekler. LAN'da CHALLENGE tek RTT'de gelir, 2 sn fazlasıyla yeterli.
         */
        const val DEFAULT_PAIR_WAIT_MS = 2_000
    }
}

/** Host bağlantıyı açıkça reddetti. */
class WifiRejectedException(
    val reason: Int,
    val detail: String,
) : IOException(
    "host reddetti (reason=0x${reason.toString(16).padStart(2, '0')})" +
        if (detail.isNotEmpty()) ": $detail" else "",
)
