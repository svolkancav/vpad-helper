# `host-connection-mockup.png` — üretim brief'i

Taşıyıcı seçim ekranının görsel maketini üreten istemin **birebir** kaydı.
Maket yeniden üretilecek ya da başka bir skin'e uyarlanacaksa başlangıç
noktası burasıdır. Ekranın mimari karşılığı:
`docs/wifi-transport-architecture.md` §3 (giriş akışı) ve §7 (görsel katman).

**Sürüm notu.** Bu, brief'in **ikinci** hâli ve elde duran PNG ile örtüşüyor:
başlık "HOST BAĞLANTISI", ikili seçim **WiFi / Bluetooth**. İlk taslak
`USB` kartı tarif ediyordu — protokolde USB taşıyıcı olmadığı için o sürüm
geçersiz, kayıt olarak da tutulmuyor.

**İki yerde bilinçli sapma var** (aşağıdaki "Uygulamaya geçerken" bölümünde
gerekçeleri): ürün adı **Virtual Gamepad**, indirme adresi yer tutucu.

---

Karanlık fantezi temalı, oyun içi menü ekranı tasarla — bir mobil
cihazın host'a bağlanma arayüzü, antik büyü kitabı / rün taşı
estetiğinde. Dikey format (2:3), tam ekran kart görünümü.

ATMOSFER & MALZEME:
Zemin gece mavisi-siyah taş (#080C18 → #101828 gradyanı), üzerinde
hafif mor nebula parıltısı ve toz zerreleri. Tüm çerçeveler oymalı
koyu metal/obsidyen; köşelerde düğüm motifli süslemeler. Neon rün
harfleri camsı mavi ışıkla yanıyor (#3DA9FC) ve etrafa yumuşak bloom
saçıyor. İkincil vurgu ametist moru (#A855F7).

YAPI (yukarıdan aşağı):

1. BAŞLIK BLOĞU
   Ortalanmış, geniş harf aralıklı, sıkıştırılmış büyük harf başlık:
   "HOST BAĞLANTISI" — beyaz, hafif metalik degrade.
   Altında soluk gri alt başlık:
   "Bağlanmak istediğiniz host yöntemini seçin"
   Altında ince ışıklı ayraç: ortasında elmas/rombus mücevher, iki
   yana doğru sönümlenen mavi çizgi.

2. CİHAZ SEÇİMİ — tek satırda dekoratif seçim taşı
   Oymalı çerçeveli yatay panel, solunda Windows logosu rün gibi
   kazınmış halde, ortada "WINDOWS 10/11" metni, sağında aşağı
   bakan üçgen ok. Panel kenarları soluk mavi ışık verir.

3. BAĞLANTI YÖNTEMİ — yan yana iki büyük rün kartı
   Kart A (SEÇİLİ): mor neon çerçeve, güçlü dış parıltı. İçinde
   büyülü wifi sembolü — üç enerji yayı ve altında havada duran
   mor kristal, arkada yıldız tozu. Çerçeve kenarlarında dizili
   rün harfleri, köşelerde küçük mücevherler.
   Kart B (PASİF): mavi neon çerçeve, daha sönük parıltı, soğuk
   gri-mavi tonlar. İçinde Bluetooth sembolü antik bir rün taşı
   gibi oyulmuş — kesişen keskin hatlar, çatlaklarından mavi ışık
   sızıyor. Köşelerde küçük Bluetooth işaretleri.

   Kartların altında birer bilgi kutusu:
   - Sol: dairesel mor ikon + "WİFİ BAĞLANTISI" başlık +
     "Aynı ağ üzerinden hızlı bağlantı kurun" açıklaması
   - Sağ: dairesel mavi ikon + "BLUETOOTH BAĞLANTISI" başlık +
     "Yakın cihazlar ile güvenli bağlantı kurun." açıklaması
   Kutular yarı saydam koyu cam, ince ışıklı kenarlık.

4. ADIMLAR — kadim ferman satırları
   İkinci ışıklı ayraçtan sonra, üç satırlık numaralı liste.
   Numaralar rün rakamları gibi stilize, mavi parıltılı daire içinde.
   1 — "Telefonun ve bilgisayarın aynı ağda olduğundan emin ol"
   2 — "Bilgisayarına Virtual Gamepad kur ve başlat"
       (altında oymalı taş plaka üzerinde: <indirme-adresi>)
   3 — "Ekranda beliren mührü telefonunla tara"

5. ANA BUTON
   Ortalanmış, geniş, oymalı metal çerçeveli aksiyon butonu.
   İçi mor-mavi enerji degradesi, üstünde QR kodu antik bir mühür
   gibi tasarlanmış ikon + "MÜHRÜ TARA" metni. Güçlü dış parıltı.

6. ALT DEKOR
   Ekranın en altında yarım daire şeklinde dizilmiş, yere kazınmış
   parlayan rün çemberi — büyü çağırma dairesi hissi.

TİPOGRAFİ: Başlıklar sıkıştırılmış, geniş harf aralıklı, tümü büyük
harf, sans-serif. Gövde metni temiz ve okunaklı açık gri.
IŞIK: Her ışıklı öğede bloom + hafif kromatik sapma. Genel kontrast
yüksek, ortam karanlık, dikkat parlayan öğelerde.

---

## Uygulamaya geçerken

**Ürün adı — düzeltildi.** Brief'in geldiği hâlde §4 adım 2'de "Remote
Gamepad" ve `remotegamepad.com/pc` yazıyordu. O ad App Store'daki rakip bir
ürüne ait (`docs/ios_report.md` §4.1) ve ekrana girmemeli; **Virtual Gamepad**
olarak düzeltildi. Companion deposunda program bugün "V-Pad Helper" adıyla
yayınlanıyor — telefon ekranı ile indirme sayfası aynı adı söylemeli, ikisinden
biri diğerine uydurulmalı.

**`<indirme-adresi>` hâlâ boş.** Gerçek adres belirlenmeden nihai görsel
üretilmemeli: uydurma bir alan adı, sahibi olmadığımız bir siteye yönlendiren
bir ekran demektir.

**§2'deki "WINDOWS 10/11" açılır kutusu bir vaat.** Bugün gerçek enjeksiyon
yalnız Windows'ta var (ViGEmBus); macOS klavye/fare öykünmesi yapıyor, Linux'ta
`log` dışında yol yok. Açılır kutu seçenek sunacaksa desteklenmeyenler ya
listelenmemeli ya "yakında" olarak işaretlenmeli.

**§4'teki üç adım yalnız İLK eşleşme içindir.** Kalıcı eşleşme kararı gereği
(`docs/wifi-transport-architecture.md` §5) token bir kez alınır ve saklanır;
sonraki açılışlarda adres mDNS ile bulunur, kullanıcı QR'a bir daha
dokunmaz. Ekran, eşleşme kayıtlıyken bu üç adımı **göstermemeli** — doğrudan
"bağlanılıyor" durumuna geçmeli.

**Bitmap olarak gömülmeyecek.** Maket 2,2 MB; APK boyutu bu projede hassas.
Neon çerçeveler ve parıltı mevcut prosedürel araç kutusuyla çizilir
(`neon_finish.dart`, `metal_finish.dart`), büyük dekor gerekirse backdrop
boru hattı (WebP q88) kullanılır.
