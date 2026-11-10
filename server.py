import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

app = Flask(__name__)


# ============================================================
# YALCIN PRO - 614 BIST HISSE
# Yahoo Finance Chart API tabanli, cache'li sunucu
#
# DUZELTILENLER:
# - /all eski cache'i sonsuza kadar dondurmuyor
# - Cache belirlenen sureyi gecince otomatik yenileniyor
# - /refresh zorunlu yeni veri cekiyor
# - Arka plan otomatik yenileme devam ediyor
# - Fiyat ve degisim yuzdesi yeni verilerle guncelleniyor
# ============================================================


# ============================================================
# AYARLAR
# ============================================================

TARGET = 0  # 0 = sembol sayisini sinirlama

SYMBOL_FILE = "yalcin_pro_active_symbols.json"

# ============================================================
# OTOMATIK BIST SEMBOL KAYNAGI
# ============================================================
# KAP kaynakli, topluluk tarafindan güncellenen BIST sembol listesi.
# Sunucu belirli araliklarla bu listeyi kontrol eder.
SYMBOL_SOURCE_URL = (
    "https://raw.githubusercontent.com/ahmeterenodaci/"
    "Istanbul-Stock-Exchange--BIST--including-symbols-and-logos/"
    "main/bist.json"
)

# RESMI KAP KAYNAGI
KAP_COMPANIES_URL = "https://www.kap.org.tr/tr/bist-sirketler"

# KAP cevabi bazen HTML icinde JSON olarak gelir.
# Bu nedenle hem stockCode alanlarini hem de sirket tablosundaki kodlari okuyoruz.
# KAP gecici olarak bozuk/eksik cevap verirse mevcut liste SILINMEZ.
MIN_VALID_ONLINE_SYMBOLS = 400

# Yeni sembol listesini 1 saatte bir kontrol et.
# Boylece yeni hisse / sembol degisikligi gun icinde otomatik yakalanir.
SYMBOL_SOURCE_REFRESH_SECONDS = 60 * 60

last_symbol_source_check = 0


# Yahoo'yu gereksiz yere hizli bombardimana tutmamak icin
# kontrollu paralellik.
WORKERS = 6

# Yahoo istek timeout
REQUEST_TIMEOUT = 15

# Her hisse icin tekrar deneme
RETRY_COUNT = 2

# Cache kac saniyede bir yenilensin?
#
# 60 saniye:
# - Normal otomatik yenileme
# - Yahoo'ya asiri istek gondermez
#
# Yenile butonu ise bunu beklemez ve force=True ile
# aninda yeni veri ister.
REFRESH_SECONDS = 60


# ============================================================
# YAHOO FINANCE
# ============================================================

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}.IS"


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "Chrome/153 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
})


# ============================================================
# CACHE
# ============================================================

cache = {}

last_refresh = {
    "timestamp": 0,
    "updated": 0,
    "missing": TARGET,
    "total": TARGET,
    "status": "baslatiliyor",
}


# Ayni anda iki farkli yenileme yapilmasini engeller.
refresh_lock = threading.Lock()


# ============================================================
# ZAMAN
# ============================================================

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# SEMBOLLERI OKU
# ============================================================

def load_symbols():
    path = os.path.join(
        os.path.dirname(__file__),
        SYMBOL_FILE
    )

    if not os.path.exists(path):
        print(
            "YALCIN PRO - SEMBOL DOSYASI YOK:",
            path
        )
        return []

    try:
        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            obj = json.load(f)

        found = []

        def add(value):
            if isinstance(value, str):
                s = value.strip().upper()

                # .IS varsa kaldir
                s = s.replace(".IS", "")

                # BIST sembol kontrolu
                if (
                    2 <= len(s) <= 8
                    and s.isalnum()
                ):
                    if s not in found:
                        found.append(s)

        # Liste
        if isinstance(obj, list):

            for x in obj:

                if isinstance(x, str):
                    add(x)

                elif isinstance(x, dict):
                    add(
                        x.get("symbol")
                        or x.get("sembol")
                        or x.get("code")
                    )

        # Obje
        elif isinstance(obj, dict):

            # Liste iceren ilk uygun alani bul
            for key in (
                "symbols",
                "semboller",
                "data",
                "stocks"
            ):

                value = obj.get(key)

                if isinstance(value, list):

                    for x in value:

                        if isinstance(x, str):
                            add(x)

                        elif isinstance(x, dict):
                            add(
                                x.get("symbol")
                                or x.get("sembol")
                                or x.get("code")
                            )

                    break

        print(
            f"YALCIN PRO - AKTIF SEMBOL: "
            f"{len(found)} / {TARGET}"
        )

        return found if TARGET <= 0 else found[:TARGET]

    except Exception as e:

        print(
            "YALCIN PRO - SEMBOL OKUMA HATASI:",
            repr(e)
        )

        return []


# ============================================================
# GERCEK BIST HISSE FILTRESI
# ============================================================

