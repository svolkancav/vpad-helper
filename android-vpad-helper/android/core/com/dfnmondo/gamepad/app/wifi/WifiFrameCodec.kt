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

    // ── Mevcut protokol (android-vpad-helper/host/vpad_host.py) ──
    // (Eskiden burada `vpad_daemon.py` yazıyordu; o dosya bu depoda hiç yok,
    //  motor `vpad_host.py`.)
    const val T_HELLO = 0x01
    const val T_REPORT = 0x02
    const val T_PING = 0x03
    const val T_BYE = 0x04
    const val T_HELLO_ACK = 0x10
    const val T_REJECT = 0x11
    const val T_PONG = 0x12

    // ── Eşleşme katmanının eklediği tipler ──
    //
    // Kodların kayıt defteri `docs/companion-daemon.md` §4'tür, çalışan
    // daemon değil: daemon yalnızca uyguladığı tipleri tanımlar, spec
    // ileride kullanılacakları da AYIRIR. İlk sürüm CHALLENGE'ı 0x20'ye
    // koymuştu; orası RUMBLE'a ayrılmış (S→C, v3) ve iOS istemcisi de öyle
    // biliyor. Yeni kodlar spec'in yön bloklarındaki ilk boş yerler:
    // S→C 0x10/0x11/0x12 → 0x13,  C→S 0x01..0x05 → 0x06.
    const val T_CHALLENGE = 0x13
    const val T_AUTH = 0x06

    /**
     * **Oturum sürekliliği.** Kayıtlı cihazın QR'sız girişi.
     *
     * `u8 id_len ‖ id(ascii) ‖ nonce(16) ‖ mac(32)`, mac =
     * `HMAC(cihaz anahtarı, "vpad-resume-v1" ‖ challenge ‖ nonce)`.
     *
     * AUTH bir KEZ olur (QR'daki bilet tek kullanımlık, harcanınca ölür);
     * ondan sonraki her bağlanış RESUME'dur. İkisi ayrı tip çünkü host'un
     * neyle doğrulayacağı ayrı: biri biletle, diğeri cihazın kalıcı
     * anahtarıyla.
     */
    const val T_RESUME = 0x07

    /**
     * Host → istemci: kayıt tamamlanınca verilen kalıcı kimlik.
     *
     * `u8 id_len ‖ id(ascii) ‖ key(32)`. AUTH'un hemen ardından, HELLO
     * gönderilmeden gelir; istemci onu saklar ve bir daha QR istemez.
     * Tanımayan istemci atar ve yalnızca sürekliliği kaybeder — bağlantı
     * kırılmaz (spec §9).
     */
    const val T_CREDENTIAL = 0x15

    /**
     * Host → istemci: **sarmalanmış** kimlik (2026-08-19).
     *
     * Gövde `nonce(16) ‖ ct ‖ tag(16)`; çözülünce içinden [T_CREDENTIAL]'ın
     * gövdesinin AYNISI çıkıyor, yani ayrıştırıcı ortak.
     *
     * Neden ayrı bir tip: [T_CREDENTIAL] anahtarı düz metin taşıyordu ve
     * taşıma TLS'siz (CWE-319). Eskisini SİLMEK yayındaki istemcileri
     * kırardı — Play'deki eski sürüm ve App Store'daki iOS uygulaması onu
     * bekliyor. Host hangisini göndereceğine el sıkışmada karar veriyor:
     * QR token'ıyla doğrulanan istemci v2 QR'ı ayrıştırabilmiş demektir,
     * ona 0x16 gider; 6 haneli kod yolunda 0x15 kalır.
     */
    const val T_CREDENTIAL_ENC = 0x16

    /**
     * Host → istemci, 1 bayt: oyuncu indeksi (0..3). Çoklu oyuncu modunda
     * `HELLO_ACK` ile **AYNI yazmada** gelir (spec §4.9 böyle şart koşuyor,
     * "hemen sonra" değil); tek oyunculu host hiç göndermez. Fark taşıyıcı:
     * istemci onu bloklamadan tahliye ediyor, yani ayrı bir segmentte
     * gelirse rozet kaybolur.
     *
     * **Neden `HELLO_ACK` genişletilmedi:** o çerçevenin gövdesi spec'te 2 bayt
     * olarak sabit ve iOS istemcisi uzunluğu doğruluyor — bir bayt eklemek
     * sahadaki iPhone'ları kırardı. Yeni tip eklemek ise spec §9'a göre kırıcı
     * değildir: tanımayan istemci atar.
     */
    const val T_SLOT = 0x14

    /**
     * Spec'in **ayırdığı ama henüz uygulanmayan** tip: host → istemci
     * titreşim, 2 bayt, v3'e ertelenmiş (`companion-daemon.md` §4.7).
     *
     * Burada tanımlı olması bilinçli. Bu kod bir kez "boş" sanılıp CHALLENGE'a
     * verilmişti; çalışan daemon'ın tablosunda görünmediği için boş duruyordu.
     * Sabiti burada tutmak, kodun sahipli olduğunu koda yazar.
     *
     * ⚠️ Burası "gelirse [WifiGamepadClient] tarafından yan kanal olarak
     * atılır" diyordu — **bugün öyle değil.** `consumeSideChannel` yalnız
     * el sıkışma penceresinde koşuyor; el sıkışmadan sonra bu soketi okuyan
     * kimse yok. RUMBLE tanımı gereği oturum ORTASINDA gelir, yani hiç
     * ulaşmaz. Host'a rumble eklendiği gün önce bir okuyucu iş parçacığı
     * gerekiyor (gerekçe [WifiGamepadClient]'ın §3 bloğunda).
     */
    const val T_RUMBLE = 0x20

    // ── REJECT sebep kodları ──
    const val R_VERSION_MISMATCH = 0x01
    const val R_IN_USE = 0x02
    const val R_UNSUPPORTED_SKIN = 0x03
    const val R_AUTH_REQUIRED = 0x04
    const val R_AUTH_FAILED = 0x05

    /**
     * Host bu cihazı tanımıyor: kaydı hiç yok, ya da 30 gün kullanılmadığı
     * için düşmüş, ya da `--forget` ile silinmiş.
     *
     * **`R_AUTH_FAILED`'dan ayrı olması işlevsel bir fark.** Bu sebep gelince
     * istemci sakladığı kimliği ATMALI ve QR ekranına düşmeli; `AUTH_FAILED`
     * gelince DÜŞMEMELİ (anahtar doğru olabilir, sorun başka). İkisini aynı
     * sayan bir istemci, kullanıcıyı gereksiz yere QR taramaya yollar.
     */
    const val R_DEVICE_UNKNOWN = 0x06

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
     * `vpad_host.decode_hello`'nun okuduğu düzen. Uzunluk alanları tek bayt
     * olduğu için buradaki teknik tavan 255.
     *
     * ⚠️ **Spec'in sınırı daha dar:** §4.1 `name` ≤ 63, `skin` ≤ 16 diyor ve
     * iOS kodlayıcı tam o değerlerle kırpıyor (`FrameCodec.swift`,
     * `clampUtf8(name, 63)` / `(skin, 16)`). Host hiçbir üst sınır
     * uygulamıyor, yani bugün ayrışma görünmüyor — ama iki istemci farklı
     * kırpıyor. Pratikte etkisiz (reklam adı sabit "V-Pad"), yine de
     * sözleşme sapması.
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
