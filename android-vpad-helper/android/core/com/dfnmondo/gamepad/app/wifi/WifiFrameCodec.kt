package com.dfnmondo.gamepad.app.wifi

/**
 * Tel çerçeve biçimi: `[u16 LE toplam uzunluk][u8 tip][gövde…]`
 *
 * Uzunluk **başlık dahildir** — yani en küçük geçerli çerçeve 3 bayttır.
 * Python karşılığı: `vpad_pairing.encode_frame` ve
 * `vpad_reference_client.FrameReader`.
 *
 * TCP mesaj sınırı vermez: tek `read` çağrısı bir çerçevenin yarısını da
 * getirebilir, üç buçuk çerçeve de. [Decoder] ikisini de doğru ele alır.
 */
object WifiFrameCodec {

    const val MAX_FRAME = 4096
    const val HEADER_LEN = 3

    // ── Mevcut protokol (vpad_daemon.py) ──
    const val T_HELLO = 0x01
    const val T_REPORT = 0x02
    const val T_PING = 0x03
    const val T_BYE = 0x04
    const val T_HELLO_ACK = 0x10
    const val T_REJECT = 0x11
    const val T_PONG = 0x12

    // ── Eşleşme katmanının eklediği tipler ──
    const val T_CHALLENGE = 0x20
    const val T_AUTH = 0x21

    // ── REJECT sebep kodları ──
    const val R_VERSION_MISMATCH = 0x01
    const val R_IN_USE = 0x02
    const val R_UNSUPPORTED_SKIN = 0x03
    const val R_AUTH_REQUIRED = 0x04
    const val R_AUTH_FAILED = 0x05
    const val R_INTERNAL_ERROR = 0xFF

    const val PROTO_VER = 1

    fun encodeFrame(msgType: Int, payload: ByteArray = ByteArray(0)): ByteArray {
        val total = HEADER_LEN + payload.size
        require(total <= MAX_FRAME) { "çerçeve çok büyük: $total > $MAX_FRAME" }
        val out = ByteArray(total)
        out[0] = (total and 0xFF).toByte()
        out[1] = ((total ushr 8) and 0xFF).toByte()
        out[2] = (msgType and 0xFF).toByte()
        payload.copyInto(out, HEADER_LEN)
        return out
    }

    /**
     * HELLO gövdesi: `[sürüm][ad uzunluğu][ad][skin uzunluğu][skin]`
     *
     * `vpad_daemon.decode_hello`'nun beklediği düzen. Ad ve skin UTF-8 ve
     * her biri en fazla 255 bayt — uzunluk alanları tek bayt.
     */
    fun encodeHello(name: String, skin: String = ""): ByteArray {
        val nameBytes = name.toByteArray(Charsets.UTF_8).truncateTo255()
        val skinBytes = skin.toByteArray(Charsets.UTF_8).truncateTo255()
        val body = ByteArray(2 + nameBytes.size + 1 + skinBytes.size)
        body[0] = PROTO_VER.toByte()
        body[1] = nameBytes.size.toByte()
        nameBytes.copyInto(body, 2)
        body[2 + nameBytes.size] = skinBytes.size.toByte()
        skinBytes.copyInto(body, 3 + nameBytes.size)
        return encodeFrame(T_HELLO, body)
    }

    /**
     * UTF-8 baytlarını 255'e kırpar — **karakter sınırında**.
     *
     * Ham `copyOf(255)` çok baytlı bir karakterin ortasından keserdi ve
     * host tarafında bozuk karakter üretirdi (cihaz adları emoji ve Türkçe
     * karakter içerebiliyor).
     */
    private fun ByteArray.truncateTo255(): ByteArray {
        if (size <= 255) return this
        var end = 255
        // UTF-8 devam baytları 10xxxxxx; başlangıca kadar geri sar.
        while (end > 0 && (this[end].toInt() and 0xC0) == 0x80) end--
        return copyOf(end)
    }

