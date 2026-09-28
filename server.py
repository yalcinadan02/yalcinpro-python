import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# YALCIN PRO BIST SERVER
# ============================================================
#
# ANA MANTIK
#
# 1) Mevcut yerel sembol listesi korunur.
# 2) Yahoo 404 geldi diye sembol SILINMEZ.
# 3) Online liste eksik/şüpheli gelirse mevcut liste korunur.
# 4) Yeni semboller güvenli şekilde eklenebilir.
# 5) Gerçek sembol değişiklikleri için şirket adı bilgisi tutulur.
# 6) KAP güncel liste kontrolü yapılır.
# 7) Liste tek kontrolde büyük oranda küçülürse değişiklik uygulanmaz.
#
# ============================================================


# ============================================================
# AYARLAR
# ============================================================

TARGET = 0

SYMBOL_FILE = "yalcin_pro_active_symbols.json"

SYMBOL_META_FILE = "yalcin_pro_symbol_meta.json"

SYMBOL_HISTORY_FILE = "yalcin_pro_symbol_history.json"


# ============================================================
# RESMI KAP KAYNAKLARI
# ============================================================

KAP_COMPANIES_URL = "https://www.kap.org.tr/tr/bist-sirketler"

KAP_BIST_ALL_URL = "https://kap.org.tr/tr/Pazarlar"


# Eski yardımcı kaynak.
# SADECE KAP alınamazsa yardımcı/fallback olarak kullanılır.
GITHUB_SYMBOL_SOURCE_URL = (
    "https://raw.githubusercontent.com/ahmeterenodaci/"
    "Istanbul-Stock-Exchange--BIST--including-symbols-and-logos/"
    "main/bist.json"
)


# ============================================================
# SEMBOL KONTROL SÜRESİ
# ============================================================

# 6 saatte bir sembol kaynağı kontrol edilir.
SYMBOL_SOURCE_REFRESH_SECONDS = 6 * 60 * 60

last_symbol_source_check = 0


# ============================================================
# YAHOO AYARLARI
# ============================================================

WORKERS = 6

REQUEST_TIMEOUT = 15

RETRY_COUNT = 2

REFRESH_SECONDS = 60


YAHOO_URL = (
    "https://query1.finance.yahoo.com/"
    "v8/finance/chart/{}.IS"
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/153 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/json,"
        "text/plain,*/*"
    ),
})


# ============================================================
# CACHE
# ============================================================

cache = {}

last_refresh = {
    "timestamp": 0,
    "updated": 0,
    "missing": 0,
    "total": 0,
    "status": "baslatiliyor",
}


refresh_lock = threading.Lock()

symbols_lock = threading.Lock()


# ============================================================
# YARDIMCI
# ============================================================

def now_text():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def normalize_symbol(value):
    if not isinstance(value, str):
        return None

    s = (
        value
        .strip()
        .upper()
        .replace(".IS", "")
    )

    if not s:
        return None

    if not (
        2 <= len(s) <= 8
        and s.isalnum()
    ):
        return None

    return s


# ============================================================
# JSON YAZ
# ============================================================

def write_json_file(filename, data):

    path = os.path.join(
        os.path.dirname(__file__),
        filename
    )

    temp_path = path + ".tmp"

    try:

        with open(
            temp_path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temp_path,
            path
        )

        return True

    except Exception as e:

        print(
            f"YALCIN PRO - JSON YAZMA HATASI "
            f"{filename}: {repr(e)}"
        )

        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass

        return False


# ============================================================
# JSON OKU
# ============================================================

def read_json_file(filename, default):

    path = os.path.join(
        os.path.dirname(__file__),
        filename
    )

    if not os.path.exists(path):
        return default

    try:

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception as e:

        print(
            f"YALCIN PRO - JSON OKUMA HATASI "
            f"{filename}: {repr(e)}"
        )

        return default


# ============================================================
# ANA SEMBOL DOSYASI
# ============================================================