def is_real_bist_equity(symbol):
    """
    Yahoo Finance tarafinda sembolun gercek bir sirket hissesi
    (EQUITY) olup olmadigini kontrol eder.
    """
    try:
        r = session.get(
            YAHOO_URL.format(symbol),
            params={
                "range": "1d",
                "interval": "1d",
            },
            timeout=10,
        )

        if r.status_code != 200:
            return False

        payload = r.json()

        result = (
            payload
            .get("chart", {})
            .get("result", [])
        )

        if not result:
            return False

        meta = result[0].get("meta", {})

        instrument_type = str(
            meta.get("instrumentType", "")
        ).upper()

        return instrument_type == "EQUITY"

    except Exception:
        return False


def filter_real_bist_equities(symbols):
    """
    Sadece Yahoo Finance tarafinda EQUITY olarak tanimlanan
    sembolleri kabul eder.
    """
    valid = []

    for symbol in symbols:
        if is_real_bist_equity(symbol):
            valid.append(symbol)
        else:
            print(
                f"YALCIN PRO - HISSE DEGIL, ATLANDI: {symbol}"
            )

    print(
        f"YALCIN PRO - GERCEK HISSE SAYISI: {len(valid)}"
    )

    return valid


# ============================================================
# SEMBOL LISTESI
# ============================================================

SYMBOLS = load_symbols()

# ONEMLI:
# Sunucu baslangicinda 600+ sembolu Yahoo'da tek tek kontrol ETME.
# Bu eski filtreleme sunucunun 5000 portunu acmadan uzun sure beklemesine
# neden oluyordu. Gercek veri kontrolu fetch_one() sirasinda yapilir.
SYMBOLS = load_symbols()

# Sembol listesi calisma sirasinda guvenli sekilde guncellenebilsin.
symbols_lock = threading.Lock()

# Sembol listesi çalışma sırasında güvenli şekilde güncellenebilsin.
symbols_lock = threading.Lock()


def is_real_bist_equity(symbol):
    """
    Sembolün Yahoo Finance tarafında gerçek bir şirket hissesi (EQUITY)
    olup olmadığını kontrol eder.
    Endeks, ETF, emtia, döviz vb. ürünleri listeye almaz.
    """
    try:
        r = session.get(
            YAHOO_URL.format(symbol),
            params={
                "range": "1d",
                "interval": "1d",
            },
            timeout=10
        )

        if r.status_code != 200:
            return False

        payload = r.json()
        result = (
            payload
            .get("chart", {})
            .get("result", [])
        )

        if not result:
            return False

        meta = result[0].get("meta", {})
        quote_type = str(
            meta.get("instrumentType", "")
        ).upper()

        return quote_type == "EQUITY"

    except Exception:
        return False


def filter_real_bist_equities(symbols):
    """
    Online kaynaktan gelen sembolleri gerçek şirket hisseleriyle sınırlar.
    """
    valid = []

    for symbol in symbols:
        if is_real_bist_equity(symbol):
            valid.append(symbol)
        else:
            print(
                f"YALCIN PRO - HISSE DEGIL, ATLANDI: {symbol}"
            )

    print(
        f"YALCIN PRO - GERCEK HISSE SAYISI: {len(valid)}"
    )

    return valid


def _clean_symbol(value):
    if not isinstance(value, str):
        return None

    s = value.strip().upper().replace(".IS", "")

    if 2 <= len(s) <= 8 and s.isalnum():
        return s

    return None


def fetch_kap_symbols():
    """
    KAP'in guncel BIST sirket listesini okur.

    KAP sayfasinda semboller iki farkli sekilde bulunabildigi icin:
      1) JSON icindeki stockCode alanlari
      2) HTML sirket tablosundaki /sirket-bilgileri/ozet/... kodlari

    birlikte okunur.
    """
    try:
        print(
            "YALCIN PRO - KAP KONTROL:",
            KAP_COMPANIES_URL
        )

        r = session.get(
            KAP_COMPANIES_URL,
            timeout=30
        )

        if r.status_code != 200:
            print(
                f"YALCIN PRO - KAP HTTP: {r.status_code}"
            )
            return []

        text = r.text or ""
        found = []
        seen = set()

        def add(value):
            symbol = _clean_symbol(value)
            if symbol and symbol not in seen:
                seen.add(symbol)
                found.append(symbol)

        # --------------------------------------------------------
        # 1) KAP'in sayfa icindeki JSON verisi
        # --------------------------------------------------------
        for match in re.finditer(
            r'(?:\\?\")?stockCode(?:\\?\")?\s*:\s*(?:\\?\")([A-Z0-9]{2,8})(?:\\?\")',
            text,
            flags=re.IGNORECASE,
        ):
            add(match.group(1))

        # --------------------------------------------------------
        # 2) KAP HTML sirket tablosu
        # Ornek:
        # /tr/sirket-bilgileri/ozet/...><div>THYAO</div>
        # --------------------------------------------------------
        for match in re.finditer(
            r'/tr/sirket-bilgileri/ozet/[^"\']+["\']?[^>]*>\s*<div[^>]*>\s*([A-Z0-9]{2,8})\s*</div>',
            text,
            flags=re.IGNORECASE,
        ):
            add(match.group(1))

        # --------------------------------------------------------
        # 3) Daha esnek HTML fallback
        # --------------------------------------------------------
        for match in re.finditer(
            r'<div[^>]*>\s*([A-Z0-9]{2,8})\s*</div>',
            text,
            flags=re.IGNORECASE,
        ):
            symbol = _clean_symbol(match.group(1))
            if symbol:
                # KAP sayfasindaki sirket kodlarini almak icin
                # yalnizca bilinen hisse kodu bicimine uygun olanlari ekle.
                add(symbol)

        # Asiri genel HTML regex'i menu/ID gibi yanlis degerler uretirse
        # stockCode veya sirket tablosu sonuclarini tercih et.
        if len(found) > 1000:
            found = found[:1000]

        print(
            f"YALCIN PRO - KAP BULUNAN: {len(found)}"
        )

        return found

    except Exception as e:
        print(
            "YALCIN PRO - KAP HATA:",
            repr(e)
        )
        return []


