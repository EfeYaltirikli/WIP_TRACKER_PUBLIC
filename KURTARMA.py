# -*- coding: utf-8 -*-
"""WIP TRACKER  -  BOZUK VERITABANI KURTARMA ARACI

NASIL CALISTIRILIR  -  uc yolu da olur

  1) SPYDER  (en kolay)
     Dosyayi Spyder'da acin ve YESIL OK (Run / F5) tusuna basin.
     Argumana gerek yok: veritabaninin yerini kendisi bulmaya calisir,
     bulamazsa asagidaki konsola yolu yazmanizi ister.

  2) CIFT TIK
     Dosyaya cift tiklayin.  Yine yolu sorar.

  3) KOMUT ISTEMI
     py KURTARMA.py "\\\\teidom.local\\5304$\\LEAP\\RW\\DATA\\rw_data.db"

Bozuk bir SQLite dosyasindan okunabilen HER SATIRI cikarip yanina temiz
bir kopya yazar.  Orijinal dosyaya DOKUNMAZ - sadece okur.

NEDEN SATIR SATIR
  "database disk image is malformed" hatasi, dosyanin tamaminin degil
  BELLI SAYFALARININ bozuldugu anlamina gelir.  Tek bir SELECT butun
  tabloyu okumaya calisip ilk bozuk sayfada patlar ve saglam olan her sey
  de gitmis sayilir.  Bu arac tablolari kucuk bloklar halinde okur; bir
  blok patlarsa ikiye boler, sonunda sadece gercekten bozuk satirlari
  atar ve geri kalanini kurtarir.

NEYI KURTARMAK ONEMLI
  Excel'den tekrar yuklenebilen veriler (ops, rw, comp, parts, shipment,
  traveler) en kotu ihtimalle yeniden yuklenir.  ELLE GIRILEN veriler
  boyle degildir ve once onlar kurtarilir:

      serial_flags   MRB / SCRAP / TECO kayitlari
      user_note      kisisel notlar
      layout         B1000 plani
      layout_item    plandaki tezgahlar ve bolgeler
      app_user       kullanicilar ve sifreler
      meta           ayarlar (shop rules, calisma haftasi...)

SONUCTA NE OLUR
  <dosyaadi>_KURTARILDI.db          temiz, acilabilir veritabani
  <dosyaadi>_KURTARMA_RAPORU.txt    hangi tablodan kac satir kurtarildi
"""

import os
import shutil
import sqlite3
import sys
import time

# Elle girilen veriler once gelir: bunlar hicbir Excel'de yok.
ONCELIK = ["meta", "app_user", "serial_flags", "user_note",
           "layout", "layout_item", "discarded", "app_session"]

BLOK = 2000          # tek seferde okunmaya calisilan satir sayisi


def yaz(f, msg):
    print(msg)
    sys.stdout.flush()
    f.write(msg + "\n")