def load_symbols():

    obj = read_json_file(
        SYMBOL_FILE,
        []
    )

    found = []

    def add(value):

        symbol = normalize_symbol(value)

        if symbol and symbol not in found:
            found.append(symbol)

    if isinstance(obj, list):

        for item in obj:

            if isinstance(item, str):

                add(item)

            elif isinstance(item, dict):

                add(
                    item.get("symbol")
                    or item.get("sembol")
                    or item.get("code")
                )

    elif isinstance(obj, dict):

        for key in (
            "symbols",
            "semboller",
            "data",
            "stocks"
        ):

            value = obj.get(key)

            if isinstance(value, list):

                for item in value:

                    if isinstance(item, str):
                        add(item)

                    elif isinstance(item, dict):

                        add(
                            item.get("symbol")
                            or item.get("sembol")
                            or item.get("code")
                        )

                break

    print(
        f"YALCIN PRO - AKTIF SEMBOL: "
        f"{len(found)} / {TARGET}"
    )

    if TARGET > 0:
        return found[:TARGET]

    return found


# ============================================================
# ŞİRKET META VERİSİ
# ============================================================

def load_symbol_meta():

    obj = read_json_file(
        SYMBOL_META_FILE,
        {}
    )

    if isinstance(obj, dict):
        return obj

    return {}


symbol_meta = load_symbol_meta()


# ============================================================
# GEÇMİŞ
# ============================================================

def load_symbol_history():

    obj = read_json_file(
        SYMBOL_HISTORY_FILE,
        {}
    )

    if isinstance(obj, dict):
        return obj

    return {}


symbol_history = load_symbol_history()


# ============================================================
# SEMBOLLER
# ============================================================

SYMBOLS = load_symbols()


# ============================================================
# KAP HTML'DEN SEMBOL / ŞİRKET ADI ÇIKAR
# ============================================================

def parse_kap_companies_html(html):

    result = {}

    if not html:
        return result

    # --------------------------------------------------------
    # Önce klasik tablo satırlarını dene.
    # --------------------------------------------------------

    patterns = [

        # Kod + şirket adı
        r'<td[^>]*>\s*'
        r'([A-Z0-9]{2,8})'
        r'\s*</td>\s*'
        r'<td[^>]*>\s*'
        r'([^<]{3,250})',

        # Link içinde kod
        r'<a[^>]*>\s*'
        r'([A-Z0-9]{2,8})'
        r'\s*</a>\s*'
        r'<[^>]+>\s*'
        r'([^<]{3,250})',

    ]

    for pattern in patterns:

        try:

            matches = re.findall(
                pattern,
                html,
                flags=re.IGNORECASE
            )

            for code, name in matches:

                symbol = normalize_symbol(code)

                if not symbol:
                    continue

                clean_name = re.sub(
                    r"\s+",
                    " ",
                    name
                ).strip()

                if len(clean_name) < 3:
                    continue

                result[symbol] = clean_name

        except Exception:
            pass

    # --------------------------------------------------------
    # KAP HTML'sinde data-code benzeri alanları da dene.
    # --------------------------------------------------------

    code_patterns = [
        r'data-code=["\']([A-Z0-9]{2,8})["\']',
        r'data-symbol=["\']([A-Z0-9]{2,8})["\']',
    ]

    for pattern in code_patterns:

        try:

            matches = re.findall(
                pattern,
                html,
                flags=re.IGNORECASE
            )

            for code in matches:

                symbol = normalize_symbol(code)

                if symbol and symbol not in result:
                    result[symbol] = ""

        except Exception:
            pass

    return result


# ============================================================
# KAP GÜNCEL SEMBOLLERİ
# ============================================================

def fetch_kap_symbols():

    urls = [
        KAP_COMPANIES_URL,
        KAP_BIST_ALL_URL,
    ]

    best = {}

    for url in urls:

        try:

            print(
                "YALCIN PRO - KAP KONTROL:",
                url
            )

            r = session.get(
                url,
                timeout=REQUEST_TIMEOUT
            )

            if r.status_code != 200:

                print(
                    "YALCIN PRO - KAP HTTP:",
                    r.status_code
                )

                continue

            parsed = parse_kap_companies_html(
                r.text
            )

            if len(parsed) > len(best):
                best = parsed

            print(
                f"YALCIN PRO - KAP BULUNAN: "
                f"{len(parsed)}"
            )

        except Exception as e:

            print(
                "YALCIN PRO - KAP HATA:",
                repr(e)
            )

    return best


# ============================================================
# GITHUB FALLBACK
# ============================================================