def fetch_github_symbols():
    """KAP kullanilamazsa yardimci/fallback sembol kaynagi."""
    try:
        r = session.get(
            SYMBOL_SOURCE_URL,
            timeout=20
        )

        if r.status_code != 200:
            print(
                f"YALCIN PRO - GITHUB SEMBOL KAYNAGI HTTP {r.status_code}"
            )
            return []

        payload = r.json()
        found = []
        seen = set()

        def add(value):
            symbol = _clean_symbol(value)
            if symbol and symbol not in seen:
                seen.add(symbol)
                found.append(symbol)

        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = []
            for key in ("symbols", "semboller", "data", "stocks"):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
        else:
            items = []

        for item in items:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, dict):
                add(
                    item.get("symbol")
                    or item.get("sembol")
                    or item.get("code")
                )

        print(
            f"YALCIN PRO - GITHUB FALLBACK: {len(found)} SEMBOL"
        )
        return found

    except Exception as e:
        print(
            "YALCIN PRO - GITHUB SEMBOL HATA:",
            repr(e)
        )
        return []


def fetch_online_symbols():
    """
    Guncel BIST sembollerini KAP'tan alir.

    KAP basarili ve makul sayida sembol donderirse ANA kaynak KAP'tir.
    KAP gecici olarak bozuk/eksik cevap verirse GitHub fallback denenir.
    Fallback de yetersizse bos doner ve mevcut liste korunur.
    """
    kap = fetch_kap_symbols()

    if len(kap) >= MIN_VALID_ONLINE_SYMBOLS:
        print(
            "YALCIN PRO - ONLINE ANA KAYNAK: KAP",
            f"| {len(kap)}"
        )
        return kap

    print(
        "YALCIN PRO - KAP LISTESI EKSIK:",
        len(kap),
        f"< {MIN_VALID_ONLINE_SYMBOLS}"
    )

    fallback = fetch_github_symbols()

    if len(fallback) >= MIN_VALID_ONLINE_SYMBOLS:
        print(
            "YALCIN PRO - ONLINE FALLBACK: GITHUB",
            f"| {len(fallback)}"
        )
        return fallback

    print(
        "YALCIN PRO - ONLINE SEMBOL KAYNAGI YOK / EKSIK"
    )
    return []

