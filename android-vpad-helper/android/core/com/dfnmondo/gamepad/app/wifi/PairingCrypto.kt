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
 * İstemci nonce'u karışıma girer. ⚠️ Gerekçesi bir zamanlar "host'un
 * rastgeleliği zayıflarsa istemci kendi entropisini katmış olur" diye
 * yazıyordu — **kriptografik olarak geçersiz**: nonce, MAC ile aynı gövdede
 * telden gidiyor ve host görülmüş nonce kaydı tutmuyor. Host challenge'ı
 * tekrar etmeye başlarsa saldırgan yakaladığı `nonce‖mac` çiftini aynen
 * oynatır ve doğrulama TUTAR. Tazelik yalnız doğrulayanın challenge'ından
 * gelebilir.
 *
 * Nonce'un gerçek ve daha mütevazı değeri: istemci hiçbir zaman tamamen
 * host'un seçtiği bir girdi üzerine kör imza atmıyor.
 */
object PairingCrypto {

    const val CHALLENGE_LEN = 16
    const val NONCE_LEN = 16
    const val MAC_LEN = 32
    const val AUTH_LEN = NONCE_LEN + MAC_LEN

    private const val HMAC_ALGORITHM = "HmacSHA256"

    /** Cihaz anahtarının uzunluğu — `vpad_devices.DEVICE_KEY_LEN`. */
    const val DEVICE_KEY_LEN = 32

    /** `vpad_pairing.AUTH_LABEL` ile birebir aynı bayt dizisi. */
    private val AUTH_LABEL = "vpad-auth-v1".toByteArray(Charsets.US_ASCII)

    /**
     * `vpad_devices.RESUME_LABEL` ile birebir aynı bayt dizisi.
     *
     * **Neden AUTH'tan ayrı bir etiket** (domain separation): iki akış da
     * `HMAC(anahtar, etiket ‖ challenge ‖ nonce)` hesaplıyor. Etiket ortak
     * olsaydı, bir bağlamda üretilmiş geçerli bir imza diğerinde de geçerli
     * olurdu. Bayt farkı, iki imzayı birbirinin yerine kullanılamaz kılıyor.
     */
    private val RESUME_LABEL = "vpad-resume-v1".toByteArray(Charsets.US_ASCII)

    /** `vpad_pairing.CODE_LABEL` ile birebir aynı bayt dizisi. */
    private val CODE_LABEL = "vpad-code-v1".toByteArray(Charsets.US_ASCII)

    /** Elle giriş kodunun hane sayısı — `vpad_pairing.CODE_LEN`. */
    const val CODE_LEN = 6

    /**
     * `SecureRandom` — `kotlin.random.Random` DEĞİL.
     *
     * `kotlin.random.Random` kriptografik değil. (Not: burada bir zamanlar
     * "stdlib uygulaması **xorwow**" yazıyordu — `XorWowRandom` Kotlin/JS ve
     * Native'in, ve `Random(seed)`'in üreteci. **JVM'de** `Random.Default`
     * bir `java.util.Random` sarmalayıcısıdır, yani 48-bit LCG: xorwow'dan
     * bile kolay tersine çevrilir. Sonuç değişmiyor, gerekçe değişiyor.) Nonce tahmin edilebilir olursa
     * challenge-response'un getirdiği güvencenin bir kısmı kaybolur.
     */
    private val secureRandom = SecureRandom()

    /**
     * 6 haneli elle giriş kodunu AUTH'un beklediği 16 baytlık anahtara çevirir.
     *
     * Karşılığı `vpad_pairing.secret_from_code`; **iki taraf birebir aynı
     * baytları üretmek zorunda**, yoksa kod sessizce hep yanlış görünür.
     *
     * Bu kod QR'ın yedeği değil eşdeğeri: Android'in QR tarayıcısı Google
     * Play services'in isteğe bağlı bir modülü ve Play services'i hiç
     * olmayan bir telefon (2019 sonrası Huawei, AOSP türevleri) onu asla
     * indiremiyor. O cihazlar için host'a girmenin başka yolu yok.
     *
     * Kod **dize olarak** işleniyor: `"004321"` sayıya çevrilirse 4321 olur
     * ve host'takiyle asla eşleşmez.
     *
     * Kaba kuvvet koruması burada değil — 10^6 uzayı kapatan şey host'un
     * deneme sayacı (`vpad_devices.Ticket.MAX_CODE_ATTEMPTS`). Saldırgan
     * her denemede host'a bağlanmak zorunda, çevrimdışı deneyemiyor.
     */
    fun secretFromCode(code: String): ByteArray {
        require(code.length == CODE_LEN && code.all { it in '0'..'9' }) {
            "kod $CODE_LEN haneli rakam olmalı"
        }
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(CODE_LABEL + code.toByteArray(Charsets.US_ASCII))
        return digest.copyOf(PairingPayload.TOKEN_LEN)
    }

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