def fetch_github_symbols():

    try:

        r = session.get(
            GITHUB_SYMBOL_SOURCE_URL,
            timeout=REQUEST_TIMEOUT
        )

        if r.status_code != 200:

            print(
                "YALCIN PRO - GITHUB SYMBOL HTTP:",
                r.status_code
            )

            return {}

        payload = r.json()

        result = {}

        if isinstance(payload, list):

            for item in payload:

                if isinstance(item, str):

                    symbol = normalize_symbol(
                        item
                    )

                    if symbol:
                        result[symbol] = ""

                elif isinstance(item, dict):

                    symbol = normalize_symbol(
                        item.get("symbol")
                        or item.get("sembol")
                        or item.get("code")
                    )

                    if symbol:
                        result[symbol] = (
                            item.get("name")
                            or item.get("company")
                            or ""
                        )

        elif isinstance(payload, dict):

            for key in (
                "symbols",
                "semboller",
                "data",
                "stocks"
            ):

                value = payload.get(key)

                if not isinstance(
                    value,
                    list
                ):
                    continue

                for item in value:

                    if isinstance(item, str):

                        symbol = normalize_symbol(
                            item
                        )

                        if symbol:
                            result[symbol] = ""

                    elif isinstance(item, dict):

                        symbol = normalize_symbol(
                            item.get("symbol")
                            or item.get("sembol")
                            or item.get("code")
                        )

                        if symbol:

                            result[symbol] = (
                                item.get("name")
                                or item.get("company")
                                or ""
                            )

                break

        print(
            f"YALCIN PRO - GITHUB SYMBOL: "
            f"{len(result)}"
        )

        return result

    except Exception as e:

        print(
            "YALCIN PRO - GITHUB HATA:",
            repr(e)
        )

        return {}


# ============================================================
# ONLINE SEMBOL KAYNAĞI
# ============================================================

def fetch_online_symbols():

    kap = fetch_kap_symbols()

    if kap:

        print(
            f"YALCIN PRO - ONLINE ANA KAYNAK: KAP"
            f" | {len(kap)}"
        )

        return kap

    fallback = fetch_github_symbols()

    if fallback:

        print(
            "YALCIN PRO - KAP YOK, "
            "GITHUB FALLBACK KULLANILIYOR"
        )

        return fallback

    print(
        "YALCIN PRO - ONLINE SEMBOL KAYNAGI YOK"
    )

    return {}


# ============================================================
# SEMBOL DEĞİŞİKLİĞİ İÇİN GÜVENLİ KONTROL
# ============================================================

