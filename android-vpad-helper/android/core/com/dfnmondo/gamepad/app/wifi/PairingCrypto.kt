package com.dfnmondo.gamepad.app.wifi

import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * Challenge-response eşleşme doğrulaması.
 *
 * Python karşılığı: `host/vpad_pairing.py` → `compute_auth` / `build_auth_body`
 * / `verify_auth`. Aynı altın vektör iki tarafta da test edilir.
 *
 * ## Neden düz token değil
 *
 * En basit tasarım "token'ı ilk mesajda gönder" olurdu. Bunun bilinen zaafı
 * var: aynı ağı pasif dinleyen biri token'ı yakalar ve tekrar kullanır.
 * Challenge-response'ta host her bağlantıda yeni bir challenge üretir; yakalanan
 * cevap başka bir challenge için geçersizdir.
 *
 * ## MAC hesabı
 *
 * ```
 * mac = HMAC-SHA256(token, "vpad-auth-v1" ‖ challenge(16) ‖ nonce(16))
 * ```
 *
 * Sabit etiket alan ayracıdır: aynı token ileride başka bir amaçla
 * kullanılırsa MAC'ler birbirine karışmasın. Sürüm de etiketin içinde, yani
 * şema değişirse eski MAC yeni şemada geçerli olmaz.
 *
 * İstemci nonce'u karışıma girer: host'un rastgeleliği bir gün zayıflarsa
 * (kötü tohumlanmış RNG) istemci kendi entropisini katmış olur.
 */
object PairingCrypto {

    const val CHALLENGE_LEN = 16
    const val NONCE_LEN = 16
    const val MAC_LEN = 32
    const val AUTH_LEN = NONCE_LEN + MAC_LEN

    private const val HMAC_ALGORITHM = "HmacSHA256"

    /** `vpad_pairing.AUTH_LABEL` ile birebir aynı bayt dizisi. */
    private val AUTH_LABEL = "vpad-auth-v1".toByteArray(Charsets.US_ASCII)

    /**
     * `SecureRandom` — `kotlin.random.Random` DEĞİL.
     *
     * `kotlin.random.Random`'ın stdlib uygulaması Marsaglia'nın **xorwow**
     * algoritmasıdır (sınıf adı `XorWowRandom`) ve kriptografik değildir;
     * birkaç çıktıdan iç durumu çıkarılabilir. Nonce tahmin edilebilir olursa
     * challenge-response'un getirdiği güvencenin bir kısmı kaybolur.
     */
    private val secureRandom = SecureRandom()

    /** HMAC-SHA256(token, LABEL ‖ challenge ‖ nonce) → 32 bayt. */
    fun computeAuth(token: ByteArray, challenge: ByteArray, nonce: ByteArray): ByteArray {
        require(token.size == PairingPayload.TOKEN_LEN) {
            "token ${PairingPayload.TOKEN_LEN} bayt olmalı, ${token.size} geldi"
        }
        require(challenge.size == CHALLENGE_LEN) {
            "challenge $CHALLENGE_LEN bayt olmalı, ${challenge.size} geldi"
        }
        require(nonce.size == NONCE_LEN) {
            "nonce $NONCE_LEN bayt olmalı, ${nonce.size} geldi"
        }

        val mac = Mac.getInstance(HMAC_ALGORITHM)
        mac.init(SecretKeySpec(token, HMAC_ALGORITHM))
        mac.update(AUTH_LABEL)
        mac.update(challenge)
        mac.update(nonce)
        return mac.doFinal()
    }

    /** Rastgele nonce üretir. */
    fun randomNonce(): ByteArray = ByteArray(NONCE_LEN).also(secureRandom::nextBytes)

    /** Rastgele token üretir (host tarafı için; testlerde de kullanışlı). */
    fun randomToken(): ByteArray =
        ByteArray(PairingPayload.TOKEN_LEN).also(secureRandom::nextBytes)

    /** AUTH çerçevesinin gövdesi: nonce(16) ‖ mac(32). */
    fun buildAuthBody(
        token: ByteArray,
        challenge: ByteArray,
        nonce: ByteArray = randomNonce(),
    ): ByteArray {
        val mac = computeAuth(token, challenge, nonce)
        return ByteArray(AUTH_LEN).also {
            nonce.copyInto(it, 0)
            mac.copyInto(it, NONCE_LEN)
        }
    }

    /**
     * AUTH gövdesini doğrular. **Sabit zamanlı**; hiçbir durumda istisna atmaz.
     *
     * `MessageDigest.isEqual` JDK'da sabit zamanlı olacak şekilde belgelenmiştir
     * (erken çıkış yapmaz). Elle `==` karşılaştırması ilk farklı baytta dönerdi.
     *
     * Bu fonksiyon istemcide zorunlu değil (doğrulamayı host yapar) ama
     * simetriyi ve testleri mümkün kılmak için burada.
     */
    fun verifyAuth(token: ByteArray, challenge: ByteArray, body: ByteArray): Boolean {
        // Uzunluk denetimi bir zamanlama sızıntısı değil: uzunluk zaten telde
        // açıkça görünüyor.
        if (body.size != AUTH_LEN) return false
        val nonce = body.copyOfRange(0, NONCE_LEN)
        val mac = body.copyOfRange(NONCE_LEN, AUTH_LEN)
        val expected = try {
            computeAuth(token, challenge, nonce)
        } catch (_: IllegalArgumentException) {
            return false
        }
        return MessageDigest.isEqual(mac, expected)
    }
}