def refresh_symbols_from_online_source(force=False):
    """
    Online sembol kaynagini periyodik olarak kontrol eder.

    Online kaynak basariliysa:
    - Yeni semboller otomatik eklenir.
    - Artik kaynakta bulunmayan eski semboller otomatik kaldirilir.
    - Sembol degistiren hisselerde eski sembol gider, yeni sembol gelir.
    - Ornek: MARKA kaldirilip USHOL geldiyse MARKA silinir, USHOL eklenir.

    Online kaynak gecici olarak kullanilamazsa mevcut liste korunur.
    """
    global SYMBOLS
    global last_symbol_source_check

    now = time.time()

    if (
        not force
        and last_symbol_source_check > 0
        and now - last_symbol_source_check < SYMBOL_SOURCE_REFRESH_SECONDS
    ):
        return False

    online = fetch_online_symbols()
    last_symbol_source_check = now

    if not online:
        print("YALCIN PRO - ONLINE SEMBOL LISTESI ALINAMADI")
        print("YALCIN PRO - MEVCUT SEMBOL LISTESI KORUNUYOR")
        return False

    online_clean = []
    seen = set()

    for symbol in online:
        if not isinstance(symbol, str):
            continue

        s = symbol.strip().upper().replace(".IS", "")

        if not s or s in seen:
            continue

        seen.add(s)
        online_clean.append(s)

    # Cok kucuk/bozuk bir online cevap gelirse mevcut listeyi koru.
    # Bu kontrol yanlislikla 600+ hissenin 1-2 sembole dusmesini engeller.
    if len(online_clean) < MIN_VALID_ONLINE_SYMBOLS:
        print(
            "YALCIN PRO - ONLINE LISTE GUVENLIK REDDI | "
            f"GELEN={len(online_clean)} | "
            f"MIN={MIN_VALID_ONLINE_SYMBOLS}"
        )
        print(
            "YALCIN PRO - MEVCUT SEMBOL LISTESI KORUNUYOR"
        )
        return False

    with symbols_lock:
        old = list(SYMBOLS)
        old_set = set(old)
        new_set = set(online_clean)

        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)

        if not added and not removed:
            print(
                "YALCIN PRO - OTOMATIK SEMBOL KONTROLU: "
                f"DEGISIKLIK YOK | TOPLAM={len(online_clean)}"
            )
            return False

        # Online kaynak artik guncel ana sembol listemizdir.
        SYMBOLS = online_clean

        # Artik aktif olmayan sembollerin eski cache verisini de sil.
        # Boylece MARKA gibi kaldirilan bir sembol /all cevabinda kalmaz.
        for old_symbol in removed:
            cache.pop(old_symbol, None)

        print("=================================================")
        print("YALCIN PRO - SEMBOL LISTESI GUNCELLENDI")
        print(f"YALCIN PRO - ESKI={len(old_set)}")
        print(f"YALCIN PRO - YENI={len(new_set)}")
        print(f"YALCIN PRO - EKLENEN={len(added)}")
        print(f"YALCIN PRO - CIKAN={len(removed)}")

        if added:
            print(
                "YALCIN PRO - YENI SEMBOLLER: "
                + ", ".join(added)
            )

        if removed:
            print(
                "YALCIN PRO - CIKAN HISSeler: "
                + ", ".join(removed)
            )

        if "MARKA" in removed:
            print(
                "YALCIN PRO - MARKA ESKI SEMBOL OLARAK "
                "LISTEDEN KALDIRILDI"
            )

        if "USHOL" in added:
            print(
                "YALCIN PRO - USHOL YENI SEMBOL OLARAK "
                "LISTEYE EKLENDI"
            )

        try:
            symbol_path = os.path.join(
                os.path.dirname(__file__),
                SYMBOL_FILE
            )

            with open(
                symbol_path,
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(
                    SYMBOLS,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            print("YALCIN PRO - SEMBOL DOSYASI GUNCELLENDI")

        except Exception as e:
            print(
                "YALCIN PRO - SEMBOL DOSYASI YAZMA HATASI:",
                repr(e)
            )

        print("=================================================")
        return True

def refresh_symbols_from_file():
    """
    yalcin_pro_active_symbols.json dosyasını tekrar okur.
    Böylece dosya sonradan güncellenirse sunucu yeniden başlatılmadan
    yeni hisseler sisteme alınabilir.
    """
    global SYMBOLS

    new_symbols = load_symbols()

    if not new_symbols:
        return False

    with symbols_lock:
        old_set = set(SYMBOLS)
        new_set = set(new_symbols)

        added = sorted(new_set - old_set)
        removed = sorted(old_set - new_set)

        if added or removed:
            SYMBOLS = new_symbols

            print(
                f"YALCIN PRO - SEMBOL LISTESI GUNCELLENDI | "
                f"ESKI={len(old_set)} | YENI={len(new_set)}"
            )

            if added:
                print(
                    "YALCIN PRO - YENI HISSELER: "
                    + ", ".join(added)
                )

            if removed:
                print(
                    "YALCIN PRO - CIKAN HISSeler: "
                    + ", ".join(removed)
                )

            return True

    return False


# ============================================================
# TEK HISSE VERISI CEK
# ============================================================

def fetch_one(symbol):
    """
    Tek hisse icin Yahoo Chart verisini alir.

    Donen alanlar:
        sembol
        kod
        symbol
        code
        fiyat
        price
        oncekiKapanis
        previousClose
        degisimYuzde
        changePercent
        paraBirimi
        currency
    """

    url = YAHOO_URL.format(symbol)

    for attempt in range(RETRY_COUNT + 1):

        try:

            # ------------------------------------------------
            # YAHOO ISTEGI
            # ------------------------------------------------

            r = session.get(
                url,
                params={
                    # 5 gunluk veri
                    "range": "1mo",

                    # Gunluk veri.
                    # regularMarketPrice meta alanindan
                    # guncel fiyati almaya calisiyoruz.
                    "interval": "1d",

                    "events": "div,splits",

                    "includeAdjustedClose": "true",
                },
                timeout=REQUEST_TIMEOUT,
            )


            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if r.status_code == 429:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"429 RATE LIMIT | "
                    f"DENEME={attempt + 1}"
                )

                time.sleep(
                    1.5 * (attempt + 1)
                )

                continue


            # ------------------------------------------------
            # HTTP HATASI
            # ------------------------------------------------

            if r.status_code != 200:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"HTTP {r.status_code}"
                )

                return None


            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            payload = r.json()

            result = (
                payload
                .get("chart", {})
                .get("result", [])
            )


            if not result:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"YAHOO RESULT BOS"
                )

                return None


            # ------------------------------------------------
            # META
            # ------------------------------------------------

            meta = result[0].get(
                "meta",
                {}
            )


            # Guncel fiyat
            price = meta.get(
                "regularMarketPrice"
            )

            # Onceki kapanis
            previous = meta.get(
                "previousClose"
            )


            # ------------------------------------------------
            # CHART KAPANISLARI (timestamp ile birlikte)
            # ------------------------------------------------
            indicators = result[0].get("indicators", {})
            quote = indicators.get("quote", [])
            raw_closes = quote[0].get("close", []) if quote else []
            adjusted = indicators.get("adjclose", [])
            raw_adjusted_closes = (
                adjusted[0].get("adjclose", []) if adjusted else []
            )
            timestamps = result[0].get("timestamp") or []

            # Kapanislari tarihleriyle eslestir; None degerleri ayiklayip
            # indeksleri kaydirmak yanlis seans secimine neden olabilir.
            candles = []
            istanbul = ZoneInfo("Europe/Istanbul")
            for index, (ts, close_value) in enumerate(zip(timestamps, raw_closes)):
                if ts is None or close_value is None:
                    continue
                try:
                    close_float = float(close_value)
                    adjusted_value = (
                        raw_adjusted_closes[index]
                        if index < len(raw_adjusted_closes)
                        else None
                    )
                    adjusted_float = (
                        float(adjusted_value)
                        if adjusted_value is not None
                        else None
                    )
                    candle_date = datetime.fromtimestamp(
                        int(ts), tz=istanbul
                    ).date()
                except (TypeError, ValueError, OverflowError):
                    continue
                if close_float > 0:
                    candles.append((candle_date, close_float, adjusted_float))

            # ------------------------------------------------
            # FIYAT
            # ------------------------------------------------
            if price is None and candles:
                price = candles[-1][1]

            if price is None:
                print(f"YALCIN PRO - {symbol}: FIYAT BULUNAMADI")
                return None

            price = float(price)

            # ------------------------------------------------
            # ONCEKI KAPANIS / GUNLUK DEGISIM
            # ------------------------------------------------
            # DUNYH icin Yahoo meta.previousClose hatali gelebiliyor.
            # Tarihli gunluk mumlardan son tamamlanmis seansin kapanisini
            # sec. Seans tarihini Istanbul saatine gore belirle.
            if previous is not None:
                try:
                    previous = float(previous)
                    if previous <= 0:
                        previous = None
                except (TypeError, ValueError):
                    previous = None

            if symbol == "DUNYH":
                # ONCELIK: Yahoo meta.previousClose. Onceki surum bu alan
                # makul olsa bile chart kapanisini zorla seciyordu; DUNYH
                # chart verisindeki 109.10 gibi olcekli kapanis bu nedenle
                # 12.27 TL fiyatla uyusmuyordu.
                meta_previous = previous
                meta_change = (
                    ((price - meta_previous) / meta_previous) * 100.0
                    if meta_previous is not None and meta_previous > 0
                    else None
                )

                if meta_change is not None and abs(meta_change) <= 15.0:
                    previous = meta_previous
                    print(
                        "YALCIN PRO - DUNYH META ONCEKI KAPANIS KABUL: "
                        f"KAPANIS={previous} | DEGISIM={meta_change:.4f}%"
                    )
                else:
                    # Meta alani makul degilse son tamamlanmis chart mumunu
                    # kullan; sadece fiyatla +/-15% icindeki deger kabul edilir.
                    today_istanbul = datetime.now(istanbul).date()
                    prior_candles = [
                        candle for candle in candles if candle[0] < today_istanbul
                    ]

                    if prior_candles:
                        latest_candle = max(prior_candles, key=lambda item: item[0])
                        chart_reference_date = latest_candle[0]
                        close_reference = latest_candle[1]
                        adjusted_reference = latest_candle[2]

                        candidates = []
                        if close_reference and close_reference > 0:
                            candidates.append(("close", close_reference))
                        if adjusted_reference and adjusted_reference > 0:
                            candidates.append(("adjclose", adjusted_reference))

                        valid_candidates = [
                            (kind, value, ((price - value) / value) * 100.0)
                            for kind, value in candidates
                            if abs(((price - value) / value) * 100.0) <= 15.0
                        ]

                        if valid_candidates:
                            kind, previous, candidate_change = valid_candidates[0]
                            print(
                                "YALCIN PRO - DUNYH CHART KAPANIS KABUL: "
                                f"TARIH={chart_reference_date} | "
                                f"KAYNAK={kind} | KAPANIS={previous} | "
                                f"DEGISIM={candidate_change:.4f}%"
                            )
                        else:
                            previous = None
                            print(
                                "YALCIN PRO - DUNYH META/CHART KAPANIS "
                                "MAKUL DEGIL; DEGISIM GECERSIZ. "
                                f"META={meta_previous} | "
                                f"TARIH={chart_reference_date} | "
                                f"CLOSE={close_reference} | "
                                f"ADJCLOSE={adjusted_reference}"
                            )
                    else:
                        previous = None
                        print(
                            "YALCIN PRO - DUNYH: ONCEKI KAPANIS "
                            "TARIHLE DOGRULANAMADI."
                        )

            # Other tickers: Yahoo's previousClose is used when valid;
            # if absent, use the most recent chart candle before today.
            elif previous is None:
                today_istanbul = datetime.now(istanbul).date()
                prior_candles = [
                    candle for candle in candles if candle[0] < today_istanbul
                ]
                if prior_candles:
                    latest_candle = max(prior_candles, key=lambda item: item[0])
                    previous = latest_candle[1]

            change = None
            if previous is not None and previous > 0:
                change = ((price - previous) / previous) * 100.0

            if symbol == "DUNYH":
                # BIST'te olağan günlük hareketin çok dışındaki fark,
                # fiyat ölçeği/kurumsal işlem kaynaklı veri uyuşmazlığı
                # olabilir. Yanlış -80/-90% göstermemek için böyle bir
                # durumda kapanışı ve yüzdeyi geçersiz işaretle.
                # Fiyatlar eşleşene kadar uygulama bu değişimi kullanmasın.
                if change is not None and abs(change) > 15.0:
                    print(
                        "YALCIN PRO - DUNYH VERI UYUSMAZLIGI: "
                        f"FIYAT={price} | ONCEKI={previous} | "
                        f"HESAP={change:.4f}% | DEGISIM GECERSIZ"
                    )
                    previous = None
                    change = None

                change_valid = change is not None
                if change is not None:
                    print(
                        f"YALCIN PRO - DUNYH HESAPLANAN DEGISIM: "
                        f"{change:.4f}%"
                    )

            # ------------------------------------------------
            # LOG
            # ------------------------------------------------

            if previous is None:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"ONCEKI KAPANIS BULUNAMADI | "
                    f"FIYAT={price}"
                )


            # ------------------------------------------------
            # SONUC
            # ------------------------------------------------

            item = {

                "sembol": symbol,

                "kod": symbol,

                "symbol": symbol,

                "code": symbol,


                "fiyat": round(
                    price,
                    4
                ),

                "price": round(
                    price,
                    4
                ),


                "oncekiKapanis": (
                    round(
                        previous,
                        4
                    )
                    if previous is not None
                    else None
                ),

                "previousClose": (
                    round(
                        previous,
                        4
                    )
                    if previous is not None
                    else None
                ),


                "degisimYuzde": (
                    round(
                        change,
                        4
                    )
                    if change is not None
                    else None
                ),

                "changePercent": (
                    round(
                        change,
                        4
                    )
                    if change is not None
                    else 0.0
                ),


                "paraBirimi": "TRY",

                "currency": "TRY",

                "changeValid": bool(change is not None),
            }


            return item


        except Exception as e:

            print(
                f"YALCIN PRO - {symbol}: "
                f"HATA | "
                f"{repr(e)}"
            )


            if attempt < RETRY_COUNT:

                time.sleep(
                    0.8 * (attempt + 1)
                )

            else:

                return None


    return None