def apply_symbol_source_update(
    online_meta,
    force=False
):

    global SYMBOLS
    global symbol_meta
    global symbol_history

    if not online_meta:
        print(
            "YALCIN PRO - ONLINE LISTE BOS"
        )
        print(
            "YALCIN PRO - MEVCUT LISTE KORUNUYOR"
        )
        return False

    online_set = set(
        online_meta.keys()
    )

    with symbols_lock:

        old_symbols = list(SYMBOLS)

        old_set = set(old_symbols)

        added = sorted(
            online_set - old_set
        )

        missing_from_online = sorted(
            old_set - online_set
        )

        # ----------------------------------------------------
        # ÇOK ÖNEMLİ KORUMA
        #
        # Online kaynak 537, yerel liste 671 ise
        # 134 sembolü tek seferde silme.
        #
        # Çünkü online kaynak eksik olabilir.
        # ----------------------------------------------------

        if (
            missing_from_online
            and len(online_set)
            < max(
                1,
                int(len(old_set) * 0.90)
            )
            and not force
        ):

            print(
                "================================================="
            )

            print(
                "YALCIN PRO - ONLINE LISTE EKSIK"
            )

            print(
                f"YALCIN PRO - ESKI={len(old_set)}"
            )

            print(
                f"YALCIN PRO - ONLINE={len(online_set)}"
            )

            print(
                f"YALCIN PRO - POTANSIYEL CIKAN="
                f"{len(missing_from_online)}"
            )

            print(
                "YALCIN PRO - TOPLU SILME YAPILMADI"
            )

            print(
                "YALCIN PRO - MEVCUT LISTE KORUNUYOR"
            )

            print(
                "================================================="
            )

            # Yeni hisseleri yine ekleyebiliriz.
            if added:

                for symbol in added:

                    if symbol not in SYMBOLS:
                        SYMBOLS.append(
                            symbol
                        )

                for symbol in added:

                    symbol_meta[
                        symbol
                    ] = {
                        "name": online_meta.get(
                            symbol,
                            ""
                        ),
                        "firstSeen":
                            now_text(),
                    }

                write_json_file(
                    SYMBOL_FILE,
                    SYMBOLS
                )

                write_json_file(
                    SYMBOL_META_FILE,
                    symbol_meta
                )

                print(
                    "YALCIN PRO - "
                    f"YENI SEMBOL EKLENDI: "
                    f"{len(added)}"
                )

            return bool(added)

        # ----------------------------------------------------
        # Kaynak makul büyüklükteyse yeni sembolleri ekle.
        # ----------------------------------------------------

        for symbol in added:

            if symbol not in SYMBOLS:

                SYMBOLS.append(
                    symbol
                )

                symbol_meta[
                    symbol
                ] = {
                    "name": online_meta.get(
                        symbol,
                        ""
                    ),
                    "firstSeen":
                        now_text(),
                }

        # ----------------------------------------------------
        # Eksilen semboller:
        #
        # Hemen silme.
        #
        # İlk kontrolde history'e yaz.
        # İkinci ayrı kontrolde de yoksa sil.
        # ----------------------------------------------------

        confirmed_removed = []

        for symbol in missing_from_online:

            history = symbol_history.get(
                symbol,
                {}
            )

            count = int(
                history.get(
                    "missingCount",
                    0
                )
            )

            last_seen = history.get(
                "lastSeenOnline"
            )

            # Online'da tekrar bulunduysa sıfırla.
            if symbol in online_set:

                symbol_history.pop(
                    symbol,
                    None
                )

                continue

            count += 1

            symbol_history[
                symbol
            ] = {
                "missingCount": count,
                "firstMissing": (
                    history.get(
                        "firstMissing"
                    )
                    or now_text()
                ),
                "lastMissing": now_text(),
                "lastSeenOnline": last_seen,
                "name": (
                    symbol_meta
                    .get(symbol, {})
                    .get("name", "")
                ),
            }

            # En az 2 başarılı online kontrolde
            # bulunmazsa kaldır.
            if count >= 2:

                confirmed_removed.append(
                    symbol
                )

        # ----------------------------------------------------
        # Kaldırılanları gerçekten çıkar.
        # ----------------------------------------------------

        if confirmed_removed:

            remove_set = set(
                confirmed_removed
            )

            SYMBOLS = [
                s
                for s in SYMBOLS
                if s not in remove_set
            ]

            for symbol in confirmed_removed:

                symbol_history.pop(
                    symbol,
                    None
                )

                symbol_meta.pop(
                    symbol,
                    None
                )

        # ----------------------------------------------------
        # Yeni online isim bilgilerini güncelle.
        # ----------------------------------------------------

        for symbol, name in online_meta.items():

            if symbol not in symbol_meta:

                symbol_meta[
                    symbol
                ] = {
                    "name": name,
                    "firstSeen":
                        now_text(),
                }

            elif name:

                old_name = (
                    symbol_meta[
                        symbol
                    ].get("name", "")
                )

                if old_name != name:

                    symbol_meta[
                        symbol
                    ]["previousName"] = old_name

                    symbol_meta[
                        symbol
                    ]["name"] = name

                    symbol_meta[
                        symbol
                    ]["lastNameChange"] = (
                        now_text()
                    )

        # ----------------------------------------------------
        # Dosyaları kaydet.
        # ----------------------------------------------------

        write_json_file(
            SYMBOL_FILE,
            SYMBOLS
        )

        write_json_file(
            SYMBOL_META_FILE,
            symbol_meta
        )

        write_json_file(
            SYMBOL_HISTORY_FILE,
            symbol_history
        )

        print(
            "================================================="
        )

        print(
            "YALCIN PRO - SEMBOL KONTROL SONUCU"
        )

        print(
            f"ESKI             : {len(old_set)}"
        )

        print(
            f"ONLINE           : {len(online_set)}"
        )

        print(
            f"YENI             : {len(added)}"
        )

        print(
            f"ONLINE'DA YOK    : "
            f"{len(missing_from_online)}"
        )

        print(
            f"GERCEKTEN SILINEN: "
            f"{len(confirmed_removed)}"
        )

        print(
            f"SON LISTE        : "
            f"{len(SYMBOLS)}"
        )

        if added:

            print(
                "YENI SEMBOLLER: "
                + ", ".join(added)
            )

        if confirmed_removed:

            print(
                "SILINEN SEMBOLLER: "
                + ", ".join(
                    confirmed_removed
                )
            )

        print(
            "================================================="
        )

        return bool(
            added
            or confirmed_removed
        )