    // ── Kimlik sarmalama (2026-08-19) ────────────────────────────────
    //
    // NEDEN: `CREDENTIAL` (0x15) 32 baytlık kalıcı anahtarı DÜZ METİN
    // taşıyordu, taşıma TLS'siz. WPA2-PSK bir ev ağında Wi-Fi parolasını
    // bilen biri 4-way handshake'i yakalayıp (ya da deauth ile yeniden
    // tetikleyip) o paketi okuyabiliyor — CWE-319. Bu yol artık 0x16
    // ile şifreli gidiyor; 0x15 yalnız 6 haneli kod yolunda ve yayındaki
    // eski istemciler için duruyor.
    //
    // NEDEN AES-GCM DEĞİL: karşı taraf Python ve **stdlib'inde simetrik
    // şifre yok**. `cryptography` eklemek PyInstaller paketine native
    // wheel sokardı; yayındaki exe zaten bir kez Defender'a takılmıştı.
    // Bu yüzden yapı tamamen HMAC-SHA256 üstünde: RFC 5869 HKDF ile
    // anahtar akışı + ayrı anahtarla encrypt-then-MAC.
    //
    // NE KORUMUYOR: 6 haneli kod yolunu (~20 bit; dinleyen çevrimdışı
    // 10^6 dener). Onu kapatmak PAKE ister ve bu turun dışında bırakıldı.
    private val CRED_ENC_LABEL = "vpad-cred-enc-v1".toByteArray(Charsets.US_ASCII)
    private val CRED_MAC_LABEL = "vpad-cred-mac-v1".toByteArray(Charsets.US_ASCII)
    const val CRED_NONCE_LEN = 16
    const val CRED_TAG_LEN = 16

    /**
     * RFC 5869 HKDF (SHA-256). Python karşılığı: `vpad_pairing.hkdf_sha256`.
     *
     * Extract adımında **`salt` HMAC anahtarı, `ikm` mesajdır**; ters
     * yazmak sessizce farklı ama "çalışır görünen" bir çıktı üretir, bu
     * yüzden RFC'nin altın vektörüyle pinlendi.
     */
    fun hkdf(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
        require(length in 1..(255 * 32)) { "hkdf uzunluğu aralık dışı: $length" }
        val extract = Mac.getInstance(HMAC_ALGORITHM)
        extract.init(SecretKeySpec(salt, HMAC_ALGORITHM))
        val prk = extract.doFinal(ikm)

        val out = ByteArray(length)
        var block = ByteArray(0)
        var counter = 1
        var written = 0
        val expand = Mac.getInstance(HMAC_ALGORITHM)
        while (written < length) {
            expand.init(SecretKeySpec(prk, HMAC_ALGORITHM))
            expand.update(block)
            expand.update(info)
            expand.update(counter.toByte())
            block = expand.doFinal()
            val take = minOf(block.size, length - written)
            block.copyInto(out, written, 0, take)
            written += take
            counter++
        }
        return out
    }

    /**
     * Sarmalanmış gövdeyi çözer; etiket tutmazsa `null` — istisna DEĞİL.
     *
     * Bozuk ya da sahte bir çerçeve oturumu düşürmemeli: bu tip el
     * sıkışmanın yan kanalı, [parseCredential]'ın hoşgörüsüyle aynı
     * gerekçe.
     */
    fun unwrapCredential(secret: ByteArray, challenge: ByteArray, body: ByteArray): ByteArray? {
        if (secret.size != PairingPayload.TOKEN_LEN || challenge.size != CHALLENGE_LEN) return null
        if (body.size <= CRED_NONCE_LEN + CRED_TAG_LEN) return null

        val nonce = body.copyOfRange(0, CRED_NONCE_LEN)
        val ciphertext = body.copyOfRange(CRED_NONCE_LEN, body.size - CRED_TAG_LEN)
        val tag = body.copyOfRange(body.size - CRED_TAG_LEN, body.size)

        val salt = challenge + nonce
        val macKey = hkdf(secret, salt, CRED_MAC_LABEL, 32)
        val mac = Mac.getInstance(HMAC_ALGORITHM)
        mac.init(SecretKeySpec(macKey, HMAC_ALGORITHM))
        mac.update(nonce)
        mac.update(ciphertext)
        val expected = mac.doFinal().copyOf(CRED_TAG_LEN)
        // Sabit zamanlı: `MessageDigest.isEqual` erken çıkış yapmıyor.
        if (!MessageDigest.isEqual(tag, expected)) return null

        val keystream = hkdf(secret, salt, CRED_ENC_LABEL, ciphertext.size)
        return ByteArray(ciphertext.size) { (ciphertext[it].toInt() xor keystream[it].toInt()).toByte() }
    }

