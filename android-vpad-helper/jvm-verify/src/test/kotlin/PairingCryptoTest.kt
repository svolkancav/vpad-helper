import com.dfnmondo.gamepad.app.wifi.PairingCrypto
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * **Protokol sözleşmesi testi.**
 *
 * Buradaki altın vektör, Python tarafındaki
 * `test_vpad_pairing.GOLDEN_MAC_HEX` ile birebir aynı olmak ZORUNDA. İki ayrı
 * dilde yazılmış iki uygulamanın aynı teli konuştuğunun kanıtı bu tek sabit.
 *
 * Değer değişirse protokol kırılmış demektir; o zaman iki taraf birlikte
 * güncellenmeli. Python tarafında yeniden üretmek için:
 *
 *     python host/test_vpad_pairing.py --print-golden
 */
class PairingCryptoTest {

    private val token = ByteArray(16) { it.toByte() }
    private val challenge = ByteArray(16) { (100 + it).toByte() }
    private val nonce = ByteArray(16) { (200 + it).toByte() }

    private companion object {
        /** `python host/test_vpad_pairing.py --print-golden` çıktısı. */
        const val GOLDEN_MAC_HEX =
            "ef9831a78ba47467094297a247929b5a4c98ba105cbd363dd18a61cd994e64df"
    }

    private fun ByteArray.hex(): String =
        joinToString("") { "%02x".format(it) }

    @Test
    fun `altin vektor Python ile ayni`() {
        val mac = PairingCrypto.computeAuth(token, challenge, nonce)
        assertEquals(
            GOLDEN_MAC_HEX,
            mac.hex(),
            "MAC Python tarafıyla uyuşmuyor — protokol kırık",
        )
    }

    @Test
    fun `mac deterministik ve 32 bayt`() {
        val a = PairingCrypto.computeAuth(token, challenge, nonce)
        val b = PairingCrypto.computeAuth(token, challenge, nonce)
        assertTrue(a.contentEquals(b))
        assertEquals(PairingCrypto.MAC_LEN, a.size)
    }

    @Test
    fun `dogru cevap kabul edilir`() {
        val body = PairingCrypto.buildAuthBody(token, challenge, nonce)
        assertEquals(PairingCrypto.AUTH_LEN, body.size)
        assertTrue(PairingCrypto.verifyAuth(token, challenge, body))
    }

    @Test
    fun `yanlis token reddedilir`() {
        val body = PairingCrypto.buildAuthBody(token, challenge, nonce)
        assertFalse(PairingCrypto.verifyAuth(ByteArray(16), challenge, body))
    }

    @Test
    fun `tekrar oynatma reddedilir`() {
        // ASIL GÜVENLİK İDDİASI: yakalanan AUTH başka bir challenge'da geçersiz.
        val body = PairingCrypto.buildAuthBody(token, challenge, nonce)
        val freshChallenge = ByteArray(16) { (it * 7 + 3).toByte() }
        assertFalse(freshChallenge.contentEquals(challenge))
        assertFalse(PairingCrypto.verifyAuth(token, freshChallenge, body))
    }

    @Test
    fun `oynanmis mac reddedilir`() {
        val body = PairingCrypto.buildAuthBody(token, challenge, nonce)
        body[body.size - 1] = (body[body.size - 1].toInt() xor 0x01).toByte()
        assertFalse(PairingCrypto.verifyAuth(token, challenge, body))
    }

    @Test
    fun `oynanmis nonce reddedilir`() {
        val body = PairingCrypto.buildAuthBody(token, challenge, nonce)
        body[0] = (body[0].toInt() xor 0x01).toByte()
        assertFalse(PairingCrypto.verifyAuth(token, challenge, body))
    }

    @Test
    fun `bozuk uzunluklar istisna atmadan reddedilir`() {
        listOf(0, 1, 47, 49, 1000).forEach { len ->
            assertFalse(
                PairingCrypto.verifyAuth(token, challenge, ByteArray(len)),
                "$len baytlık gövde kabul edildi",
            )
        }
    }

    @Test
    fun `yanlis boyutlu girdiler reddedilir`() {
        assertFailsWith<IllegalArgumentException> {
            PairingCrypto.computeAuth(ByteArray(15), challenge, nonce)
        }
        assertFailsWith<IllegalArgumentException> {
            PairingCrypto.computeAuth(token, ByteArray(15), nonce)
        }
        assertFailsWith<IllegalArgumentException> {
            PairingCrypto.computeAuth(token, challenge, ByteArray(15))
        }
    }

    @Test
    fun `nonce her cagrida taze`() {
        val seen = (1..200).map { PairingCrypto.randomNonce().hex() }.toSet()
        assertEquals(200, seen.size, "nonce tekrar ediyor")
        assertEquals(PairingCrypto.NONCE_LEN, PairingCrypto.randomNonce().size)
    }

    @Test
    fun `rastgele nonce ile uretilen cevap dogrulanir`() {
        repeat(20) {
            val body = PairingCrypto.buildAuthBody(token, challenge)
            assertTrue(PairingCrypto.verifyAuth(token, challenge, body))
        }
    }
}