# ============================================================
# ONLINE SEMBOL REFRESH
# ============================================================

def refresh_symbols_from_online_source(
    force=False
):

    global last_symbol_source_check

    now = time.time()

    if (
        not force
        and last_symbol_source_check > 0
        and (
            now
            - last_symbol_source_check
        ) < SYMBOL_SOURCE_REFRESH_SECONDS
    ):
        return False

    print("")
    print(
        "================================================="
    )

    print(
        "YALCIN PRO - SEMBOL KAYNAGI KONTROLU"
    )

    print(
        f"YALCIN PRO - SAAT: {now_text()}"
    )

    online = fetch_online_symbols()

    last_symbol_source_check = now

    if not online:

        print(
            "YALCIN PRO - ONLINE KONTROL BASARISIZ"
        )

        print(
            "YALCIN PRO - MEVCUT SEMBOL LISTESI KORUNDU"
        )

        print(
            "================================================="
        )

        return False

    changed = apply_symbol_source_update(
        online,
        force=force
    )

    return changed


# ============================================================
# DOSYADAN SEMBOL YENILE
# ============================================================

def refresh_symbols_from_file():

    global SYMBOLS

    new_symbols = load_symbols()

    if not new_symbols:
        return False

    with symbols_lock:

        old_set = set(
            SYMBOLS
        )

        new_set = set(
            new_symbols
        )

        added = sorted(
            new_set - old_set
        )

        removed = sorted(
            old_set - new_set
        )

        if not added and not removed:
            return False

        SYMBOLS = new_symbols

        print(
            "YALCIN PRO - DOSYADAN SEMBOL DEGISIKLIGI"
        )

        print(
            f"ESKI={len(old_set)} "
            f"YENI={len(new_set)}"
        )

        if added:

            print(
                "EKLENEN: "
                + ", ".join(added)
            )

        if removed:

            print(
                "DOSYADAN CIKAN: "
                + ", ".join(removed)
            )

        return True


# ============================================================
# TEK HISSE VERISI
# ============================================================

def fetch_one(symbol):

    url = YAHOO_URL.format(
        symbol
    )

    for attempt in range(
        RETRY_COUNT + 1
    ):

        try:

            r = session.get(
                url,
                params={
                    "range": "5d",
                    "interval": "1d",
                    "events": "div,splits",
                    "includeAdjustedClose":
                        "true",
                },
                timeout=REQUEST_TIMEOUT,
            )

            if r.status_code == 429:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"429 RATE LIMIT | "
                    f"DENEME={attempt + 1}"
                )

                time.sleep(
                    1.5 * (
                        attempt + 1
                    )
                )

                continue

            if r.status_code != 200:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"HTTP {r.status_code}"
                )

                # ÖNEMLİ:
                # 404 burada sadece veri yok demektir.
                # SEMBOLÜ SİLME.
                return None

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

            meta = result[0].get(
                "meta",
                {}
            )

            price = meta.get(
                "regularMarketPrice"
            )

            previous = meta.get(
                "previousClose"
            )

            indicators = result[0].get(
                "indicators",
                {}
            )

            quote = indicators.get(
                "quote",
                []
            )

            closes = []

            if quote:

                raw_closes = quote[0].get(
                    "close",
                    []
                )

                closes = [
                    float(x)
                    for x in raw_closes
                    if x is not None
                ]

            # ------------------------------------------------
            # Fiyat fallback
            # ------------------------------------------------

            if (
                price is None
                and closes
            ):
                price = closes[-1]

            if price is None:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"FIYAT BULUNAMADI"
                )

                return None

            price = float(
                price
            )

            # ------------------------------------------------
            # Previous fallback
            # ------------------------------------------------

            if (
                previous is None
                and len(closes) >= 2
            ):

                previous = closes[-2]

            if previous is not None:

                previous = float(
                    previous
                )

            # =================================================
            # DUNYH
            # =================================================

            if symbol == "DUNYH":

                if len(closes) >= 2:

                    previous = float(
                        closes[-2]
                    )

                    if abs(
                        previous - 109.10
                    ) < 0.01:

                        previous = 44.56

                        print(
                            "YALCIN PRO - "
                            "DUNYH SABIT "
                            "DUZELTME | "
                            f"PRICE={price} | "
                            f"PREVIOUS={previous}"
                        )

            # ------------------------------------------------
            # Değişim yüzdesi
            # ------------------------------------------------

            change = None

            if previous not in (
                None,
                0
            ):

                change = (
                    (
                        price
                        - previous
                    )
                    / previous
                ) * 100.0

            if previous is None:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"ONCEKI KAPANIS "
                    f"BULUNAMADI | "
                    f"FIYAT={price}"
                )

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
                    else 0.0
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
            }

            return item

        except Exception as e:

            print(
                f"YALCIN PRO - {symbol}: "
                f"HATA | {repr(e)}"
            )

            if (
                attempt
                < RETRY_COUNT
            ):

                time.sleep(
                    0.8 * (
                        attempt + 1
                    )
                )

            else:

                return None

    return None