    /**
     * Gövdeyi sarmalar. Üretimde host'un işi; burada **simetriyi ve
     * testleri** mümkün kılmak için duruyor (`verifyAuth` ile aynı gerekçe).
     */
    fun wrapCredential(
        secret: ByteArray,
        challenge: ByteArray,
        plaintext: ByteArray,
        nonce: ByteArray = ByteArray(CRED_NONCE_LEN).also(secureRandom::nextBytes),
    ): ByteArray {
        require(secret.size == PairingPayload.TOKEN_LEN) { "sır ${PairingPayload.TOKEN_LEN} bayt olmalı" }
        require(challenge.size == CHALLENGE_LEN) { "challenge $CHALLENGE_LEN bayt olmalı" }
        require(nonce.size == CRED_NONCE_LEN) { "nonce $CRED_NONCE_LEN bayt olmalı" }
        require(plaintext.isNotEmpty()) { "boş gövde sarmalanamaz" }

        val salt = challenge + nonce
        val keystream = hkdf(secret, salt, CRED_ENC_LABEL, plaintext.size)
        val macKey = hkdf(secret, salt, CRED_MAC_LABEL, 32)
        val ciphertext = ByteArray(plaintext.size) {
            (plaintext[it].toInt() xor keystream[it].toInt()).toByte()
        }
        val mac = Mac.getInstance(HMAC_ALGORITHM)
        mac.init(SecretKeySpec(macKey, HMAC_ALGORITHM))
        mac.update(nonce)
        mac.update(ciphertext)
        return nonce + ciphertext + mac.doFinal().copyOf(CRED_TAG_LEN)
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

    /** HMAC-SHA256(cihaz anahtarı, RESUME_LABEL ‖ challenge ‖ nonce) → 32 bayt. */
    fun computeResume(
        key: ByteArray,
        challenge: ByteArray,
        nonce: ByteArray,
    ): ByteArray {
        require(key.size == DEVICE_KEY_LEN) {
            "cihaz anahtarı $DEVICE_KEY_LEN bayt olmalı, ${key.size} geldi"
        }
        require(challenge.size == CHALLENGE_LEN) {
            "challenge $CHALLENGE_LEN bayt olmalı, ${challenge.size} geldi"
        }
        require(nonce.size == NONCE_LEN) {
            "nonce $NONCE_LEN bayt olmalı, ${nonce.size} geldi"
        }

        val mac = Mac.getInstance(HMAC_ALGORITHM)
        mac.init(SecretKeySpec(key, HMAC_ALGORITHM))
        mac.update(RESUME_LABEL)
        mac.update(challenge)
        mac.update(nonce)
        return mac.doFinal()
    }

    /**
     * RESUME çerçevesinin gövdesi: `u8 id_len ‖ id(ascii) ‖ nonce(16) ‖ mac(32)`.
     *
     * AUTH'tan farkı kimliğin de taşınması: host, hangi kayda göre
     * doğrulayacağını bilmek zorunda. Kimlik gizli değil.
     *
     * ⚠️ **"Anahtar hiçbir zaman telden geçmiyor" — YANLIŞTI.** Doğrusu:
     * anahtar **RESUME akışında** geçmiyor, yalnız ondan türetilen MAC
     * geçiyor. Ama kayıt anında bir kez, `CREDENTIAL` (0x15) çerçevesinin
     * gövdesinde **düz metin** olarak geçiyor ([parseCredential], hemen
     * aşağıda) ve taşıma TLS'siz.
     *
     * Fark önemli, çünkü bu dosyanın açılış gerekçesi "aynı ağı pasif
     * dinleyen"i hedefliyor: o saldırgan kayıt oturumunu yakalarsa anahtarı
     * alır. Kabul edilen tehdit modeli
     * `docs/wifi-transport-architecture.md` §5.3.
     */
    fun buildResumeBody(
        deviceId: String,
        key: ByteArray,
        challenge: ByteArray,
        nonce: ByteArray = randomNonce(),
    ): ByteArray {
        val raw = deviceId.toByteArray(Charsets.US_ASCII)
        require(raw.size in 1..255) {
            "cihaz kimliği 1..255 bayt olmalı, ${raw.size} geldi"
        }
        val mac = computeResume(key, challenge, nonce)
        return ByteArray(1 + raw.size + NONCE_LEN + MAC_LEN).also {
            it[0] = raw.size.toByte()
            raw.copyInto(it, 1)
            nonce.copyInto(it, 1 + raw.size)
            mac.copyInto(it, 1 + raw.size + NONCE_LEN)
        }
    }

    /**
     * CREDENTIAL gövdesini çözer: `u8 id_len ‖ id(ascii) ‖ key(32)`.
     *
     * Bozuk gövdede `null` — istisna DEĞİL. Bu çerçeve el sıkışmanın yan
     * kanalı: host kayıt biter bitmez gönderiyor ve istemci onu anlamasa da
     * oturum sürmeli. Burada patlamak, çalışan bir bağlantıyı sırf gelecekteki
     * kolaylık uğruna düşürmek olurdu.
     */
    fun parseCredential(body: ByteArray): Pair<String, ByteArray>? {
        if (body.isEmpty()) return null
        val idLen = body[0].toInt() and 0xFF
        if (idLen == 0 || body.size != 1 + idLen + DEVICE_KEY_LEN) return null
        val id = String(body, 1, idLen, Charsets.US_ASCII)
        val key = body.copyOfRange(1 + idLen, body.size)
        return id to key
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
