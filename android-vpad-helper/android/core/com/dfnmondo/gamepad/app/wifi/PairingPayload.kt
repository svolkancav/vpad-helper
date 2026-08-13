package com.dfnmondo.gamepad.app.wifi

/**
 * QR eşleşme payload'ının ayrıştırılması ve doğrulanması.
 *
 * Python karşılığı: `host/vpad_pairing.py` → `parse_payload` / `build_payload`
 * / `is_lan_ipv4`. İki taraf **aynı vakalarla** test edilir; birinde bir kural
 * değişirse diğeri de değişmek zorundadır.
 *
 * Bu dosya **hiçbir Android API'si kullanmaz** — yalnızca Kotlin stdlib. Sebep:
 * eşleşmenin en güvenlik-kritik parçası olan bu kod, cihaz veya emülatör
 * olmadan JVM'de derlenip test edilebilsin.
 *
 * ## Tehdit modeli
 *
 * Bu sınıf **düşmanca girdi** alır: kullanıcı herhangi bir QR kodunu tarayabilir.
 * Her adım açıkça reddeder; hiçbir yerde "makul varsayım" yapılmaz. Özellikle:
 *
 *  - **Alan adı kabul edilmez.** Yalnızca IPv4 literal. Bir alan adına izin
 *    vermek, kötü niyetli bir QR'ın telefonu internetteki bir sunucuya
 *    yönlendirmesine ve DNS rebinding ile LAN kontrolünün baypas edilmesine
 *    kapı açardı.
 *  - **LAN dışı adres kabul edilmez.** `vpad://8.8.8.8:80?...` taransa bile
 *    bağlanılmaz.
 */
object PairingPayload {

    const val SCHEME = "vpad://"
    const val PAYLOAD_VERSION = 1
    const val TOKEN_LEN = 16

    /** Bir sekizli grubun üst sınırı dahil aralığı. */
    private data class LanRange(
        val first: Int,
        val secondFrom: Int = 0,
        val secondTo: Int = 255,
    )

    /**
     * `vpad_pairing.LAN_RANGES` ile BİREBİR aynı liste.
     *
     * `127/8` loopback, `10/8` · `172.16/12` · `192.168/16` RFC1918,
     * `169.254/16` link-local, `100.64/10` operatör NAT.
     */
    private val LAN_RANGES = listOf(
        LanRange(127),
        LanRange(10),
        LanRange(172, 16, 31),
        LanRange(192, 168, 168),
        LanRange(169, 254, 254),
        LanRange(100, 64, 127),
    )

    /**
     * Adres LAN aralıklarından birinde mi?
     *
     * Ayrıştırma bilinçli olarak KATI ve elle yazılmış:
     * `InetAddress.getByName` **DNS çözümlemesi yapar** — tam olarak kapatmak
     * istediğimiz kapı o. Ayrıca baştaki sıfırı olan sekizliler (`192.168.01.1`)
     * bazı kütüphanelerde sekizlik sayı olarak yorumlanır; burada doğrudan
     * reddediliyor.
     *
     * Python tarafı da reddeder — **ama yalnızca 3.9.5 / 3.8.12'den itibaren.**
     * O sürümlerden önce `ipaddress` baştaki sıfırları kırpıp adresi kabul
     * ediyordu (CVE-2021-29921); belgedeki not aynen şöyle: *"Changed in
     * version 3.9.5: Leading zeros are no longer tolerated and are treated as
     * an error."* Daemon daha eski bir Python'da koşarsa iki taraf aynı
     * payload'a farklı cevap verir — pratikte host kendi adresini üretip
     * yayınladığı için tetiklenmesi zor, ama parite iddiası o eşiğe bağlı.
     */
    fun isLanIpv4(text: String): Boolean {
        val octets = parseIpv4(text) ?: return false
        return LAN_RANGES.any { range ->
            octets[0] == range.first && octets[1] in range.secondFrom..range.secondTo
        }
    }

    /** Katı IPv4 ayrıştırıcı. Geçersizse null. */
    private fun parseIpv4(text: String): IntArray? {
        if (text.isEmpty() || text.length > 15) return null
        val parts = text.split('.')
        if (parts.size != 4) return null

        val octets = IntArray(4)
        for (i in parts.indices) {
            val part = parts[i]
            if (part.isEmpty() || part.length > 3) return null
            // Baştaki sıfır: "01" reddedilir, tek başına "0" kabul edilir.
            if (part.length > 1 && part[0] == '0') return null
            var value = 0
            for (ch in part) {
                // `Char.isDigit()` Unicode rakamlarını da kabul eder
                // (Arapça-Hint rakamları gibi); burada yalnızca ASCII.
                if (ch !in '0'..'9') return null
                value = value * 10 + (ch - '0')
            }
            if (value > 255) return null
            octets[i] = value
        }
        return octets
    }

