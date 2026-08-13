# `host-connection-mockup.png` — üretim brief'i

Bu dosya, taşıyıcı seçim ekranının görsel maketini üreten/yönlendiren
istemin **birebir** kaydıdır. Maket yeniden üretilecek ya da başka bir
skin'e uyarlanacak olursa başlangıç noktası burasıdır.

> **Not — brief ile mevcut PNG birebir aynı değil.** Elde duran
> `host-connection-mockup.png` **WiFi / Bluetooth** ikili seçimini
> gösteriyor. Aşağıdaki brief ise bir sonraki iterasyonu tarif ediyor:
> Windows sürüm seçici, **WiFi / USB** ikilisi, üç adımlık yönerge ve
> "mührü tara" aksiyon butonu. Yani bu metin üretilmiş görselin değil,
> hedeflenen ekranın tarifi.

---

Karanlık fantezi temalı, oyun içi menü ekranı tasarla — bir mobil
cihazın PC'ye bağlanma arayüzü, ama antik büyü kitabı / rün taşı
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
   "CİHAZ BAĞLANTISI" — beyaz, hafif metalik degrade.
   Altında soluk gri alt başlık: "Bağlanmak istediğiniz cihazı seçin"
   Altında ince ışıklı ayraç: ortasında elmas/rombus mücevher, iki
   yana doğru sönümlenen mavi çizgi.

2. CİHAZ SEÇİMİ — tek satırda dekoratif seçim taşı
   Oymalı çerçeveli yatay panel, solunda Windows logosu rün gibi
   kazınmış halde, ortada "WINDOWS 10/11" metni, sağında aşağı
   bakan üçgen ok. Panel kenarları soluk mavi ışık verir.

3. BAĞLANTI TÜRÜ — yan yana iki büyük rün kartı
   Kart A (SEÇİLİ): mor neon çerçeve, güçlü dış parıltı. İçinde
   büyülü wifi sembolü — üç enerji yayı ve altında havada duran
   mor kristal, arkada yıldız tozu. Çerçeve kenarlarında dizili
   rün harfleri, köşelerde küçük mücevherler.
   Kart B (PASİF): mavi neon çerçeve, daha sönük parıltı, soğuk
   gri-mavi tonlar. İçinde USB sembolü antik bir rün taşı gibi
   oyulmuş, çatlaklarından mavi ışık sızıyor.

   Kartların altında birer bilgi kutusu:
   - Sol: dairesel mor ikon + "WİFİ BAĞLANTISI" başlık +
     "Aynı ağ üzerinden hızlı bağlantı kurun" açıklaması
   - Sağ: dairesel mavi ikon + "USB BAĞLANTISI" başlık +
     "Kablo ile kesintisiz bağlantı kurun" açıklaması
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

## Uygulamaya geçerken dikkat

- **§4 adım 2'deki ürün adı "Virtual Gamepad"** (düzeltildi 2026-08-13).
  Brief'in ilk hâlinde "Remote Gamepad" ve `remotegamepad.com/pc` yazıyordu;
  o ad App Store'daki rakip bir ürüne ait (`docs/ios_report.md` §4.1) ve
  ekrana girmemeli.
  - **`<indirme-adresi>` hâlâ boş.** Gerçek adres belirlenmeden nihai görsel
    üretilmemeli — uydurma bir alan adı, sahibi olmadığımız bir siteye
    yönlendiren bir ekran demek.
  - Adlandırma tutarlılığı: companion deposunda program **"V-Pad Helper"**
    adıyla yayınlanıyor ("V-Pad Helper — the free companion app…"). Ekranda
    "Virtual Gamepad" yazarsa kullanıcı indirme sayfasında farklı bir ad
    görür. İkisinden biri diğerine uydurulmalı.
- Maket **USB** yolu gösteriyor; protokolde bugün USB taşıyıcı yok
  (Android'de BT-HID, iOS'ta ağ). Ekran tasarımı desteklenmeyen bir
  yol vaat etmemeli — ya USB kartı çıkarılmalı ya "yakında" durumu
  açıkça işaretlenmeli.