# ============================================================
# TUM HISSeleri YENILE
# ============================================================

def refresh_all(force=False):

    global cache
    global last_refresh


    # --------------------------------------------------------
    # Ayni anda ikinci refresh'i engelle
    # --------------------------------------------------------

    if not refresh_lock.acquire(
        blocking=False
    ):

        print(
            "YALCIN PRO - "
            "REFRESH ZATEN CALISIYOR"
        )

        return len(cache)


    try:

        # ----------------------------------------------------
        # CACHE KONTROLU
        # ----------------------------------------------------

        if not force and cache:

            age = (
                time.time()
                - last_refresh["timestamp"]
            )


            if age < REFRESH_SECONDS:

                print(
                    f"YALCIN PRO - "
                    f"REFRESH ATLANDI | "
                    f"CACHE YASI={age:.1f}s"
                )

                return len(cache)


        # ----------------------------------------------------
        # SEMBOL SAYISI
        # ----------------------------------------------------

        total = len(SYMBOLS)


        if total == 0:

            last_refresh.update({

                "status": "sembol_yok",

                "timestamp": time.time(),

                "updated": 0,

                "total": TARGET,

                "missing": TARGET,
            })

            return 0


        # ----------------------------------------------------
        # DURUM
        # ----------------------------------------------------

        last_refresh.update({

            "status": "veri_aliniyor",

            "total": total,

            "missing": total,

            "updated": 0,
        })


        print(
            ""
        )

        print(
            "=================================================="
        )

        print(
            f"YALCIN PRO - YAHOO YENILEME BASLADI"
        )

        print(
            f"HISSE SAYISI : {total}"
        )

        print(
            f"WORKER       : {WORKERS}"
        )

        print(
            f"FORCE        : {force}"
        )

        print(
            "=================================================="
        )


        # ----------------------------------------------------
        # YENI CACHE
        #
        # ONEMLI:
        # Eski cache'i burada direkt silmiyoruz.
        #
        # Yahoo'dan basarili gelenler new_cache'e giriyor.
        # Daha sonra cache.update(new_cache) ile
        # sadece basarili veriler yenileniyor.
        # ----------------------------------------------------

        new_cache = {}

        completed = 0


        # ----------------------------------------------------
        # PARALEL YAHOO ISTEKLERI
        # ----------------------------------------------------

        with ThreadPoolExecutor(
            max_workers=WORKERS
        ) as executor:


            futures = {

                executor.submit(
                    fetch_one,
                    s
                ): s

                for s in SYMBOLS
            }


            for future in as_completed(
                futures
            ):

                symbol = futures[
                    future
                ]

                completed += 1


                try:

                    item = future.result()

                except Exception as e:

                    print(
                        f"YALCIN PRO - "
                        f"{symbol}: "
                        f"FUTURE HATASI "
                        f"{repr(e)}"
                    )

                    item = None


                # ------------------------------------------------
                # BASARILI VERI
                # ------------------------------------------------

                if item:

                    new_cache[
                        symbol
                    ] = item


                # ------------------------------------------------
                # ILERLEME LOG
                # ------------------------------------------------

                if (
                    completed % 25 == 0
                    or completed == total
                ):

                    print(
                        f"YALCIN PRO - YAHOO: "
                        f"{completed}/{total} | "
                        f"YENI VERI={len(new_cache)}"
                    )


        # ----------------------------------------------------
        # CACHE GUNCELLE
        # ----------------------------------------------------

        if new_cache:

            cache.update(
                new_cache
            )


        # ----------------------------------------------------
        # EKSIK SAYISI
        # ----------------------------------------------------

        missing = max(
            total - len(cache),
            0
        )


        # ----------------------------------------------------
        # SON REFRESH BILGISI
        # ----------------------------------------------------

        last_refresh.update({

            "timestamp": time.time(),

            "updated": len(new_cache),

            "missing": missing,

            "total": total,

            "status": (
                "guncel"
                if new_cache
                else "veri_alinamadi"
            ),
        })


        # ----------------------------------------------------
        # LOG
        # ----------------------------------------------------

        print(
            ""
        )

        print(
            "=================================================="
        )

        print(
            f"YALCIN PRO - CACHE GUNCELLENDI"
        )

        print(
            f"CACHE       : {len(cache)}/{total}"
        )

        print(
            f"YENI VERI   : {len(new_cache)}"
        )

        print(
            f"MISSING     : {missing}"
        )

        print(
            f"SAAT        : {now_text()}"
        )

        print(
            "=================================================="
        )

        print(
            ""
        )


        return len(cache)


    finally:

        refresh_lock.release()


