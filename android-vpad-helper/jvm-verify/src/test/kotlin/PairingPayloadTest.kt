import com.dfnmondo.gamepad.app.wifi.PairingException
import com.dfnmondo.gamepad.app.wifi.PairingPayload
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * `host/test_vpad_pairing.py` içindeki `LanRangeTests` ve `PayloadTests`
 * ile AYNI vakalar. İki taraf aynı şeyi reddetmezse protokol tutarsızdır.
 */
class PairingPayloadTest {

    private val token = ByteArray(16) { it.toByte() }
    private val tokenHex = "000102030405060708090a0b0c0d0e0f"

    // ── LAN aralıkları ──────────────────────────────────────────────

    @Test
    fun `LAN adresleri kabul edilir`() {
        listOf(
            "127.0.0.1", "127.255.255.254",
            "10.0.0.0", "10.255.255.255",
            "172.16.0.0", "172.31.255.255",
            "192.168.0.0", "192.168.255.255",
            "169.254.0.1", "169.254.255.254",
            "100.64.0.0", "100.127.255.255",
        ).forEach {
            assertTrue(PairingPayload.isLanIpv4(it), "$it LAN sayılmalıydı")
        }
    }

    @Test
    fun `genel adresler reddedilir`() {
        listOf(
            "8.8.8.8", "1.1.1.1", "93.184.216.34",
            "172.15.255.255",  // 172.16/12'nin HEMEN ALTI
            "172.32.0.0",      // 172.16/12'nin HEMEN ÜSTÜ
            "100.63.255.255",  // 100.64/10'un hemen altı
            "100.128.0.0",     // 100.64/10'un hemen üstü
            "11.0.0.1",
            "192.169.0.1", "192.167.255.255",
            "169.253.255.255", "169.255.0.0",
            "0.0.0.0", "255.255.255.255",
        ).forEach {
            assertFalse(PairingPayload.isLanIpv4(it), "$it reddedilmeliydi")
        }
    }

    @Test
    fun `IPv4 literal olmayanlar reddedilir`() {
        listOf(
            "example.com", "localhost", "vpad.local",
            "::1", "fe80::1", "[::1]",
            "", "   ", "192.168.1", "192.168.1.256", "192.168.1.1.1",
            "0x7f000001", "2130706433",
            "192.168.01.1",   // baştaki sıfır
            "192.168.1.-1", "192.168. 1.1",
        ).forEach {
            assertFalse(PairingPayload.isLanIpv4(it), "'$it' reddedilmeliydi")
        }
    }

    @Test
    fun `unicode rakamlar reddedilir`() {
        // Char.isDigit() bunları kabul ederdi; ASCII kontrolü şart.
        assertFalse(PairingPayload.isLanIpv4("١٩٢.١٦٨.١.١"))
    }

    // ── payload ─────────────────────────────────────────────────────

    @Test
    fun `kur ve ayristir gidis donus`() {
        val text = PairingPayload.build("192.168.1.34", 53124, token)
        assertEquals("vpad://192.168.1.34:53124?t=$tokenHex&v=1", text)

        val info = PairingPayload.parse(text)
        assertEquals("192.168.1.34", info.host)
        assertEquals(53124, info.port)
        assertEquals(tokenHex, info.tokenHex)
        assertTrue(token.contentEquals(info.token))
    }

    @Test
    fun `dusmanca payloadlar reddedilir`() {
        val cases = mapOf(
            "boş" to "",
            "yanlış şema" to "http://192.168.1.1:80?t=$tokenHex&v=1",
            "şemasız" to "192.168.1.1:80?t=$tokenHex&v=1",
            "netpad şeması" to "netpad://192.168.1.1:80?t=$tokenHex&v=1",
            "sorgu yok" to "vpad://192.168.1.1:80",
            "port yok" to "vpad://192.168.1.1?t=$tokenHex&v=1",
            "port boş" to "vpad://192.168.1.1:?t=$tokenHex&v=1",
            "port sayı değil" to "vpad://192.168.1.1:abc?t=$tokenHex&v=1",
            "port işaretli" to "vpad://192.168.1.1:+80?t=$tokenHex&v=1",
            "port sıfır" to "vpad://192.168.1.1:0?t=$tokenHex&v=1",
            "port aşkın" to "vpad://192.168.1.1:65536?t=$tokenHex&v=1",
            "adres boş" to "vpad://:80?t=$tokenHex&v=1",
            "genel adres" to "vpad://8.8.8.8:80?t=$tokenHex&v=1",
            "alan adı" to "vpad://evil.example.com:80?t=$tokenHex&v=1",
            "token yok" to "vpad://192.168.1.1:80?v=1",
            "token kısa" to "vpad://192.168.1.1:80?t=abcd&v=1",
            "token hex değil" to "vpad://192.168.1.1:80?t=${"z".repeat(32)}&v=1",
            "sürüm yok" to "vpad://192.168.1.1:80?t=$tokenHex",
            "sürüm yanlış" to "vpad://192.168.1.1:80?t=$tokenHex&v=2",
            "sürüm sayı değil" to "vpad://192.168.1.1:80?t=$tokenHex&v=x",
            "biçimsiz sorgu" to "vpad://192.168.1.1:80?t=$tokenHex&v=1&bozuk",
            "yinelenen anahtar" to
                "vpad://192.168.1.1:80?t=$tokenHex&t=${"0".repeat(32)}&v=1",
        )
        for ((name, payload) in cases) {
            assertFailsWith<PairingException>("'$name' kabul edildi") {
                PairingPayload.parse(payload)
            }
        }
    }

    @Test
    fun `bilinmeyen ek parametre tolere edilir`() {
        val info = PairingPayload.parse(
            "vpad://10.0.0.5:9000?t=$tokenHex&v=1&name=Studio",
        )
        assertEquals("10.0.0.5", info.host)
        assertEquals(9000, info.port)
    }

    @Test
    fun `bastaki ve sondaki bosluk kirpilir`() {
        val info = PairingPayload.parse("  vpad://10.0.0.5:9000?t=$tokenHex&v=1\n")
        assertEquals(9000, info.port)
    }

    @Test
    fun `token gizli kalir loglarda`() {
        val info = PairingPayload.parse("vpad://10.0.0.5:9000?t=$tokenHex&v=1")
        val text = info.toString()
        assertFalse(text.contains(tokenHex), "toString token'ın tamamını sızdırıyor")
        assertTrue(text.contains("10.0.0.5"))
    }

    @Test
    fun `LAN disi adres yayinlanamaz`() {
        assertFailsWith<IllegalArgumentException> {
            PairingPayload.build("8.8.8.8", 8765, token)
        }
        assertFailsWith<IllegalArgumentException> {
            PairingPayload.build("192.168.1.1", 0, token)
        }
        assertFailsWith<IllegalArgumentException> {
            PairingPayload.build("192.168.1.1", 65536, token)
        }
    }
}