# ============================================================
# TÜM HİSSELERİ YENİLE
# ============================================================

def refresh_all(force=False):

    global cache
    global last_refresh

    if not refresh_lock.acquire(
        blocking=False
    ):

        print(
            "YALCIN PRO - "
            "REFRESH ZATEN CALISIYOR"
        )

        return len(cache)

    try:

        if (
            not force
            and cache
        ):

            age = (
                time.time()
                - last_refresh[
                    "timestamp"
                ]
            )

            if age < REFRESH_SECONDS:

                return len(cache)

        total = len(SYMBOLS)

        if total == 0:

            last_refresh.update({
                "status":
                    "sembol_yok",
                "timestamp":
                    time.time(),
                "updated":
                    0,
                "total":
                    0,
                "missing":
                    0,
            })

            return 0

        last_refresh.update({
            "status":
                "veri_aliniyor",
            "total":
                total,
            "missing":
                total,
            "updated":
                0,
        })

        print("")
        print(
            "=================================================="
        )

        print(
            "YALCIN PRO - YAHOO YENILEME BASLADI"
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

        new_cache = {}

        completed = 0

        symbols_snapshot = list(
            SYMBOLS
        )

        with ThreadPoolExecutor(
            max_workers=WORKERS
        ) as executor:

            futures = {
                executor.submit(
                    fetch_one,
                    s
                ): s
                for s in symbols_snapshot
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

                if item:

                    new_cache[
                        symbol
                    ] = item

                if (
                    completed % 25 == 0
                    or completed == total
                ):

                    print(
                        f"YALCIN PRO - YAHOO: "
                        f"{completed}/{total} | "
                        f"YENI VERI="
                        f"{len(new_cache)}"
                    )

        # Başarılı veriler güncellenir.
        if new_cache:

            cache.update(
                new_cache
            )

        missing = max(
            total - len(
                [
                    s
                    for s in symbols_snapshot
                    if s in cache
                ]
            ),
            0
        )

        last_refresh.update({

            "timestamp":
                time.time(),

            "updated":
                len(new_cache),

            "missing":
                missing,

            "total":
                total,

            "status":
                (
                    "guncel"
                    if new_cache
                    else
                    "veri_alinamadi"
                ),
        })

        print("")
        print(
            "=================================================="
        )

        print(
            "YALCIN PRO - CACHE GUNCELLENDI"
        )

        print(
            f"CACHE       : "
            f"{len(cache)}/{total}"
        )

        print(
            f"YENI VERI   : "
            f"{len(new_cache)}"
        )

        print(
            f"MISSING     : "
            f"{missing}"
        )

        print(
            f"SAAT        : "
            f"{now_text()}"
        )

        print(
            "=================================================="
        )

        return len(cache)

    finally:

        refresh_lock.release()


# ============================================================
# ARKA PLAN
# ============================================================

def background_loop():

    print(
        "YALCIN PRO - ARKA PLAN BASLADI"
    )

    print(
        f"YALCIN PRO - OTOMATIK YENILEME: "
        f"{REFRESH_SECONDS} SANİYE"
    )

    while True:

        try:

            # ------------------------------------------------
            # Sembol kaynağı kontrolü
            # ------------------------------------------------

            refresh_symbols_from_online_source()

            # ------------------------------------------------
            # Yerel dosya kontrolü
            # ------------------------------------------------

            refresh_symbols_from_file()

            # ------------------------------------------------
            # Veri yenileme
            # ------------------------------------------------

            refresh_all(
                force=(
                    not cache
                )
            )

        except Exception as e:

            print(
                "YALCIN PRO - "
                "ARKA PLAN HATASI:",
                repr(e)
            )

        time.sleep(
            REFRESH_SECONDS
        )


# ============================================================
# ANA
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success": True,

        "status": "online",

        "service":
            "Yalcin Pro BIST",

        "source":
            "Yahoo Finance Chart",

        "symbolSource":
            "KAP + güvenli fallback",

        "symbols":
            len(SYMBOLS),

        "target":
            TARGET,

        "data":
            len(cache),

        "lastRefresh":
            last_refresh,
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

        "serverTime":
            now_text(),

        "symbols":
            len(SYMBOLS),

        "target":
            TARGET,

        "data":
            len(cache),

        "lastRefresh":
            last_refresh,
    })