# ============================================================
# ARKA PLAN OTOMATIK YENILEME
# ============================================================

def background_loop():

    print(
        "YALCIN PRO - "
        "ARKA PLAN BASLADI"
    )

    print(
        f"YALCIN PRO - "
        f"OTOMATIK YENILEME: "
        f"{REFRESH_SECONDS} SANİYE"
    )


    while True:

        try:

            # İnternetten güncel BIST sembollerini periyodik kontrol et.
            refresh_symbols_from_online_source()


            # ------------------------------------------------
            # SEMBOL LISTESINI KONTROL ET
            # ------------------------------------------------
            # JSON dosyasına yeni bir hisse eklenmişse sunucu
            # yeniden başlatılmadan listeye alınır.
            refresh_symbols_from_file()

            # ------------------------------------------------
            # Ilk calismada hemen veri al
            # ------------------------------------------------

            refresh_all(
                force=(not cache)
            )


        except Exception as e:

            print(
                "YALCIN PRO - "
                "ARKA PLAN HATASI:",
                repr(e)
            )


        # ----------------------------------------------------
        # Bir sonraki yenilemeye kadar bekle
        # ----------------------------------------------------

        time.sleep(
            REFRESH_SECONDS
        )


# ============================================================
# ANA SAYFA
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success": True,

        "status": "online",

        "service": "Yalcin Pro BIST",

        "source": "Yahoo Finance Chart",

        "delay": (
            "Yahoo tarafindaki mevcut "
            "piyasa gecikmesi"
        ),

        "symbols": len(SYMBOLS),

        "target": TARGET,

        "data": len(cache),

        "lastRefresh": last_refresh,
    })


