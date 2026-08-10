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
 * [connect] ve [sendReport] **bloklar**; çağıranın bunları ana iş
 * parçacığından ÇAĞIRMAMASI gerekir (Android'de `NetworkOnMainThreadException`
 * alırsınız — bu bilinçli, sessiz ANR yerine gürültülü hata). Entegrasyonda
 * bir coroutine veya `HandlerThread` üzerinden sürülür.
 *
 * Tüm yazmalar [writeLock] üzerinden serileştirilir, yani [close] ve
 * [sendReport] farklı iş parçacıklarından çağrılsa bile tel bozulmaz. Okuma
 * hâlâ tek iş parçacıklıdır: [ping] ile [connect] aynı anda çalıştırılmamalı.
 */
class WifiGamepadClient(
    private val deviceName: String,
    private val listener: (WifiConnectionState) -> Unit = {},
) {

    @Volatile
    private var socket: Socket? = null
    private var input: InputStream? = null
    private var output: OutputStream? = null

    /**
     * Her bağlantı için YENİ çözücü.
     *
     * `val` değil `var`: aynı nesne yeniden bağlanırsa önceki bağlantıdan
     * kalan yarım çerçeve baytları yeni akışa karışırdı.
     */
    private var decoder = WifiFrameCodec.Decoder()
    private val readBuffer = ByteArray(4096)

    /** Yazmaları serileştirir — bkz. sınıf KDoc'undaki iş parçacığı sözleşmesi. */
    private val writeLock = Any()

    @Volatile
    private var lastWriteAtMs: Long = 0L

    /** Kalp atışının tekrarlayacağı son rapor. */
    @Volatile
    private var lastReportFrame: ByteArray? = null

    @Volatile
    private var heartbeat: Thread? = null

    /** Eşleşme kapısından geçildi mi (false = eşleşmesiz mod). */
    @Volatile
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
     * @param heartbeatMs boşta kalp atışı aralığı; 0 → kapalı (bkz. [sendReport]).
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
        heartbeatMs: Long = DEFAULT_HEARTBEAT_MS,
    ) {
        // Aynı nesne yeniden kullanılabilsin: önceki bağlantının artıkları
        // yeni akışa sızmamalı.
        decoder = WifiFrameCodec.Decoder()
        paired = false
        lastReportFrame = null

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

            // Bağlantı kurulur kurulmaz nötr durumdayız. Kalp atışının
            // tekrarlayacak bir şeyi olsun diye başlangıç değeri budur —
            // aksi hâlde bağlanıp hiçbir tuşa dokunmayan kullanıcının
            // bağlantısı yine 10 saniyede düşerdi.
            lastReportFrame = WifiFrameCodec.encodeReport()
            if (heartbeatMs > 0) startHeartbeat(heartbeatMs)
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
        val frame = WifiFrameCodec.encodeReport(buttons, hat, lx, ly, rx, ry, lt, rt)
        lastReportFrame = frame
        write(frame)
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

    /**
     * PING gönderir ve PONG bekler.
     *
     * Boşta kalp atışı için bunu KULLANMAYIN — her PING bir PONG üretir ve
     * onu okuyan olmazsa alım tamponunda birikir. Kalp atışı bunun yerine son
     * raporu tekrarlar; bkz. [startHeartbeat].
     */
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
        stopHeartbeat()
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

    // ── Kalp atışı ──────────────────────────────────────────────────

    /**
     * Boşta kalan bağlantıyı canlı tutar.
     *
     * **Neden gerekli:** host, el sıkışmadan sonra sokete 10 saniyelik okuma
     * zaman aşımı koyuyor (`vpad_daemon.py`: "PING every 2 s when idle").
     * Kullanıcı telefonu bırakıp 10 saniye hiçbir tuşa dokunmazsa host
     * bağlantıyı düşürür ve bunu istemci ancak bir sonraki gönderimde,
     * `ConnectionAbortedError` ile öğrenir. Yani "oyunu duraklattım, geri
     * döndüm, kumanda ölmüş" hatası.
     *
     * **Neden PING değil de son raporun tekrarı:** her PING bir PONG üretir;
     * kalp atışı iş parçacığı okuma yapmadığı için o PONG'lar alım tamponunda
     * birikirdi. Raporun tekrarı yanıt istemez, protokole birebir uygundur ve
     * host'un durum görüntüsünü de tazeler. Bluetooth tarafındaki
     * `MaxConnectionModeController` keepalive'ı da aynı deseni kullanıyor.
     *
     * Trafik akarken sessizdir: son yazmanın üzerinden [intervalMs] geçmediyse
     * hiçbir şey göndermez.
     */
    private fun startHeartbeat(intervalMs: Long) {
        stopHeartbeat()
        val thread = Thread({
            try {
                while (!Thread.currentThread().isInterrupted && socket != null) {
                    Thread.sleep(intervalMs / 2)
                    val frame = lastReportFrame ?: continue
                    val idleFor = System.currentTimeMillis() - lastWriteAtMs
                    if (idleFor >= intervalMs) {
                        write(frame)
                    }
                }
            } catch (_: InterruptedException) {
                // close() istedi; sessizce çık.
            } catch (_: IOException) {
                // Bağlantı zaten kopmuş. Hatayı burada yükseltmenin anlamı
                // yok — çağıranın bir sonraki gönderimi aynı hatayı
                // görecek ve durumu oradan yönetecek.
            }
        }, "vpad-wifi-heartbeat")
        thread.isDaemon = true
        heartbeat = thread
        thread.start()
    }

    private fun stopHeartbeat() {
        heartbeat?.interrupt()
        heartbeat = null
    }

    // ── İç yardımcılar ──────────────────────────────────────────────

    private fun closeQuietly() {
        stopHeartbeat()
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
        // Kalp atışı iş parçacığı ile çağıranın yazması araya girmemeli:
        // yarım yazılmış iki çerçeve teli bozar.
        synchronized(writeLock) {
            val out = output ?: throw WifiProtocolException("bağlantı yok")
            out.write(bytes)
            out.flush()
            lastWriteAtMs = System.currentTimeMillis()
        }
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
         * Host boştayken 2 sn'de bir trafik beklendiği için 10 sn hem onu hem
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

        /**
         * Boşta kalp atışı aralığı.
         *
         * Host'un 10 sn'lik zaman aşımına karşı 2 sn: bir atış kaybolsa bile
         * dört şans daha var.
         */
        const val DEFAULT_HEARTBEAT_MS = 2_000L
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
