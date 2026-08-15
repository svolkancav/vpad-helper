#!/usr/bin/env python3
"""
Çoklu oyuncu slot havuzu — 4 telefon, tek bilgisayar.

`vpad_pairing.PairingGate` ile aynı disiplin: **hiç I/O yapmaz.** Slot
dağıtır, geri alır, yapışkanlığı yönetir; soketi ve enjektörü çağıran
yönetir. Böylece dört telefon olmadan, tek bir birim testiyle
doğrulanabiliyor.

Neden host atıyor, istemci beyan etmiyor
────────────────────────────────────────
Referans uygulama (`Technosaurus8/Magic-Gamepad-Windows`) slot'u mesajın
içine koyuyor: her paket `p1`…`p4` önekiyle geliyor ve host o indeksi
kullanıyor. Ucuz ama kırılgan — iki telefon da `p1` derse birbirinin
girdisini sessizce ezer. Burada slot'u **host** atar; istemci yalnızca
`T_SLOT` çerçevesiyle kendi numarasını öğrenir.

Tavan neden 4
─────────────
Windows XInput aynı anda en fazla dört kumanda tanır (`XUSER_MAX_COUNT`).
Beşinci istemci slot bulamaz ve `REJECT(in_use)` alır.
"""
from __future__ import annotations

from dataclasses import dataclass

# XInput'un tavanı. Bu bir tercih değil, platform sınırı.
MAX_SLOTS = 4


class SlotError(ValueError):
    """Geçersiz slot işlemi — çağıran hatası."""


@dataclass(frozen=True)
class SlotLease:
    """Bir istemciye verilmiş slot."""
    index: int
    key: str


class SlotPool:
    """Oyuncu slotlarını dağıtır ve geri alır.

    Kullanım (daemon tarafı):

        pool = SlotPool(args.players)          # varsayılan 1
        lease = pool.acquire(client_key)
        if lease is None:
            sock.sendall(encode_reject(R_IN_USE, "tüm slotlar dolu"))
            return
        try:
            sock.sendall(pairing.encode_slot(lease.index))
            ...
        finally:
            pool.release(lease)                # nötr rapor GÖNDERİLDİKTEN sonra
    """

    __slots__ = ("_size", "_taken", "_sticky")

    def __init__(self, size: int = 1):
        if not 1 <= size <= MAX_SLOTS:
            raise SlotError(f"slot sayısı 1..{MAX_SLOTS} olmalı, {size} geldi")
        self._size = size
        # index → key. Boş slotlar sözlükte yoktur.
        self._taken: dict[int, str] = {}
        # key → index. Kopan istemcinin eski slotu; yalnızca hatırlatma
        # amaçlı, rezervasyon DEĞİL (bkz. acquire).
        self._sticky: dict[str, int] = {}

    @property
    def size(self) -> int:
        return self._size

    @property
    def free(self) -> int:
        return self._size - len(self._taken)

    def occupants(self) -> dict[int, str]:
        """index → key kopyası. Tanı ve arayüz için."""
        return dict(self._taken)

    def acquire(self, key: str) -> SlotLease | None:
        """Slot verir; havuz doluysa None.

        **Yapışkanlık:** aynı `key` daha önce bir slot aldıysa ve o slot şu
        an boşsa aynısını geri alır. Böylece kopup dönen telefon P1 iken P3
        olmaz. Slot başkası tarafından kapılmışsa ilk boş slota düşer —
        rezervasyon yapılmaz, yoksa dönmeyen bir oyuncu slotu sonsuza kadar
        kilitlerdi.

        `key`'in ne olduğu çağıranın kararı; daemon (cihaz adı, IP) çiftini
        kullanıyor. Kesin kimlik için protokole cihaz kimliği alanı eklemek
        gerekir — bkz. `docs/wifi-transport-architecture.md` §10.5.
        """
        if not key:
            raise SlotError("boş key ile slot alınamaz")

        preferred = self._sticky.get(key)
        if preferred is not None and preferred not in self._taken:
            return self._claim(preferred, key)

        for index in range(self._size):
            if index not in self._taken:
                return self._claim(index, key)
        return None

    def _claim(self, index: int, key: str) -> SlotLease:
        self._taken[index] = key
        self._sticky[key] = index
        return SlotLease(index=index, key=key)

    def release(self, lease: SlotLease) -> None:
        """Slotu geri verir. **Nötr rapor gönderildikten sonra çağrılmalı** —
        aksi hâlde sonraki oyuncu basılı bir tuşla başlar.

        Aynı lease iki kez bırakılırsa sessizce yoksayılır; kopma
        yollarında `finally` iki kez çalışabiliyor ve bu bir hata değil.
        Başkasının slotunu bırakmak ise hatadır ve yoksayılır — o slot
        şu an başka bir istemciye ait.
        """
        if self._taken.get(lease.index) == lease.key:
            del self._taken[lease.index]
        # `_sticky` KASITLI olarak silinmiyor: yapışkanlığın tüm amacı
        # istemci gittikten sonra da hatırlanmasıdır.