def tablolar(conn):
    """Bozuk dosyadan tablo listesi - bu bile patlayabilir."""
    try:
        rows = [(r[0], r[1]) for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
    except sqlite3.DatabaseError:
        # sqlite_master'in kendisi bozuksa isimleri elle deneriz.
        rows = []
        for n in ONCELIK + ["ops", "rw", "wf_part_data", "wf_traveler",
                            "comp", "parts", "shipment"]:
            rows.append((n, None))
    isimler = [r[0] for r in rows]
    sira = [n for n in ONCELIK if n in isimler]
    return sira + [n for n in isimler if n not in sira], dict(rows)


def tek_deger(conn, sql, params=()):
    """Tek bir sayi okumaya calisir; bozuk sayfaya carparsa None doner."""
    try:
        r = conn.execute(sql, params).fetchone()
        return r[0] if r and r[0] is not None else None
    except sqlite3.DatabaseError:
        return None


def bos_mu(conn, tablo):
    """Tablo GERCEKTEN bos mu - yoksa sadece okunamiyor mu."""
    n = tek_deger(conn, "SELECT COUNT(*) FROM \"%s\"" % tablo)
    return n == 0


def satir_araligi(conn, tablo):
    """En kucuk ve en buyuk rowid.

    Tek bir sorguya guvenilmez: bozuk bir dosyada
    "SELECT MIN(rowid), MAX(rowid)" patlarken ayni seyi iki ayri sorgu
    olarak sormak calisabiliyor - iki ucu ayri agac yollarindan okunuyor.
    Bu yuzden sirayla denenir, ve hicbiri tutmazsa tablo BOS SAYILMAZ:
    tahmini bir ust sinirla yine de taranir.  Aksi halde en buyuk tablo
    "bos" diye raporlanip butun satirlari sessizce kaybolur.
    """
    alt = tek_deger(conn, "SELECT MIN(rowid) FROM \"%s\"" % tablo)
    ust = tek_deger(conn, "SELECT MAX(rowid) FROM \"%s\"" % tablo)
    if ust is None:
        ust = tek_deger(conn, "SELECT rowid FROM \"%s\" ORDER BY rowid DESC LIMIT 1" % tablo)
    if ust is None:
        # AUTOINCREMENT tablolarinin en buyuk id'si burada da yazilidir
        # ve bu tablo cogu zaman saglam kalir.
        ust = tek_deger(conn, "SELECT seq FROM sqlite_sequence WHERE name=?", (tablo,))
    if alt is None:
        alt = tek_deger(conn, "SELECT rowid FROM \"%s\" ORDER BY rowid LIMIT 1" % tablo)
    if ust is None:
        if bos_mu(conn, tablo):
            return (None, None, "bos")
        return (1, 5000000, "aralik okunamadi, tahminle tarandi")
    return (alt if alt is not None else 1, ust, None)


def blok_oku(conn, tablo, a, b, kayip):
    """[a, b] arasini okur; patlarsa ikiye bolup tekrar dener.

    Boylece bozuk bir sayfa sadece kendi satirlarini goturur, butun
    tabloyu degil.
    """
    try:
        return conn.execute(
            "SELECT * FROM \"%s\" WHERE rowid BETWEEN ? AND ?" % tablo,
            (a, b)).fetchall()
    except sqlite3.DatabaseError:
        if a >= b:
            kayip[0] += 1
            return []
        orta = (a + b) // 2
        return (blok_oku(conn, tablo, a, orta, kayip)
                + blok_oku(conn, tablo, orta + 1, b, kayip))


def kurtar(kaynak):
    if not os.path.exists(kaynak):
        print("Dosya bulunamadi: %s" % kaynak)
        return 1

    taban = os.path.splitext(kaynak)[0]
    hedef = taban + "_KURTARILDI.db"
    rapor_yolu = taban + "_KURTARMA_RAPORU.txt"
    yerel = taban + "_OKUMA_KOPYASI.db"

    for y in (hedef, yerel):
        if os.path.exists(y):
            os.remove(y)

    rapor = open(rapor_yolu, "w", encoding="utf-8")
    t0 = time.time()
    yaz(rapor, "WIP TRACKER  -  KURTARMA RAPORU")
    yaz(rapor, "=" * 60)
    yaz(rapor, "Kaynak : %s" % kaynak)
    yaz(rapor, "Boyut  : %.1f MB" % (os.path.getsize(kaynak) / 1048576.0))
    yaz(rapor, "Tarih  : %s" % time.strftime("%d.%m.%Y %H:%M"))
    yaz(rapor, "")

    # Bozuk dosya AGDA duruyorsa once yerele cekilir: ag uzerinde
    # okumak hem yavas hem de yeni hata uretebilir.
    yaz(rapor, "Okuma kopyasi cikariliyor...")
    shutil.copyfile(kaynak, yerel)

    src = sqlite3.connect(yerel)
    src.row_factory = sqlite3.Row
    # Bozuk sayfalardan gelen metin gecerli UTF-8 olmayabilir.  Varsayilan
    # ayarda boyle bir satir kurtarmayi durdurur; burada bozuk karakter
    # yerine "?" konur ve satirin geri kalani kurtarilir.  bytes'a
    # gecmiyoruz: o zaman butun metinler yeni dosyaya BLOB olarak yazilir
    # ve uygulama veriyi taniyamaz.
    src.text_factory = lambda b: b.decode("utf-8", "replace")

    try:
        durum = src.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        durum = "okunamadi (%s)" % e
    yaz(rapor, "integrity_check: %s" % str(durum)[:200])
    yaz(rapor, "")

    isimler, semalar = tablolar(src)
    dst = sqlite3.connect(hedef)

    yaz(rapor, "%-16s %10s %10s   %s" % ("TABLO", "KURTARILAN", "KAYIP", "DURUM"))
    yaz(rapor, "-" * 60)

    toplam_ok = toplam_kayip = 0
    elle_girilen = 0
    for tablo in isimler:
        sema = semalar.get(tablo)
        if not sema:
            try:
                r = src.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?",
                    (tablo,)).fetchone()
                sema = r[0] if r else None
            except sqlite3.DatabaseError:
                sema = None
        if not sema:
            yaz(rapor, "%-16s %10s %10s   %s"
                % (tablo, "-", "-", "tablo tanimi okunamadi, atlandi"))
            continue
        try:
            dst.execute(sema)
        except sqlite3.DatabaseError as e:
            yaz(rapor, "%-16s %10s %10s   %s" % (tablo, "-", "-", e))
            continue

        a, b, not_ = satir_araligi(src, tablo)
        if a is None:
            yaz(rapor, "%-16s %10d %10d   %s" % (tablo, 0, 0, "bos"))
            continue

        kayip = [0]
        alinan = 0
        blok = a
        while blok <= b:
            son = min(blok + BLOK - 1, b)
            satirlar = blok_oku(src, tablo, blok, son, kayip)
            if satirlar:
                n = len(satirlar[0])
                dst.executemany(
                    "INSERT OR IGNORE INTO \"%s\" VALUES (%s)"
                    % (tablo, ",".join("?" * n)),
                    [tuple(r) for r in satirlar])
                alinan += len(satirlar)
            blok = son + 1
        dst.commit()

        toplam_ok += alinan
        if tablo in ONCELIK:
            elle_girilen += alinan
        # Kac satirin gittigini, atlanan bloklari saymak yerine KAYNAKTAKI
        # sayimla karsilastirarak soyluyoruz.  Bozuk bir sayfa bazen hata
        # vermeden eksik satir dondurur, oyle bir kayip blok sayacina hic
        # yansimaz - ve raporun "TAM" demesi en yanlis olacagi yer burasi.
        kaynak_n = tek_deger(src, "SELECT COUNT(*) FROM \"%s\"" % tablo)
        if kaynak_n is None:
            gercek_kayip = kayip[0]
            durum_txt = ("TAM gorunuyor (kaynak sayilamadi)" if not kayip[0]
                         else "EKSIK  (en az %d satir okunamadi)" % kayip[0])
        else:
            gercek_kayip = max(0, kaynak_n - alinan)
            durum_txt = ("TAM" if gercek_kayip == 0
                         else "EKSIK  (%d / %d satir kurtarilamadi)"
                              % (gercek_kayip, kaynak_n))
        toplam_kayip += gercek_kayip
        if not_:
            durum_txt += "  [%s]" % not_
        yaz(rapor, "%-16s %10d %10d   %s" % (tablo, alinan, gercek_kayip, durum_txt))

    yaz(rapor, "-" * 60)
    yaz(rapor, "%-16s %10d %10d" % ("TOPLAM", toplam_ok, toplam_kayip))
    yaz(rapor, "")

    # Indeksler ve tetikleyiciler - satir kurtarmadan sonra kurulur.
    try:
        for r in src.execute(
                "SELECT sql FROM sqlite_master WHERE type IN ('index','trigger','view') "
                "AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"):
            try:
                dst.execute(r[0])
            except sqlite3.DatabaseError:
                pass
        dst.commit()
    except sqlite3.DatabaseError:
        yaz(rapor, "NOT: indeks listesi okunamadi, uygulama acilista kendisi kurar.")

    src.close()
    try:
        son = dst.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        son = str(e)
    dst.close()

    yaz(rapor, "Kurtarilan dosyanin integrity_check sonucu: %s" % str(son)[:120])
    yaz(rapor, "Sure: %.1f saniye" % (time.time() - t0))
    yaz(rapor, "")
    yaz(rapor, "ELLE GIRILEN VERILERDEN KURTARILAN: %d satir" % elle_girilen)
    yaz(rapor, "   (MRB/SCRAP/TECO, notlar, B1000 plani, kullanicilar, ayarlar)")
    yaz(rapor, "")
    yaz(rapor, "SIRADA NE YAPILACAK")
    yaz(rapor, "  1) %s dosyasini rw_data.db olarak kopyalayin" % os.path.basename(hedef))
    yaz(rapor, "  2) Uygulamayi acin, DATA & SETTINGS > DATA HEALTH'e bakin")
    yaz(rapor, "  3) Eksik cikan veri turlerini arsivdeki Excel'lerden")
    yaz(rapor, "     CHANGE ile yeniden yukleyin")
    yaz(rapor, "  4) MRB/SCRAP/TECO sayfasindan EXPORT TO EXCEL alip saklayin")

    try:
        os.remove(yerel)
    except OSError:
        pass
    rapor.close()
    print("\nRapor: %s" % rapor_yolu)
    print("Temiz veritabani: %s" % hedef)
    return 0


