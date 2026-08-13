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

    /**
     * Host'un atadığı oyuncu indeksi (0..3), yoksa `null`.
     *
     * `null` **tek oyunculu host** demektir — hata değil. Slot'u host atar;
     * istemci kendi numarasını beyan edemez (aksi hâlde iki telefon aynı
     * slotu sahiplenip birbirinin girdisini ezerdi).
     */
    @Volatile
    var playerSlot: Int? = null
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
        playerSlot = null
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
                readUntil(WifiFrameCodec.T_CHALLENGE)
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
                        // Token yanlışsa host REJECT gönderip soketi KAPATIR.
                        // Bu noktada HELLO yazmak, kapanmakta olan sokete
                        // yazmaktır: karşı taraf RST ile cevap verir ve RST
                        // tamponumuzdaki REJECT'i de SİLER — kullanıcı
                        // "yanlış QR" yerine ham bir ağ hatası görür.
                        // O yüzden HELLO'dan ÖNCE bloklamadan bakıyoruz.
                        drainBuffered()?.let { throwRejected(it.payload) }
                    }
                    WifiFrameCodec.T_REJECT -> throwRejected(first.payload)
                    else -> throw WifiProtocolException(
                        "ilk çerçeve CHALLENGE olmalıydı, " +
                            "0x${first.type.toString(16)} geldi",
                    )
                }
            }

            // ── 2) HELLO / HELLO_ACK ──
            val ack = try {
                write(WifiFrameCodec.encodeHello(deviceName))
                readUntil(WifiFrameCodec.T_HELLO_ACK)
            } catch (e: IOException) {
                // Yukarıdaki yarışın kalan payı: REJECT henüz gelmemişken
                // HELLO yazdıysak RST'yi burada yeriz ve REJECT kaybolur.
                // AUTH gönderdikten sonra host'un bağlantıyı kapatmasının
                // pratikteki tek sebebi eşleşmenin reddedilmesidir; ham soket
                // hatası yerine bunu söylemek hem daha doğru hem kullanıcının
                // yapabileceği bir şeye işaret ediyor ("QR'ı yeniden tara").
                if (paired && e !is SocketTimeoutException) {
                    throw WifiRejectedException(
                        WifiFrameCodec.R_AUTH_FAILED,
                        "host AUTH sonrası bağlantıyı kapattı",
                    )
                }
                throw e
            }
            if (ack.type == WifiFrameCodec.T_REJECT) throwRejected(ack.payload)

            // ── 3) Slot (çoklu oyuncu) ──
            // Çoklu oyuncu modundaki host, HELLO_ACK'in hemen ardından
            // T_SLOT yolluyor — pratikte aynı TCP segmentinde gelir ve
            // tamponda hazır bekler. Bu yüzden BLOKLAMADAN alıyoruz: tek
            // oyunculu host hiç göndermediği için beklemek, bağlanan
            // herkese bedava gecikme yazdırırdı.
            //
            // Geç gelirse kayıp değil: bir sonraki okumada [readUntil]
            // onu yan kanal olarak yakalar.
            drainBuffered()

            // Bağlantı kurulur kurulmaz nötr durumdayız. Kalp atışının
            // tekrarlayacak bir şeyi olsun diye başlangıç değeri budur —
            // aksi hâlde bağlanıp hiçbir tuşa dokunmayan kullanıcının
            // bağlantısı yine 10 saniyede düşerdi.
            lastReportFrame = WifiFrameCodec.encodeReport()
            if (heartbeatMs > 0) startHeartbeat(heartbeatMs)
            emit(
                WifiConnectionState.Connected(
                    info.host, info.port, paired, playerSlot,
                ),
            )
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
        // readUntil: araya giren SLOT ya da tanımadığımız bir tip PONG'u
        // kaçırtmaz.
        val frame = readUntil(WifiFrameCodec.T_PONG)
        if (frame.type == WifiFrameCodec.T_REJECT) throwRejected(frame.payload)
    }

    /**
     * Nötr rapor + BYE gönderip soketi kapatır. **Hiçbir istisna sızdırmaz.**
     *
     * Bu söz ciddiye alınmalı: `close()` çoğu zaman bir `finally` bloğundan
     * veya yaşam döngüsü geri çağrısından çağrılır; oradan kaçan bir istisna
     * ya asıl hatayı maskeler ya da (arka plan iş parçacığındaysa) süreci
     * öldürür — bkz. [startHeartbeat].
     */
    fun close() {
        if (socket == null) return
        stopHeartbeat()
        try {
            sendNeutral()
            write(WifiFrameCodec.encodeFrame(WifiFrameCodec.T_BYE))
        } catch (_: IOException) {
            // Kapanışta yazma hatası beklenen bir şey; bastırılıyor.
        } catch (_: WifiProtocolException) {
            // `socket == null` denetimini geçtikten sonra başka bir iş
            // parçacığı kapatmış olabilir (iki kez close, ya da connect
            // hatasının temizliğiyle çakışma). Vedalaşamamak kapanışı
            // engellemez.
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
     *
     * ## Buradan hiçbir istisna kaçamaz — sebebi Android'e özgü
     *
     * `RuntimeInit.commonInit()`, `KillApplicationHandler`'ı
     * `Thread.setDefaultUncaughtExceptionHandler` ile kuruyor: kendi
     * handler'ı olmayan **her** iş parçacığını kapsar ve hangi thread'in
     * attığına bakmadan `Process.killProcess(myPid())` + `System.exit(10)`
     * çağırır. Yani bu döngüden kaçan tek bir istisna, kullanıcı kumandaya
     * dokunmuyorken bile tüm uygulamayı kapatır — üstelik yığın izi
     * kullanıcının yaptığı hiçbir şeyi göstermez.
     *
     * Bu yüzden burada iki katman var: yazma [writeIfOpen] ile yapılır
     * (kapanışla yarışmak istisna üretmez) ve döngü yine de her şeyi
     * yakalar.
     */
    private fun startHeartbeat(intervalMs: Long) {
        stopHeartbeat()
        val thread = Thread({
            try {
                while (!Thread.currentThread().isInterrupted && socket != null) {
                    Thread.sleep(intervalMs / 2)
                    val frame = lastReportFrame ?: continue
                    val idleFor = System.currentTimeMillis() - lastWriteAtMs
                    // [write] DEĞİL: close() `output`u [writeLock] dışında
                    // null'ladığı için, kilidi bekleyip uyandığımızda
                    // bağlantı kapanmış olabilir. Bu bir hata değil, kapanış
                    // sinyali — döngüyü bitir.
                    if (idleFor >= intervalMs && !writeIfOpen(frame)) break
                }
            } catch (_: InterruptedException) {
                // close() istedi; sessizce çık.
            } catch (_: IOException) {
                // Bağlantı zaten kopmuş. Hatayı burada yükseltmenin anlamı
                // yok — çağıranın bir sonraki gönderimi aynı hatayı
                // görecek ve durumu oradan yönetecek.
            } catch (t: Throwable) {
                // SON SAVUNMA HATTI. Buraya düşmek bir hatadır; ama uygulamayı
                // öldürmek (yukarıdaki KDoc) ile sessizce yutmak arasındaki
                // doğru yer, görülebilir biçimde ölmektir. Bu dosya Android
                // API'si kullanmadığı için `Log` yok; `System.err` Android'de
                // logcat'e düşer, JVM testlerinde de görünür.
                System.err.println("vpad: kalp atışı beklenmedik şekilde durdu — $t")
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

    /**
     * Çerçeveyi yazar; bağlantı kapalıysa [WifiProtocolException].
     *
     * Dikkat: o istisna **denetlenmeyen** (RuntimeException), yani
     * `@Throws(IOException::class)` imzasına bakıp yalnızca `IOException`
     * yakalayan bir çağıran onu ıskalar. Çağıran bir arka plan iş
     * parçacığındaysa bu, Android'de sürecin ölümü demektir — bkz.
     * [startHeartbeat]. Kapanmış bağlantıya yazma ihtimali olan yerler
     * [writeIfOpen] kullanmalı.
     */
    @Throws(IOException::class)
    private fun write(bytes: ByteArray) {
        if (!writeIfOpen(bytes)) throw WifiProtocolException("bağlantı yok")
    }

    /**
     * Bağlantı açıksa yazar ve `true`, kapalıysa hiçbir şey yapmadan `false`
     * döner.
     *
     * "Bağlantı yok" durumunu istisna yerine **sonuç** olarak bildirmesi
     * bilinçli: [close] akışında `output` alanı [writeLock] dışında
     * null'lanıyor, dolayısıyla kilidi bekleyen bir yazıcının uyandığında
     * bağlantıyı kapanmış bulması NORMAL bir yarış sonucudur, hata değil.
     *
     * Yazmalar [writeLock] üzerinden serileştirilir: kalp atışı iş parçacığı
     * ile çağıranın yazması araya girerse yarım yazılmış iki çerçeve teli
     * bozar.
     */
    @Throws(IOException::class)
    private fun writeIfOpen(bytes: ByteArray): Boolean {
        synchronized(writeLock) {
            val out = output ?: return false
            out.write(bytes)
            out.flush()
            lastWriteAtMs = System.currentTimeMillis()
            return true
        }
    }

    /**
     * [wanted] tipi (ya da REJECT) gelene kadar okur.
     *
     * Aradaki çerçeveler **yan kanal** sayılır: [WifiFrameCodec.T_SLOT]
     * yakalanır, tanınmayan her tip sessizce atılır. Spec §9 istemcilerden
     * tam olarak bunu istiyor ("old clients just ignore the unknown type")
     * ve bunu yapmamak, host bir gün SLOT veya RUMBLE gönderdiğinde
     * bağlantıyı kırardı — `ping()` "PONG bekleniyordu, 0x14 geldi" diye
     * patlardı.
     */
    @Throws(IOException::class)
    private fun readUntil(wanted: Int): WifiFrameCodec.Frame {
        while (true) {
            val frame = readFrame()
            if (frame.type == wanted || frame.type == WifiFrameCodec.T_REJECT) {
                return frame
            }
            consumeSideChannel(frame)
        }
    }

    /** Yan kanal çerçevesi: tanıyorsak işleriz, tanımıyorsak atarız. */
    private fun consumeSideChannel(frame: WifiFrameCodec.Frame) {
        if (frame.type == WifiFrameCodec.T_SLOT && frame.payload.isNotEmpty()) {
            playerSlot = frame.payload[0].toInt() and 0xFF
        }
    }

    /**
     * **Bloklamadan** hâlihazırda gelmiş çerçeveleri tüketir; ilk REJECT'i
     * döndürür (yoksa `null`).
     *
     * Tamponda bekleyeni ve soket üzerinde okumaya hazır olanı alır; hiçbir
     * şey yoksa anında döner. Henüz gelmemiş bir çerçeve için **beklemez** —
     * beklemek, tek oyunculu host'a bağlanan herkese bedava gecikme
     * yazdırırdı (bkz. [connect] §3).
     */
    private fun drainBuffered(): WifiFrameCodec.Frame? {
        val stream = input ?: return null
        while (true) {
            val buffered = decoder.poll()
            if (buffered != null) {
                if (buffered.type == WifiFrameCodec.T_REJECT) return buffered
                consumeSideChannel(buffered)
                continue
            }
            if (stream.available() <= 0) return null
            val read = stream.read(readBuffer)
            if (read <= 0) return null
            decoder.push(readBuffer, read)
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