# ============================================================
# HEALTH
# ============================================================

@app.route("/health")
def health():

    refresh_symbols_from_file()

    return jsonify({

        "success": True,

        "status": "online",

        "serverTime": now_text(),

        "symbols": len(SYMBOLS),

        "target": TARGET,

        "data": len(cache),

        "lastRefresh": last_refresh,
    })


# ============================================================
# STATS
# ============================================================

@app.route("/stats")
def stats():

    refresh_symbols_from_file()

    return jsonify({

        "success": True,

        "symbols": len(SYMBOLS),

        "target": TARGET,

        "cached": len(cache),

        "missing": max(
            len(SYMBOLS) - len(cache),
            0
        ),

        "lastRefresh": last_refresh,
    })


# ============================================================
# SEMBOLLER
# ============================================================

@app.route("/symbols")
def symbols():

    refresh_symbols_from_file()

    return jsonify({

        "success": True,

        "count": len(SYMBOLS),

        "symbols": SYMBOLS,
    })


# ============================================================
# TUM HISSeler
# ============================================================

@app.route("/all")
def all_stocks():

    # Online sembol taramasını HTTP isteği içinde çalıştırma.
    refresh_symbols_from_file()

    # --------------------------------------------------------
    # ONEMLI DUZELTME
    #
    # Eskiden burada sadece:
    #
    #     if not cache:
    #
    # vardi.
    #
    # Bu nedenle cache dolu oldugu surece /all
    # Yahoo'ya yeni istek yapmiyordu.
    #
    # Artik cache'in yasini kontrol ediyoruz.
    # --------------------------------------------------------

    age = (
        time.time()
        - last_refresh["timestamp"]
    )


    # --------------------------------------------------------
    # Cache bos ise
    # VEYA
    # Cache REFRESH_SECONDS'tan eskiyse
    # yeni veri cek.
    # --------------------------------------------------------

    if (
        not cache
        or age >= REFRESH_SECONDS
    ):

        print(
            ""
        )

        print(
            "YALCIN PRO - /all "
            "YENI VERI ISTEDI"
        )

        print(
            f"CACHE       : {len(cache)}"
        )

        print(
            f"CACHE YASI  : {age:.1f} saniye"
        )

        print(
            f"LIMITE      : {REFRESH_SECONDS} saniye"
        )


        def _background_refresh():
            try:
                refresh_all(force=True)
            except Exception as e:
                print(
                    "YALCIN PRO - /all ARKA PLAN REFRESH HATASI:",
                    repr(e)
                )

        threading.Thread(
            target=_background_refresh,
            daemon=True,
            name="yalcinpro-all-refresh"
        ).start()


    # --------------------------------------------------------
    # SYMBOLS SIRASINI KORUYARAK DATA OLUSTUR
    # --------------------------------------------------------

    data = [

        cache[s]

        for s in SYMBOLS

        if s in cache
    ]


    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    return jsonify({

        "success": True,

        "status": "online",

        "serverTime": now_text(),

        "symbols": len(SYMBOLS),

        "target": TARGET,

        "count": len(data),

        "data": data,

        "lastRefresh": last_refresh,
    })


