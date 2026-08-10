import com.dfnmondo.gamepad.app.wifi.WifiFrameCodec
import com.dfnmondo.gamepad.app.wifi.WifiProtocolException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class WifiFrameCodecTest {

    // ── Çerçeve başlığı ─────────────────────────────────────────────

    @Test
    fun `cerceve basligi daemon bicimiyle ayni`() {
        val frame = WifiFrameCodec.encodeFrame(0x02, "12345678".toByteArray())
        assertEquals(11, frame[0].toInt() and 0xFF)   // 3 + 8, düşük bayt
        assertEquals(0, frame[1].toInt() and 0xFF)    // yüksek bayt
        assertEquals(0x02, frame[2].toInt() and 0xFF)
        assertEquals("12345678", String(frame, 3, 8))
    }

    @Test
    fun `bos govdeli cerceve 3 bayt`() {
        val frame = WifiFrameCodec.encodeFrame(WifiFrameCodec.T_PING)
        assertEquals(3, frame.size)
        assertEquals(3, frame[0].toInt() and 0xFF)
    }

    @Test
    fun `asiri buyuk cerceve reddedilir`() {
        assertFailsWith<IllegalArgumentException> {
            WifiFrameCodec.encodeFrame(0x02, ByteArray(WifiFrameCodec.MAX_FRAME))
        }
    }

    // ── REPORT düzeni — HidReportSender ile birebir ─────────────────

    @Test
    fun `report baytlari HID duzeniyle ayni`() {
        // host/test_e2e_pairing.py → test_report_bytes_match_hid_layout
        // ile AYNI vaka: A | HOME, hat=3, eksenler 1..6
        val a = 0x0001
        val home = 0x0400
        val frame = WifiFrameCodec.encodeReport(
            buttons = a or home, hat = 3,
            lx = 1, ly = 2, rx = 3, ry = 4, lt = 5, rt = 6,
        )
        assertEquals(11, frame.size) // 3 başlık + 8 gövde
        val body = frame.copyOfRange(3, 11)

        assertEquals(0x01, body[0].toInt() and 0xFF, "A düşük baytta olmalı")
        // HOME yüksek baytın b2'si (0x04), hat üst nibble'da
        assertEquals(0x04 or (3 shl 4), body[1].toInt() and 0xFF)
        assertEquals(3, (body[1].toInt() and 0xFF) ushr 4, "hat nibble'ı yanlış yerde")
        assertEquals(listOf(1, 2, 3, 4, 5, 6), body.drop(2).map { it.toInt() and 0xFF })
    }

    @Test
    fun `notr report merkezde dinlenir`() {
        val body = WifiFrameCodec.encodeReport().copyOfRange(3, 11)
        assertEquals(0, body[0].toInt() and 0xFF)
        assertEquals(WifiFrameCodec.HAT_CENTER, (body[1].toInt() and 0xFF) ushr 4)
        assertEquals(
            listOf(128, 128, 128, 128, 0, 0),
            body.drop(2).map { it.toInt() and 0xFF },
        )
    }

    @Test
    fun `gecersiz hat merkeze cekilir`() {
        listOf(-1, 8, 9, 15, 99).forEach { hat ->
            val body = WifiFrameCodec.encodeReport(hat = hat).copyOfRange(3, 11)
            assertEquals(
                WifiFrameCodec.HAT_CENTER,
                (body[1].toInt() and 0xFF) ushr 4,
                "hat=$hat merkeze çekilmedi",
            )
        }
    }

    @Test
    fun `eksenler araliga kirpilir`() {
        val body = WifiFrameCodec.encodeReport(
            lx = -100, ly = 999, rx = 256, ry = -1, lt = 300, rt = -5,
        ).copyOfRange(3, 11)
        assertEquals(
            listOf(0, 255, 255, 0, 255, 0),
            body.drop(2).map { it.toInt() and 0xFF },
        )
    }

    @Test
    fun `yuksek butonlarin yalniz uc biti gecer`() {
        // Yüksek baytta yalnız L3/R3/Home var; kalan bitler hat nibble'ına
        // taşmamalı.
        val body = WifiFrameCodec.encodeReport(buttons = 0xFFFF, hat = 0)
            .copyOfRange(3, 11)
        assertEquals(0xFF, body[0].toInt() and 0xFF)
        assertEquals(0x07, body[1].toInt() and 0x0F, "yüksek bitler sızdı")
        assertEquals(0, (body[1].toInt() and 0xFF) ushr 4)
    }

    // ── HELLO ───────────────────────────────────────────────────────

    @Test
    fun `hello duzeni daemon decode_hello ile uyumlu`() {
        val frame = WifiFrameCodec.encodeHello("Pixel 8", "samurai")
        val body = frame.copyOfRange(3, frame.size)
        assertEquals(WifiFrameCodec.PROTO_VER, body[0].toInt())
        val nameLen = body[1].toInt()
        assertEquals(7, nameLen)
        assertEquals("Pixel 8", String(body, 2, nameLen))
        val skinLen = body[2 + nameLen].toInt()
        assertEquals(7, skinLen)
        assertEquals("samurai", String(body, 3 + nameLen, skinLen))
    }

    @Test
    fun `uzun ad karakter sinirinda kirpilir`() {
        // "ğ" UTF-8'de 2 bayt: ham 255'e kırpmak son karakteri ortadan böler.
        val name = "ğ".repeat(200) // 400 bayt
        val frame = WifiFrameCodec.encodeHello(name)
        val body = frame.copyOfRange(3, frame.size)
        val nameLen = body[1].toInt() and 0xFF
        assertTrue(nameLen <= 255)
        val decoded = String(body, 2, nameLen, Charsets.UTF_8)
        assertFalse(
            decoded.contains('�'),
            "ad çok baytlı karakterin ortasından kesilmiş",
        )
        assertEquals(nameLen, decoded.toByteArray(Charsets.UTF_8).size)
    }

    // ── Decoder ─────────────────────────────────────────────────────

    @Test
    fun `parcali gelen cerceve birlestirilir`() {
        val frame = WifiFrameCodec.encodeReport(buttons = 1)
        val decoder = WifiFrameCodec.Decoder()

        // Bayt bayt besle — son bayta kadar hiçbir çerçeve çıkmamalı
        for (i in frame.indices) {
            decoder.push(byteArrayOf(frame[i]))
            if (i < frame.size - 1) {
                assertNull(decoder.poll(), "erken çerçeve üretildi (i=$i)")
            }
        }
        val out = assertNotNull(decoder.poll())
        assertEquals(WifiFrameCodec.T_REPORT, out.type)
        assertEquals(8, out.payload.size)
        assertNull(decoder.poll())
    }

    @Test
    fun `tek okumada gelen coklu cerceve tek tek verilir`() {
        val a = WifiFrameCodec.encodeReport(buttons = 1)
        val b = WifiFrameCodec.encodeFrame(WifiFrameCodec.T_PING)
        val c = WifiFrameCodec.encodeReport(buttons = 2)
        val blob = a + b + c

        val decoder = WifiFrameCodec.Decoder()
        decoder.push(blob)

        assertEquals(WifiFrameCodec.T_REPORT, assertNotNull(decoder.poll()).type)
        assertEquals(WifiFrameCodec.T_PING, assertNotNull(decoder.poll()).type)
        val third = assertNotNull(decoder.poll())
        assertEquals(WifiFrameCodec.T_REPORT, third.type)
        assertEquals(2, third.payload[0].toInt())
        assertNull(decoder.poll())
        assertEquals(0, decoder.pending)
    }

    @Test
    fun `yarim kalan sonraki pushta tamamlanir`() {
        val a = WifiFrameCodec.encodeReport(buttons = 1)
        val b = WifiFrameCodec.encodeReport(buttons = 2)
        val blob = a + b

        val decoder = WifiFrameCodec.Decoder()
        decoder.push(blob, a.size + 4) // ikinci çerçevenin yalnız bir kısmı
        assertNotNull(decoder.poll())
        assertNull(decoder.poll())

        decoder.push(blob.copyOfRange(a.size + 4, blob.size))
        assertEquals(2, assertNotNull(decoder.poll()).payload[0].toInt())
    }

    @Test
    fun `asiri buyuk uzunluk alani reddedilir`() {
        val decoder = WifiFrameCodec.Decoder()
        // uzunluk = 5000 > MAX_FRAME
        decoder.push(byteArrayOf(0x88.toByte(), 0x13, 0x02))
        assertFailsWith<WifiProtocolException> { decoder.poll() }
    }

    @Test
    fun `eksik boyutlu uzunluk alani reddedilir`() {
        val decoder = WifiFrameCodec.Decoder()
        decoder.push(byteArrayOf(0x02, 0x00, 0x02)) // uzunluk 2 < 3
        assertFailsWith<WifiProtocolException> { decoder.poll() }
    }

    @Test
    fun `buyuk yigin tamponu buyutur`() {
        val decoder = WifiFrameCodec.Decoder()
        val one = WifiFrameCodec.encodeReport()
        val many = ByteArray(one.size * 800) { one[it % one.size] }
        decoder.push(many)
        var count = 0
        while (decoder.poll() != null) count++
        assertEquals(800, count)
        assertEquals(0, decoder.pending)
    }
}