# ============================================================
# STATS
# ============================================================

@app.route("/stats")
def stats():

    refresh_symbols_from_file()

    return jsonify({

        "success": True,

        "symbols":
            len(SYMBOLS),

        "target":
            TARGET,

        "cached":
            len(cache),

        "missing":
            max(
                len(SYMBOLS)
                - len(cache),
                0
            ),

        "lastRefresh":
            last_refresh,
    })


# ============================================================
# SEMBOLLER
# ============================================================

@app.route("/symbols")
def symbols():

    refresh_symbols_from_file()

    return jsonify({

        "success": True,

        "count":
            len(SYMBOLS),

        "symbols":
            SYMBOLS,
    })


# ============================================================
# TÜM HİSSELER
# ============================================================

@app.route("/all")
def all_stocks():

    refresh_symbols_from_file()

    age = (
        time.time()
        - last_refresh[
            "timestamp"
        ]
    )

    if (
        not cache
        or age >= REFRESH_SECONDS
    ):

        print("")
        print(
            "YALCIN PRO - /all "
            "YENI VERI ISTEDI"
        )

        print(
            f"CACHE       : {len(cache)}"
        )

        print(
            f"CACHE YASI  : "
            f"{age:.1f} saniye"
        )

        print(
            f"LIMITE      : "
            f"{REFRESH_SECONDS} saniye"
        )

        def background_refresh():

            try:

                refresh_all(
                    force=True
                )

            except Exception as e:

                print(
                    "YALCIN PRO - "
                    "/all REFRESH HATASI:",
                    repr(e)
                )

        threading.Thread(
            target=background_refresh,
            daemon=True,
            name="yalcinpro-all-refresh"
        ).start()

    data = [

        cache[s]

        for s in SYMBOLS

        if s in cache
    ]

    return jsonify({

        "success": True,

        "status": "online",

        "serverTime":
            now_text(),

        "symbols":
            len(SYMBOLS),

        "target":
            TARGET,

        "count":
            len(data),

        "data":
            data,

        "lastRefresh":
            last_refresh,
    })


# ============================================================
# TEK HİSSE
# ============================================================

@app.route("/stock/<symbol>")
def stock(symbol):

    symbol = (
        symbol
        .upper()
        .replace(".IS", "")
        .strip()
    )

    item = cache.get(
        symbol
    )

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

    if item is None:

        return jsonify({

            "success": False,

            "symbol":
                symbol,

            "data":
                None,

        }), 404

    return jsonify({

        "success": True,

        "status":
            "online",

        "serverTime":
            now_text(),

        "data":
            item,
    })


# ============================================================
# MANUEL REFRESH
# ============================================================

@app.route("/refresh")
def manual_refresh():

    print("")
    print(
        "=================================================="
    )

    print(
        "YALCIN PRO - MANUEL REFRESH ISTENDI"
    )

    print(
        "=================================================="
    )

    # Önce sembol listesini kontrol et.
    refresh_symbols_from_online_source(
        force=True
    )

    count = refresh_all(
        force=True
    )

    return jsonify({

        "success":
            count > 0,

        "status":
            last_refresh[
                "status"
            ],

        "data":
            count,

        "target":
            len(SYMBOLS),

        "symbols":
            len(SYMBOLS),

        "lastRefresh":
            last_refresh,
    })


# ============================================================
# UYGULAMA BAŞLANGICI
# ============================================================

if __name__ == "__main__":

    t = threading.Thread(
        target=background_loop,
        daemon=True
    )

    t.start()

    port = int(
        os.environ.get(
            "PORT",
            "5000"
        )
    )

    print("")
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
        f"REFRESH    : "
        f"{REFRESH_SECONDS} saniye"
    )

    print(
        "=================================================="
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
    )
