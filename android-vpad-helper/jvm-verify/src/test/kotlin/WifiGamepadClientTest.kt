import com.dfnmondo.gamepad.app.wifi.PairingCrypto
import com.dfnmondo.gamepad.app.wifi.PairingInfo
import com.dfnmondo.gamepad.app.wifi.WifiConnectionState
import com.dfnmondo.gamepad.app.wifi.WifiFrameCodec
import com.dfnmondo.gamepad.app.wifi.WifiGamepadClient
import com.dfnmondo.gamepad.app.wifi.WifiRejectedException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.Collections
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.test.AfterTest
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * Kotlin istemcisinin **gerçek TCP soketi** üzerinden doğrulanması.
 *
 * Karşısındaki test host'u, `host/DAEMON_PATCH.md`'nin `vpad_daemon.py`'ye
 * önerdiği akışın aynısını uygular — Python tarafındaki `TestDaemon` ile
 * eşdeğerdir. Aynı senaryolar iki dilde de koşuyor.
 */
class WifiGamepadClientTest {

    private val hosts = mutableListOf<TestHost>()

    @AfterTest
    fun tearDown() {
        hosts.forEach { it.close() }
        hosts.clear()
    }

    private fun host(token: ByteArray?): TestHost =
        TestHost(token).also { hosts.add(it) }

    // ── Mutlu yol ───────────────────────────────────────────────────

    @Test
    fun `dogru token ile eslesir ve girdi akar`() {
        val token = PairingCrypto.randomToken()
        val h = host(token)
        val states = Collections.synchronizedList(mutableListOf<WifiConnectionState>())
        val client = WifiGamepadClient("Test Telefon") { states.add(it) }

        client.connect(h.info(token), pairWaitMs = 1_000)
        assertTrue(client.paired, "eşleşme gerçekleşmedi")
        assertTrue(client.state is WifiConnectionState.Connected)

        client.sendReport(buttons = 0x0001 or 0x0080, hat = 2, lx = 255, lt = 200)
        assertTrue(h.awaitReports(1), "rapor host'a ulaşmadı")
        client.close()

        assertEquals(listOf("Test Telefon"), h.hellos.toList())
        assertTrue(h.errors.isEmpty(), "host hata gördü: ${h.errors}")

        // Durum akışı: Connecting → Pairing → Connected → Disconnected
        val tags = states.map { it::class.simpleName }
        assertEquals(
            listOf("Connecting", "Pairing", "Connected", "Disconnected"),
            tags,
        )
    }

    @Test
    fun `report baytlari telde HID duzeninde`() {
        val token = PairingCrypto.randomToken()
        val h = host(token)
        val client = WifiGamepadClient("T")

        client.connect(h.info(token), pairWaitMs = 1_000)
        client.sendReport(
            buttons = 0x0001 or 0x0400, hat = 3,
            lx = 1, ly = 2, rx = 3, ry = 4, lt = 5, rt = 6,
        )
        assertTrue(h.awaitReports(1))
        client.close()

        val report = h.reports.first()
        assertEquals(8, report.size)
        assertEquals(0x01, report[0].toInt() and 0xFF)
        assertEquals(0x04 or (3 shl 4), report[1].toInt() and 0xFF)
        assertContentEquals(
            byteArrayOf(1, 2, 3, 4, 5, 6),
            report.copyOfRange(2, 8),
        )
    }

    @Test
    fun `kapanista notr rapor gonderilir`() {
        val token = PairingCrypto.randomToken()
        val h = host(token)
        val client = WifiGamepadClient("T")

        client.connect(h.info(token), pairWaitMs = 1_000)
        client.sendReport(buttons = 0x0001)
        assertTrue(h.awaitReports(1))
        client.close()
        assertTrue(h.awaitReports(2), "kapanışta nötr rapor gitmedi")

        val last = h.reports.last()
        assertEquals(0, last[0].toInt() and 0xFF, "son rapor nötr değil")
        assertEquals(WifiFrameCodec.HAT_CENTER, (last[1].toInt() and 0xFF) ushr 4)
        assertContentEquals(
            byteArrayOf(128.toByte(), 128.toByte(), 128.toByte(), 128.toByte()),
            last.copyOfRange(2, 6),
        )
    }

    @Test
    fun `ping pong calisir`() {
        val token = PairingCrypto.randomToken()
        val h = host(token)
        val client = WifiGamepadClient("T")
        client.connect(h.info(token), pairWaitMs = 1_000)
        client.ping() // PONG gelmezse istisna
        client.close()
        assertTrue(h.errors.isEmpty(), "host hata gördü: ${h.errors}")
    }

    // ── Reddedilme yolları ──────────────────────────────────────────