def adaylar():
    """Veritabaninin olabilecegi yerler, en olasidan baslayarak.

    Uygulama ortak veritabaninin yerini kendi klasorunde duz metin bir
    dosyada tutar; once oraya bakilir, boylece kullanicinin ag yolunu
    elle yazmasi gerekmez.
    """
    out = []
    try:
        burasi = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        burasi = os.getcwd()
    isaret = os.path.join(burasi, "shared_db_location.txt")
    try:
        with open(isaret, "r", encoding="utf-8") as f:
            yol = f.read().strip()
        if yol:
            out.append(yol)
    except Exception:
        pass
    out.append(os.path.join(burasi, "rw_data.db"))
    out.append(os.path.join(os.getcwd(), "rw_data.db"))
    # Ayni sira iki kez denenmesin
    gorulen, tekil = set(), []
    for y in out:
        k = os.path.normcase(os.path.abspath(y))
        if k not in gorulen:
            gorulen.add(k)
            tekil.append(y)
    return tekil


def yol_sor():
    """Arguman verilmediyse yolu bulur ya da sorar.

    Spyder'dan F5 ile calistirildiginda komut satiri argumani yoktur;
    her seferinde Run ayarlarindan arguman girmek yerine burada
    hallediliyor.
    """
    for y in adaylar():
        if os.path.exists(y):
            print("Bulundu: %s" % y)
            c = input("Bu dosya kurtarilsin mi?  [E/h] ").strip().lower()
            if c in ("", "e", "evet", "y", "yes"):
                return y
            break
    print("")
    print("Kurtarilacak veritabaninin TAM YOLUNU yazin.")
    print("Ornek: C:\\gecici\\rw_data_bozuk.db")
    print("(Dosya Gezgini'nde dosyaya Shift+sag tik > 'Yol olarak kopyala')")
    print("")
    yol = input("Yol: ").strip().strip('"')
    return yol or None


if __name__ == "__main__":
    print(__doc__)
    hedef = sys.argv[1] if len(sys.argv) > 1 else yol_sor()
    if not hedef:
        print("Yol verilmedi, cikiliyor.")
        kod = 1
    else:
        kod = kurtar(hedef)
    # Cift tikla acildiginda pencere kapanip gitmesin.
    try:
        input("\nBitti.  Kapatmak icin Enter'a basin.")
    except EOFError:
        pass
    sys.exit(kod)