    /**
     * 8 baytlık REPORT — `HidReportSender.sendGamepadReportTo` ile **BİREBİR**.
     *
     * ```
     * [0]   buttons düşük bayt   (A=b0 B=b1 X=b2 Y=b3 L1=b4 R1=b5 Sel=b6 Start=b7)
     * [1]   buttons yüksek 3 bit (L3 R3 Home) + 1 dolgu biti + hat 4 bit
     * [2-5] lx ly rx ry          (u8, merkez 128)
     * [6-7] lt rt                (u8, 0..255)
     * ```
     *
     * WiFi yolunun tüm hilesi bu: uygulamanın Bluetooth'a yazdığı baytların
     * **aynısı**, yalnızca taşıyıcı farklı. Girdi işleme, deadzone ve eğri
     * kodu olduğu gibi kalır.
     */
    fun encodeReport(
        buttons: Int = 0,
        hat: Int = HAT_CENTER,
        lx: Int = 128,
        ly: Int = 128,
        rx: Int = 128,
        ry: Int = 128,
        lt: Int = 0,
        rt: Int = 0,
    ): ByteArray {
        val hatValue = if (hat in 0..7) hat else HAT_CENTER
        val body = byteArrayOf(
            (buttons and 0xFF).toByte(),
            (((buttons ushr 8) and 0x07) or ((hatValue and 0x0F) shl 4)).toByte(),
            lx.coerceIn(0, 255).toByte(),
            ly.coerceIn(0, 255).toByte(),
            rx.coerceIn(0, 255).toByte(),
            ry.coerceIn(0, 255).toByte(),
            lt.coerceIn(0, 255).toByte(),
            rt.coerceIn(0, 255).toByte(),
        )
        return encodeFrame(T_REPORT, body)
    }

    /** `HidDescriptor.HAT_CENTER` ile aynı: 8 = bırakıldı. */
    const val HAT_CENTER = 8

    /** Çözülmüş bir çerçeve. */
    class Frame(val type: Int, val payload: ByteArray) {
        override fun toString(): String =
            "Frame(type=0x${type.toString(16).padStart(2, '0')}, " +
                "len=${payload.size})"
    }

    /**
     * Akış çözücü. [push] ile bayt beslenir, [poll] ile tamamlanan çerçeveler
     * tek tek alınır.
     *
     * **Neden ayrı push/poll:** okuma zaman aşımına uğradığında tamponun
     * kaybolmaması gerekir. Python tarafında bu yüzden generator yerine sınıf
     * kullanıldı; burada da aynı sebeple durum nesnenin içinde tutuluyor.
     */
    class Decoder {
        private var buf = ByteArray(INITIAL_CAPACITY)
        private var size = 0

        fun push(data: ByteArray, length: Int = data.size) {
            require(length >= 0 && length <= data.size) { "geçersiz uzunluk" }
            ensureCapacity(size + length)
            data.copyInto(buf, size, 0, length)
            size += length
        }

        /** Tamamlanmış bir çerçeve varsa döner, yoksa null. */
        fun poll(): Frame? {
            if (size < HEADER_LEN) return null
            val total = (buf[0].toInt() and 0xFF) or ((buf[1].toInt() and 0xFF) shl 8)
            if (total < HEADER_LEN) {
                throw WifiProtocolException("eksik boyutlu çerçeve: $total")
            }
            if (total > MAX_FRAME) {
                throw WifiProtocolException("aşırı büyük çerçeve: $total")
            }
            if (size < total) return null

            val type = buf[2].toInt() and 0xFF
            val payload = buf.copyOfRange(HEADER_LEN, total)
            // Kalanı başa kaydır. Çerçeveler küçük (en fazla 4 KiB) olduğu
            // için bu kopya ölçülebilir bir maliyet değil; halka tampon
            // karmaşıklığı burada kazandırmaz.
            buf.copyInto(buf, 0, total, size)
            size -= total
            return Frame(type, payload)
        }

        /** Tampondaki bekleyen bayt sayısı — tanı amaçlı. */
        val pending: Int get() = size

        private fun ensureCapacity(needed: Int) {
            if (needed <= buf.size) return
            var capacity = buf.size
            while (capacity < needed) capacity *= 2
            buf = buf.copyOf(capacity)
        }

        private companion object {
            // Tipik çerçeve 11 bayt (REPORT); 4 KiB tek okumada gelen
            // yığını rahat karşılar ve büyümeye nadiren ihtiyaç olur.
            const val INITIAL_CAPACITY = 4096
        }
    }
}

/** Tel üzerinde beklenmeyen bir şey görüldü. */
class WifiProtocolException(message: String) : RuntimeException(message)
