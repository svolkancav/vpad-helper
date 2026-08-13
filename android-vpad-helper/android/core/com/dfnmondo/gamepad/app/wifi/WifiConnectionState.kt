package com.dfnmondo.gamepad.app.wifi

/**
 * WiFi köprüsünün yaşam döngüsü durumu.
 *
 * Mevcut `domain/ConnectionState.kt` (Bluetooth) ile **aynı desende** yazıldı:
 * sealed sınıf + `wireTag()`. Böylece Dart katmanına aynı EventChannel
 * sözleşmesiyle sunulabilir ve arayüz iki taşıyıcıyı tek bir durum modeliyle
 * gösterebilir.
 *
 * Bilinçli olarak Bluetooth durumuyla BİRLEŞTİRİLMEDİ: iki taşıyıcının
 * yaşam döngüleri farklı (BT'de "profil kayıtlı", WiFi'de "eşleşme
 * bekleniyor" gibi karşılığı olmayan durumlar var) ve tek bir sealed
 * hiyerarşiye sıkıştırmak her iki tarafta da anlamsız durumlar üretirdi.
 */
sealed class WifiConnectionState {

    /** Henüz bir şey başlatılmadı. */
    data object Idle : WifiConnectionState()

    /** Kamera açık, QR bekleniyor. */
    data object AwaitingQr : WifiConnectionState()

    /** QR okundu ve doğrulandı; TCP bağlantısı kuruluyor. */
    data class Connecting(val host: String, val port: Int) : WifiConnectionState()

    /** CHALLENGE geldi, AUTH gönderildi, cevap bekleniyor. */
    data class Pairing(val host: String, val port: Int) : WifiConnectionState()

    /** HELLO_ACK alındı; girdi akıyor. */
    data class Connected(
        val host: String,
        val port: Int,
        /** Eşleşme kapısından mı geçildi, yoksa eşleşmesiz mod mu. */
        val paired: Boolean,
        /**
         * Host'un atadığı oyuncu indeksi (0..3) — çoklu oyuncu modunda.
         * `null` = tek oyunculu host; arayüz "Oyuncu N" rozetini
         * göstermez. Varsayılanı `null`, böylece tek oyunculu çağıranlar
         * bu alanı hiç bilmek zorunda değil.
         */
        val slot: Int? = null,
    ) : WifiConnectionState()

    /**
     * Host bağlantıyı reddetti. [reason] `WifiFrameCodec.R_*` sabitlerinden.
     *
     * Ayrı bir durum çünkü kullanıcıya gösterilecek mesaj tamamen farklı:
     * "yanlış QR" ile "ağ erişilemiyor" aynı ekranı hak etmiyor.
     */
    data class Rejected(val reason: Int, val message: String) : WifiConnectionState()

    /** Ağ/protokol hatası — yeniden denenebilir. */
    data class Failed(val message: String) : WifiConnectionState()

    /** Temiz kapanış. */
    data object Disconnected : WifiConnectionState()
}

/** EventChannel için kısa etiket — `ConnectionState.wireTag()` ile aynı desen. */
fun WifiConnectionState.wireTag(): String = when (this) {
    is WifiConnectionState.Idle -> "idle"
    is WifiConnectionState.AwaitingQr -> "awaiting_qr"
    is WifiConnectionState.Connecting -> "connecting"
    is WifiConnectionState.Pairing -> "pairing"
    is WifiConnectionState.Connected -> "connected"
    is WifiConnectionState.Rejected -> "rejected"
    is WifiConnectionState.Failed -> "error"
    is WifiConnectionState.Disconnected -> "disconnected"
}

fun WifiConnectionState.isConnected(): Boolean = this is WifiConnectionState.Connected

/** Kullanıcıya gösterilecek sebep metni. Dart tarafı bunu yerelleştirir. */
fun rejectReasonKey(reason: Int): String = when (reason) {
    WifiFrameCodec.R_VERSION_MISMATCH -> "version_mismatch"
    WifiFrameCodec.R_IN_USE -> "host_busy"
    WifiFrameCodec.R_UNSUPPORTED_SKIN -> "unsupported_skin"
    WifiFrameCodec.R_AUTH_REQUIRED -> "pairing_required"
    WifiFrameCodec.R_AUTH_FAILED -> "pairing_failed"
    else -> "internal_error"
}