    /** Host'un QR'a basacağı metni kurar. */
    fun build(host: String, port: Int, token: ByteArray): String {
        require(isLanIpv4(host)) { "LAN dışı adres yayınlanamaz: $host" }
        require(port in 1..65535) { "geçersiz port: $port" }
        require(token.size == TOKEN_LEN) { "token $TOKEN_LEN bayt olmalı" }
        return "$SCHEME$host:$port?t=${token.toHex()}&v=$PAYLOAD_VERSION"
    }

    /**
     * QR'dan okunan metni ayrıştırır ve doğrular.
     *
     * @throws PairingException her reddedilme durumunda, sebebi mesajda.
     */
    fun parse(raw: String): PairingInfo {
        val text = raw.trim()

        if (!text.startsWith(SCHEME)) throw PairingException("şema vpad:// değil")
        val body = text.substring(SCHEME.length)

        val queryStart = body.indexOf('?')
        if (queryStart < 0) throw PairingException("sorgu bölümü yok")
        val authority = body.substring(0, queryStart)
        val query = body.substring(queryStart + 1)

        // ── adres:port ──
        val colon = authority.lastIndexOf(':')
        if (colon < 0) throw PairingException("port yok")
        val host = authority.substring(0, colon)
        val portText = authority.substring(colon + 1)
        if (host.isEmpty()) throw PairingException("adres boş")
        if (portText.isEmpty() || portText.any { it !in '0'..'9' }) {
            throw PairingException("port sayı değil: '$portText'")
        }
        val port = portText.toIntOrNull() ?: throw PairingException("port okunamadı")
        if (port !in 1..65535) throw PairingException("port aralık dışı: $port")

        if (!isLanIpv4(host)) {
            throw PairingException(
                "adres LAN değil veya IPv4 literal değil: '$host' — " +
                    "kötü niyetli QR olabilir",
            )
        }

        // ── sorgu parametreleri ──
        val params = HashMap<String, String>()
        for (part in query.split('&')) {
            if (part.isEmpty()) continue
            val eq = part.indexOf('=')
            if (eq < 0) throw PairingException("biçimsiz sorgu parçası: '$part'")
            val key = part.substring(0, eq)
            // Yinelenen anahtar: hangi değerin geçerli olduğu belirsiz.
            // Belirsizliği kabul etmek yerine reddet.
            if (params.containsKey(key)) {
                throw PairingException("yinelenen sorgu anahtarı: '$key'")
            }
            params[key] = part.substring(eq + 1)
        }

        val versionText = params["v"] ?: throw PairingException("sürüm (v) yok")
        if (versionText.isEmpty() || versionText.any { it !in '0'..'9' } ||
            versionText.toIntOrNull() != PAYLOAD_VERSION
        ) {
            throw PairingException(
                "desteklenmeyen payload sürümü: '$versionText' " +
                    "(bu sürüm $PAYLOAD_VERSION)",
            )
        }

        val tokenText = params["t"] ?: throw PairingException("token (t) yok")
        val token = tokenFromHex(tokenText)

        return PairingInfo(host = host, port = port, token = token)
    }

    fun tokenFromHex(text: String): ByteArray {
        val trimmed = text.trim()
        if (trimmed.length != TOKEN_LEN * 2) {
            throw PairingException("token ${TOKEN_LEN * 2} hex karakter olmalı")
        }
        val out = ByteArray(TOKEN_LEN)
        for (i in 0 until TOKEN_LEN) {
            val hi = hexDigit(trimmed[i * 2])
            val lo = hexDigit(trimmed[i * 2 + 1])
            out[i] = ((hi shl 4) or lo).toByte()
        }
        return out
    }

    private fun hexDigit(ch: Char): Int = when (ch) {
        in '0'..'9' -> ch - '0'
        in 'a'..'f' -> ch - 'a' + 10
        in 'A'..'F' -> ch - 'A' + 10
        else -> throw PairingException("token hex değil: '$ch'")
    }
}

/** Ayrıştırılmış ve **doğrulanmış** eşleşme bilgisi. */
class PairingInfo(
    val host: String,
    val port: Int,
    val token: ByteArray,
) {
    val tokenHex: String get() = token.toHex()

    /** Loglara basılabilir kısa özet — token'ın tamamı ASLA basılmaz. */
    override fun toString(): String =
        "PairingInfo($host:$port, token=${tokenHex.take(8)}…)"

    override fun equals(other: Any?): Boolean {
        if (this === other) return true
        if (other !is PairingInfo) return false
        return host == other.host && port == other.port &&
            token.contentEquals(other.token)
    }

    override fun hashCode(): Int =
        (host.hashCode() * 31 + port) * 31 + token.contentHashCode()
}

/** Geçersiz veya düşmanca eşleşme verisi. */
class PairingException(message: String) : IllegalArgumentException(message)

internal fun ByteArray.toHex(): String {
    val sb = StringBuilder(size * 2)
    for (b in this) {
        val v = b.toInt() and 0xFF
        sb.append("0123456789abcdef"[v ushr 4])
        sb.append("0123456789abcdef"[v and 0x0F])
    }
    return sb.toString()
}