    @Test
    fun `yanlis token reddedilir`() {
        val hostToken = PairingCrypto.randomToken()
        val phoneToken = PairingCrypto.randomToken()
        val h = host(hostToken)
        val states = Collections.synchronizedList(mutableListOf<WifiConnectionState>())
        val client = WifiGamepadClient("Sahte") { states.add(it) }

        val ex = assertFailsWith<WifiRejectedException> {
            client.connect(h.info(phoneToken), pairWaitMs = 1_000)
        }
        assertEquals(WifiFrameCodec.R_AUTH_FAILED, ex.reason)
        assertTrue(h.reports.isEmpty(), "reddedilen istemci girdi geçirdi")

        val last = states.last()
        assertTrue(last is WifiConnectionState.Rejected)
        assertEquals(WifiFrameCodec.R_AUTH_FAILED, last.reason)
    }

    @Test
    fun `tekrar oynatilan auth reddedilir`() {
        // ASIL GÜVENLİK İDDİASI. İstemci sınıfını atlayıp ham soketle
        // saldırganı canlandırıyoruz.
        val token = PairingCrypto.randomToken()
        val h = host(token)

        // 1) Meşru bağlantı — AUTH gövdesini "dinle"
        val s1 = Socket(InetAddress.getLoopbackAddress(), h.port)
        s1.soTimeout = 3_000
        val challenge1 = s1.readFrameOfType(WifiFrameCodec.T_CHALLENGE)
        val captured = PairingCrypto.buildAuthBody(token, challenge1)
        s1.getOutputStream().writeFrame(WifiFrameCodec.T_AUTH, captured)
        s1.getOutputStream().write(WifiFrameCodec.encodeHello("Mesru"))
        s1.getOutputStream().flush()
        s1.readFrameOfType(WifiFrameCodec.T_HELLO_ACK)
        s1.close()

        // 2) Saldırgan aynı AUTH'u yeni bağlantıda tekrar oynatır
        val s2 = Socket(InetAddress.getLoopbackAddress(), h.port)
        s2.soTimeout = 3_000
        val challenge2 = s2.readFrameOfType(WifiFrameCodec.T_CHALLENGE)
        assertFalse(
            challenge1.contentEquals(challenge2),
            "challenge tekrar kullanıldı — replay açık!",
        )
        s2.getOutputStream().writeFrame(WifiFrameCodec.T_AUTH, captured)
        val reject = s2.readFrame()
        assertEquals(
            WifiFrameCodec.T_REJECT, reject.type,
            "tekrar oynatılan AUTH kabul edildi",
        )
        assertEquals(WifiFrameCodec.R_AUTH_FAILED, reject.payload[0].toInt() and 0xFF)
        s2.close()
    }

    @Test
    fun `auth atlayan istemci reddedilir`() {
        val token = PairingCrypto.randomToken()
        val h = host(token)

        val s = Socket(InetAddress.getLoopbackAddress(), h.port)
        s.soTimeout = 3_000
        s.readFrameOfType(WifiFrameCodec.T_CHALLENGE)
        // CHALLENGE'ı yok say, doğrudan HELLO yolla
        s.getOutputStream().write(WifiFrameCodec.encodeHello("Kurnaz"))
        s.getOutputStream().flush()

        val reject = s.readFrame()
        assertEquals(WifiFrameCodec.T_REJECT, reject.type)
        assertEquals(
            WifiFrameCodec.R_AUTH_REQUIRED,
            reject.payload[0].toInt() and 0xFF,
        )
        s.close()
        assertTrue(h.reports.isEmpty())
    }

    // ── Boşta kalp atışı (GERİLEME TESTİ) ───────────────────────────

    @Test
    fun `bosta kalan baglanti kalp atisiyla canli kalir`() {
        // GERİLEME: host, el sıkışmadan sonra sokete 10 sn okuma zaman aşımı
        // koyuyor ("PING every 2 s when idle"). Kalp atışı olmadan, kullanıcı
        // telefonu bırakıp 10 sn dokunmayınca host bağlantıyı düşürüyordu ve
        // istemci bunu ancak bir sonraki gönderimde öğreniyordu.
        val token = PairingCrypto.randomToken()
        val h = host(token)
        val client = WifiGamepadClient("Bosta Telefon")

        client.connect(h.info(token), pairWaitMs = 1_000, heartbeatMs = 120)
        try {
            // Hiçbir tuşa dokunulmuyor — yalnızca kalp atışı yazmalı.
            assertTrue(
                h.awaitReports(3, timeoutMs = 2_000),
                "boşta kalp atışı gitmedi: ${h.reports.size} rapor geldi",
            )
            // Gelenlerin hepsi nötr olmalı (tuş basılı sanılmasın).
            h.reports.toList().forEach { r ->
                assertEquals(0, r[0].toInt() and 0xFF, "kalp atışı nötr değil")
                assertEquals(
                    WifiFrameCodec.HAT_CENTER,
                    (r[1].toInt() and 0xFF) ushr 4,
                )
            }
        } finally {
            client.close()
        }
    }

