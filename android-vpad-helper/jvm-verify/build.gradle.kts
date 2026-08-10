/*
 * Saf-JVM doğrulama projesi.
 *
 * Amacı TEK: `android/core/` altındaki Kotlin dosyalarını gerçekten derlemek
 * ve test etmek. O dosyalar bilinçli olarak hiçbir Android API'si kullanmaz,
 * bu yüzden Android SDK'sı, emülatör veya cihaz olmadan burada koşabiliyorlar.
 *
 * Kaynak dizini kopyalanmıyor, DOĞRUDAN gösteriliyor: `android/core/` hem
 * gamepad_universal'a bırakılacak teslimat hem de burada test edilen kod.
 * Tek kopya — sürüklenme (drift) imkânsız.
 *
 * Android'e özgü dosyalar (`android/ui/`) bu projeye DAHİL DEĞİL; onlar
 * CameraX/ML Kit gerektirir ve ancak uygulama içinde derlenir.
 *
 * Koşturma:
 *     gradle test
 */
plugins {
    kotlin("jvm") version "2.1.0"
}

repositories {
    mavenCentral()
}

sourceSets {
    main {
        // Teslim edilen dosyaların ta kendisi.
        kotlin.setSrcDirs(listOf("../android/core"))
        resources.setSrcDirs(emptyList<String>())
    }
    test {
        kotlin.setSrcDirs(listOf("src/test/kotlin"))
        resources.setSrcDirs(emptyList<String>())
    }
}

dependencies {
    testImplementation(kotlin("test"))
}

tasks.test {
    useJUnitPlatform()
    testLogging {
        events("passed", "failed", "skipped")
        showStandardStreams = false
    }
}