# ============================================================
# TEK HISSE
# ============================================================

@app.route("/stock/<symbol>")
def stock(symbol):

    # --------------------------------------------------------
    # Sembol temizle
    # --------------------------------------------------------

    symbol = (
        symbol
        .upper()
        .replace(".IS", "")
        .strip()
    )


    # --------------------------------------------------------
    # Once cache'e bak
    # --------------------------------------------------------

    item = cache.get(
        symbol
    )


    # --------------------------------------------------------
    # Cache'de yoksa Yahoo'dan cek
    # --------------------------------------------------------

    if item is None:

        print(
            f"YALCIN PRO - "
            f"/stock/{symbol} "
            f"YAHOO'DAN CEKILIYOR"
        )

        item = fetch_one(
            symbol
        )


        if item:

            cache[
                symbol
            ] = item


    # --------------------------------------------------------
    # BULUNAMADI
    # --------------------------------------------------------

    if item is None:

        return jsonify({

            "success": False,

            "symbol": symbol,

            "data": None,

        }), 404


    # --------------------------------------------------------
    # SONUC
    # --------------------------------------------------------

    return jsonify({

        "success": True,

        "status": "online",

        "serverTime": now_text(),

        "data": item,
    })


# ============================================================
# MANUEL YENILE
# ============================================================

@app.route("/refresh")
def manual_refresh():

    print(
        ""
    )

    print(
        "=================================================="
    )

    print(
        "YALCIN PRO - MANUEL REFRESH ISTENDI"
    )

    print(
        "=================================================="
    )


    # --------------------------------------------------------
    # FORCE TRUE
    #
    # 60 saniyeyi beklemeden yeni veri ceker.
    # --------------------------------------------------------

    count = refresh_all(
        force=True
    )


    return jsonify({

        "success": count > 0,

        "status": last_refresh[
            "status"
        ],

        "data": count,

        "target": len(SYMBOLS),

        "lastRefresh": last_refresh,
    })


# ============================================================
# UYGULAMA BASLANGICI
# ============================================================

if __name__ == "__main__":

    # --------------------------------------------------------
    # Arka plan thread
    # --------------------------------------------------------

    t = threading.Thread(

        target=background_loop,

        daemon=True,
    )


    t.start()


    # --------------------------------------------------------
    # PORT
    # --------------------------------------------------------

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )


    # --------------------------------------------------------
    # FLASK
    # --------------------------------------------------------

    print(
        ""
    )

    print(
        "=================================================="
    )

    print(
        "YALCIN PRO BIST SERVER"
    )

    print(
        f"PORT       : {port}"
    )

    print(
        f"SEMBOL     : {len(SYMBOLS)}"
    )

    print(
        f"HEDEF      : {TARGET}"
    )

    print(
        f"REFRESH    : {REFRESH_SECONDS} saniye"
    )

    print(
        "=================================================="
    )

    print(
        ""
    )


    app.run(

        host="0.0.0.0",

        port=port,

        debug=False,

        threaded=True,
    )