    @Test
    fun `kalp atisi gercek trafik akarken sessiz kalir`() {
        val token = PairingCrypto.randomToken()
        val h = host(token)
        val client = WifiGamepadClient("Aktif Telefon")

        // Aralık uzun: bu süre içinde kalp atışı hiç devreye girmemeli.
        client.connect(h.info(token), pairWaitMs = 1_000, heartbeatMs = 5_000)
        try {
            repeat(5) { client.sendReport(buttons = 0x0001) }
            assertTrue(h.awaitReports(5))
            Thread.sleep(200)
            assertEquals(
                5, h.reports.size,
                "kalp atışı trafik akarken fazladan çerçeve yolladı",
            )
        } finally {
            client.close()
        }
    }

    @Test
    fun `kalp atisi kapatilabilir`() {
        val token = PairingCrypto.randomToken()
        val h = host(token)
        val client = WifiGamepadClient("Atissiz")
        client.connect(h.info(token), pairWaitMs = 1_000, heartbeatMs = 0)
        try {
            Thread.sleep(250)
            assertTrue(h.reports.isEmpty(), "heartbeatMs=0 iken çerçeve gitti")
        } finally {
            client.close()
        }
    }

    // ── Yeniden kullanım (GERİLEME TESTİ) ───────────────────────────

    @Test
    fun `ayni nesne yeniden baglanabilir`() {
        // GERİLEME: `decoder` ve `paired` connect() içinde sıfırlanmıyordu.
        // Önceki bağlantıdan kalan yarım çerçeve baytları yeni akışa
        // karışabilir, `paired` bayrağı yanlış kalabilirdi.
        val token = PairingCrypto.randomToken()
        val paired = host(token)
        val open = host(null)
        val client = WifiGamepadClient("Tekrar Kullanilan")

        client.connect(paired.info(token), pairWaitMs = 1_000, heartbeatMs = 0)
        assertTrue(client.paired)
        client.sendReport(buttons = 0x0001)
        assertTrue(paired.awaitReports(1))
        client.close()

        // İkinci bağlantı eşleşmesiz bir host'a: `paired` false'a dönmeli
        client.connect(open.info(PairingCrypto.randomToken()),
            pairWaitMs = 300, heartbeatMs = 0)
        assertFalse(client.paired, "önceki bağlantının paired bayrağı kaldı")
        client.sendReport(buttons = 0x0002)
        assertTrue(open.awaitReports(1))
        client.close()

        assertTrue(paired.errors.isEmpty(), "1. host hata gördü: ${paired.errors}")
        assertTrue(open.errors.isEmpty(), "2. host hata gördü: ${open.errors}")
    }

    // ── Geriye uyum ─────────────────────────────────────────────────

    @Test
    fun `eslesmesiz host ile kilitlenmeden baglanir`() {
        // "QR'ı taradım ama host'u --pair olmadan yeniden başlattım"
        // senaryosu: kilitlenme olmamalı, eşleşmesiz moda düşmeli.
        val h = host(null)
        val client = WifiGamepadClient("Eski Akis")

        client.connect(h.info(PairingCrypto.randomToken()), pairWaitMs = 400)
        assertFalse(client.paired, "eşleşme kapalıyken eşleşti sanıldı")

        client.sendReport(buttons = 0x0002)
        assertTrue(h.awaitReports(1))
        client.close()

        assertEquals(listOf("Eski Akis"), h.hellos.toList())
        assertTrue(h.errors.isEmpty(), "host hata gördü: ${h.errors}")
    }
}

// ── Test host'u ─────────────────────────────────────────────────────

/**
 * Eşleşme kapılı minik host. `DAEMON_PATCH.md`'deki akışın aynısı.
 */
private class TestHost(private val token: ByteArray?) {

    private val server = ServerSocket(0, 4, InetAddress.getLoopbackAddress())
    private val running = AtomicBoolean(true)

    val port: Int get() = server.localPort
    val reports: MutableList<ByteArray> = Collections.synchronizedList(mutableListOf())
    val hellos: MutableList<String> = Collections.synchronizedList(mutableListOf())
    val errors: MutableList<String> = Collections.synchronizedList(mutableListOf())

    init {
        Thread({ acceptLoop() }, "test-host").apply { isDaemon = true }.start()
    }

    fun info(clientToken: ByteArray) =
        PairingInfo("127.0.0.1", port, clientToken)

    fun awaitReports(count: Int, timeoutMs: Long = 3_000): Boolean {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000
        while (System.nanoTime() < deadline) {
            if (reports.size >= count) return true
            Thread.sleep(5)
        }
        return reports.size >= count
    }

    fun close() {
        running.set(false)
        try {
            server.close()
        } catch (_: Exception) {
        }
    }

    private fun acceptLoop() {
        while (running.get()) {
            val conn = try {
                server.accept()
            } catch (_: Exception) {
                return
            }
            Thread({ handle(conn) }, "test-host-conn").apply { isDaemon = true }.start()
        }
    }

    private fun handle(conn: Socket) {
        conn.soTimeout = 5_000
        try {
            conn.use {
                val out = conn.getOutputStream()

                // ── Eşleşme kapısı — yamanın eklediği tek blok ──
                if (token != null) {
                    val challenge = ByteArray(PairingCrypto.CHALLENGE_LEN)
                        .also { b -> java.security.SecureRandom().nextBytes(b) }
                    out.writeFrame(WifiFrameCodec.T_CHALLENGE, challenge)

                    val first = conn.readFrame()
                    if (first.type != WifiFrameCodec.T_AUTH) {
                        out.writeReject(
                            WifiFrameCodec.R_AUTH_REQUIRED,
                            "bu host QR ile eslesme istiyor",
                        )
                        return
                    }
                    if (!PairingCrypto.verifyAuth(token, challenge, first.payload)) {
                        out.writeReject(
                            WifiFrameCodec.R_AUTH_FAILED,
                            "eslesme dogrulamasi basarisiz",
                        )
                        return
                    }
                }

                // ── Mevcut daemon akışı ──
                val hello = conn.readFrame()
                if (hello.type != WifiFrameCodec.T_HELLO) {
                    out.writeReject(WifiFrameCodec.R_INTERNAL_ERROR, "HELLO bekleniyordu")
                    return
                }
                val nameLen = hello.payload[1].toInt() and 0xFF
                hellos.add(String(hello.payload, 2, nameLen, Charsets.UTF_8))
                out.writeFrame(WifiFrameCodec.T_HELLO_ACK, byteArrayOf(1, 1))

                while (true) {
                    val frame = conn.readFrame()
                    when (frame.type) {
                        WifiFrameCodec.T_REPORT -> reports.add(frame.payload)
                        WifiFrameCodec.T_PING ->
                            out.writeFrame(WifiFrameCodec.T_PONG, ByteArray(0))
                        WifiFrameCodec.T_BYE -> return
                    }
                }
            }
        } catch (e: Exception) {
            // Karşı taraf kapattıysa bu normal; başka her şey gerçek hata.
            val message = e.message ?: e.toString()
            if (!message.contains("kapattı") && e !is java.net.SocketException) {
                errors.add("${e::class.simpleName}: $message")
            }
        }
    }
}

// ── Ham soket yardımcıları (istemci sınıfını atlayan testler için) ──

private fun OutputStream.writeFrame(type: Int, payload: ByteArray) {
    write(WifiFrameCodec.encodeFrame(type, payload))
    flush()
}

private fun OutputStream.writeReject(reason: Int, message: String) {
    val body = byteArrayOf(reason.toByte()) + message.toByteArray(Charsets.UTF_8)
    writeFrame(WifiFrameCodec.T_REJECT, body)
}

private fun Socket.readFrame(): WifiFrameCodec.Frame {
    val decoder = decoderFor(this)
    val stream: InputStream = getInputStream()
    val buf = ByteArray(4096)
    while (true) {
        decoder.poll()?.let { return it }
        val read = stream.read(buf)
        if (read < 0) throw IllegalStateException("karsi taraf soketi kapatti")
        if (read > 0) decoder.push(buf, read)
    }
}

private fun Socket.readFrameOfType(expected: Int): ByteArray {
    val frame = readFrame()
    check(frame.type == expected) {
        "0x${expected.toString(16)} bekleniyordu, 0x${frame.type.toString(16)} geldi"
    }
    return frame.payload
}

/**
 * Soket başına tek decoder — tampon bağlantı ömrü boyunca korunmalı, yoksa
 * bölünmüş çerçeveler kaybolur.
 */
private val decoders = java.util.WeakHashMap<Socket, WifiFrameCodec.Decoder>()

@Synchronized
private fun decoderFor(socket: Socket): WifiFrameCodec.Decoder =
    decoders.getOrPut(socket) { WifiFrameCodec.Decoder() }
