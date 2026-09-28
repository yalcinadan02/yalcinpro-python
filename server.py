import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from flask import Flask, jsonify


# ============================================================
# YALCIN PRO BIST SERVER
# ============================================================

app = Flask(__name__)


# ============================================================
# AYARLAR
# ============================================================

# 0 = sembol sayisini sinirlama
TARGET = 0

# Yerel aktif sembol dosyasi
SYMBOL_FILE = "yalcin_pro_active_symbols.json"


# ============================================================
# OTOMATIK BIST SEMBOL KAYNAGI
# ============================================================

SYMBOL_SOURCE_URL = (
    "https://raw.githubusercontent.com/ahmeterenodaci/"
    "Istanbul-Stock-Exchange--BIST--including-symbols-and-logos/"
    "main/bist.json"
)

# Yeni sembol listesini saatte bir kontrol et.
#
# Android /all istegi geldiginde de kontrol yapilir.
# Boylece Render/Gunicorn arka plan thread'ine bagimli kalmaz.
SYMBOL_SOURCE_REFRESH_SECONDS = 60 * 60

last_symbol_source_check = 0


# ============================================================
# YAHOO AYARLARI
# ============================================================

WORKERS = 6

REQUEST_TIMEOUT = 15

RETRY_COUNT = 2

# Fiyat cache'i 60 saniyede yenilenir.
REFRESH_SECONDS = 60


# ============================================================
# YAHOO FINANCE
# ============================================================

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
    "missing": 0,
    "total": 0,
    "status": "baslatiliyor",
}


# ============================================================
# LOCKLAR
# ============================================================

refresh_lock = threading.Lock()

symbols_lock = threading.Lock()


# ============================================================
# ZAMAN
# ============================================================

def now_text():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ============================================================
# SEMBOL NORMALIZE
# ============================================================

def normalize_symbol(value):

    if not isinstance(value, str):
        return ""

    symbol = (
        value
        .strip()
        .upper()
        .replace(".IS", "")
        .replace(" ", "")
    )

    if not (
        2 <= len(symbol) <= 8
    ):
        return ""

    if not symbol.isalnum():
        return ""

    return symbol


# ============================================================
# SEMBOL EKLE
# ============================================================

def add_symbol_to_list(
    found,
    value
):

    symbol = normalize_symbol(
        value
    )

    if not symbol:
        return

    if symbol not in found:
        found.append(symbol)


# ============================================================
# YEREL SEMBOL DOSYASI
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


        # ----------------------------------------------------
        # LISTE
        # ----------------------------------------------------

        if isinstance(
            obj,
            list
        ):

            for item in obj:

                if isinstance(
                    item,
                    str
                ):

                    add_symbol_to_list(
                        found,
                        item
                    )

                elif isinstance(
                    item,
                    dict
                ):

                    add_symbol_to_list(
                        found,
                        item.get("symbol")
                        or item.get("sembol")
                        or item.get("code")
                        or item.get("kod")
                    )


        # ----------------------------------------------------
        # OBJE
        # ----------------------------------------------------

        elif isinstance(
            obj,
            dict
        ):

            for key in (
                "symbols",
                "semboller",
                "data",
                "stocks"
            ):

                value = obj.get(key)

                if isinstance(
                    value,
                    list
                ):

                    for item in value:

                        if isinstance(
                            item,
                            str
                        ):

                            add_symbol_to_list(
                                found,
                                item
                            )

                        elif isinstance(
                            item,
                            dict
                        ):

                            add_symbol_to_list(
                                found,
                                item.get("symbol")
                                or item.get("sembol")
                                or item.get("code")
                                or item.get("kod")
                            )

                    break


        if TARGET > 0:

            found = found[:TARGET]


        print(
            "YALCIN PRO - AKTIF SEMBOL:",
            len(found)
        )

        return found


    except Exception as e:

        print(
            "YALCIN PRO - SEMBOL OKUMA HATASI:",
            repr(e)
        )

        return []


# ============================================================
# SEMBOL DOSYASINI KAYDET
# ============================================================

def save_symbols():

    path = os.path.join(
        os.path.dirname(__file__),
        SYMBOL_FILE
    )

    try:

        with open(
            path,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                SYMBOLS,
                f,
                ensure_ascii=False,
                indent=2
            )

        print(
            "YALCIN PRO - SEMBOL DOSYASI GUNCELLENDI:",
            len(SYMBOLS)
        )

        return True

    except Exception as e:

        print(
            "YALCIN PRO - SEMBOL DOSYASI YAZMA HATASI:",
            repr(e)
        )

        return False


# ============================================================
# BASLANGIC SEMBOL LISTESI
# ============================================================

SYMBOLS = load_symbols()


# ============================================================
# ONLINE SEMBOL LISTESINI AL
# ============================================================

def fetch_online_symbols():

    try:

        print(
            "YALCIN PRO - ONLINE SEMBOL LISTESI KONTROL EDILIYOR"
        )

        response = session.get(
            SYMBOL_SOURCE_URL,
            timeout=15
        )


        if response.status_code != 200:

            print(
                "YALCIN PRO - ONLINE SEMBOL HTTP:",
                response.status_code
            )

            return []


        payload = response.json()

        found = []


        # ----------------------------------------------------
        # LISTE
        # ----------------------------------------------------

        if isinstance(
            payload,
            list
        ):

            for item in payload:

                if isinstance(
                    item,
                    str
                ):

                    add_symbol_to_list(
                        found,
                        item
                    )

                elif isinstance(
                    item,
                    dict
                ):

                    add_symbol_to_list(
                        found,
                        item.get("symbol")
                        or item.get("sembol")
                        or item.get("code")
                        or item.get("kod")
                    )


        # ----------------------------------------------------
        # OBJE
        # ----------------------------------------------------

        elif isinstance(
            payload,
            dict
        ):

            for key in (
                "symbols",
                "semboller",
                "data",
                "stocks"
            ):

                value = payload.get(key)

                if isinstance(
                    value,
                    list
                ):

                    for item in value:

                        if isinstance(
                            item,
                            str
                        ):

                            add_symbol_to_list(
                                found,
                                item
                            )

                        elif isinstance(
                            item,
                            dict
                        ):

                            add_symbol_to_list(
                                found,
                                item.get("symbol")
                                or item.get("sembol")
                                or item.get("code")
                                or item.get("kod")
                            )

                    break


        print(
            "YALCIN PRO - ONLINE SEMBOL SAYISI:",
            len(found)
        )

        return found


    except Exception as e:

        print(
            "YALCIN PRO - ONLINE SEMBOL KAYNAGI HATASI:",
            repr(e)
        )

        return []


# ============================================================
# ONLINE SEMBOLLERI GUNCELLE
# ============================================================

def refresh_symbols_from_online_source(
    force=False
):

    global SYMBOLS
    global last_symbol_source_check


    now = time.time()


    # --------------------------------------------------------
    # KONTROL ZAMANI GELMEDI
    # --------------------------------------------------------

    if (
        not force
        and last_symbol_source_check > 0
        and (
            now
            - last_symbol_source_check
        ) < SYMBOL_SOURCE_REFRESH_SECONDS
    ):

        return False


    online_symbols = (
        fetch_online_symbols()
    )


    last_symbol_source_check = now


    if not online_symbols:

        print(
            "YALCIN PRO - ONLINE SEMBOL LISTESI ALINAMADI"
        )

        return False


    added = []


    with symbols_lock:

        old_set = set(
            SYMBOLS
        )


        for symbol in online_symbols:

            if symbol not in old_set:

                added.append(
                    symbol
                )


        if added:

            SYMBOLS = (
                list(SYMBOLS)
                + added
            )


    # --------------------------------------------------------
    # YENI HISSELER
    # --------------------------------------------------------

    if added:

        print(
            "=================================================="
        )

        print(
            "YALCIN PRO - YENI HISSELER BULUNDU"
        )

        print(
            "YENI ADET:",
            len(added)
        )

        print(
            "TOPLAM:",
            len(SYMBOLS)
        )

        print(
            "YENI SEMBOLLER:",
            ", ".join(added)
        )

        print(
            "=================================================="
        )


        save_symbols()


        return True


    print(
        "YALCIN PRO - YENI HISSE YOK | TOPLAM:",
        len(SYMBOLS)
    )

    return False


# ============================================================
# DOSYADAKI SEMBOLLERI YENIDEN OKU
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
            new_set
            - old_set
        )

        removed = sorted(
            old_set
            - new_set
        )


        if (
            added
            or removed
        ):

            SYMBOLS = (
                new_symbols
            )


            print(
                "YALCIN PRO - SEMBOL DOSYASI DEGISTI"
            )

            print(
                "ESKI:",
                len(old_set)
            )

            print(
                "YENI:",
                len(new_set)
            )


            if added:

                print(
                    "EKLENEN:",
                    ", ".join(added)
                )


            if removed:

                print(
                    "CIKAN:",
                    ", ".join(removed)
                )


            return True


    return False


# ============================================================
# TEK HISSE VERISI
# ============================================================

def fetch_one(symbol):

    symbol = normalize_symbol(
        symbol
    )


    if not symbol:

        return None


    url = YAHOO_URL.format(
        symbol
    )


    for attempt in range(
        RETRY_COUNT + 1
    ):

        try:

            response = session.get(

                url,

                params={
                    "range": "5d",
                    "interval": "1d",
                    "events": "div,splits",
                    "includeAdjustedClose": "true",
                },

                timeout=REQUEST_TIMEOUT
            )


            # ------------------------------------------------
            # RATE LIMIT
            # ------------------------------------------------

            if response.status_code == 429:

                print(
                    "YALCIN PRO -",
                    symbol,
                    "429 RATE LIMIT"
                )

                time.sleep(
                    1.5
                    * (
                        attempt
                        + 1
                    )
                )

                continue


            # ------------------------------------------------
            # HTTP HATASI
            # ------------------------------------------------

            if response.status_code != 200:

                print(
                    "YALCIN PRO -",
                    symbol,
                    "HTTP",
                    response.status_code
                )

                return None


            # ------------------------------------------------
            # JSON
            # ------------------------------------------------

            payload = (
                response.json()
            )


            result = (
                payload
                .get("chart", {})
                .get("result", [])
            )


            if not result:

                print(
                    "YALCIN PRO -",
                    symbol,
                    "YAHOO RESULT BOS"
                )

                return None


            result = result[0]


            # ------------------------------------------------
            # META
            # ------------------------------------------------

            meta = result.get(
                "meta",
                {}
            )


            price = meta.get(
                "regularMarketPrice"
            )


            previous = meta.get(
                "previousClose"
            )


            # ------------------------------------------------
            # CHART
            # ------------------------------------------------

            indicators = result.get(
                "indicators",
                {}
            )


            quote = indicators.get(
                "quote",
                []
            )


            closes = []


            if quote:

                raw_closes = (
                    quote[0]
                    .get(
                        "close",
                        []
                    )
                )


                closes = [

                    float(value)

                    for value
                    in raw_closes

                    if value is not None
                ]


            # ------------------------------------------------
            # FIYAT FALLBACK
            # ------------------------------------------------

            if (
                price is None
                and closes
            ):

                price = (
                    closes[-1]
                )


            if price is None:

                print(
                    "YALCIN PRO -",
                    symbol,
                    "FIYAT BULUNAMADI"
                )

                return None


            price = float(
                price
            )


            # ------------------------------------------------
            # ONCEKI KAPANIS FALLBACK
            # ------------------------------------------------

            if (
                previous is None
                and len(closes) >= 2
            ):

                previous = (
                    closes[-2]
                )


            if previous is not None:

                previous = float(
                    previous
                )


            # =================================================
            # DUNYH OZEL DUZELTME
            # =================================================

            if symbol == "DUNYH":

                if len(closes) >= 2:

                    chart_previous = (
                        closes[-2]
                    )


                    if (
                        chart_previous
                        is not None
                        and chart_previous > 0
                    ):

                        previous = float(
                            chart_previous
                        )


                # Bilinen hatali Yahoo degeri
                if (
                    previous is not None
                    and abs(
                        previous
                        - 109.10
                    ) < 0.01
                ):

                    previous = 44.56


            # ------------------------------------------------
            # DEGISIM
            # ------------------------------------------------

            change = None


            if (
                previous is not None
                and previous != 0
            ):

                change = (

                    (
                        price
                        - previous
                    )
                    / previous

                ) * 100.0


            # ------------------------------------------------
            # SONUC
            # ------------------------------------------------

            item = {

                "sembol":
                    symbol,

                "kod":
                    symbol,

                "symbol":
                    symbol,

                "code":
                    symbol,


                "fiyat":
                    round(
                        price,
                        4
                    ),

                "price":
                    round(
                        price,
                        4
                    ),


                "oncekiKapanis":

                    (
                        round(
                            previous,
                            4
                        )

                        if previous
                        is not None

                        else None
                    ),


                "previousClose":

                    (
                        round(
                            previous,
                            4
                        )

                        if previous
                        is not None

                        else None
                    ),


                "degisimYuzde":

                    (
                        round(
                            change,
                            4
                        )

                        if change
                        is not None

                        else 0.0
                    ),


                "changePercent":

                    (
                        round(
                            change,
                            4
                        )

                        if change
                        is not None

                        else 0.0
                    ),


                "paraBirimi":
                    "TRY",

                "currency":
                    "TRY",
            }


            return item


        except Exception as e:

            print(
                "YALCIN PRO -",
                symbol,
                "HATA:",
                repr(e)
            )


            if (
                attempt
                < RETRY_COUNT
            ):

                time.sleep(
                    0.8
                    * (
                        attempt
                        + 1
                    )
                )

            else:

                return None


    return None


# ============================================================
# TÜM HİSSELERİ YENİLE
# ============================================================

def refresh_all(
    force=False
):

    global cache
    global last_refresh


    # --------------------------------------------------------
    # AYNI ANDA İKİ REFRESH ENGELLE
    # --------------------------------------------------------

    if not refresh_lock.acquire(
        blocking=False
    ):

        print(
            "YALCIN PRO - REFRESH ZATEN CALISIYOR"
        )

        return len(cache)


    try:

        # ----------------------------------------------------
        # CACHE KONTROL
        # ----------------------------------------------------

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


            if (
                age
                < REFRESH_SECONDS
            ):

                return len(cache)


        # ----------------------------------------------------
        # SEMBOLLER
        # ----------------------------------------------------

        with symbols_lock:

            current_symbols = (
                list(SYMBOLS)
            )


        total = len(
            current_symbols
        )


        if total == 0:

            last_refresh.update({

                "timestamp":
                    time.time(),

                "updated":
                    0,

                "missing":
                    0,

                "total":
                    0,

                "status":
                    "sembol_yok",
            })


            return 0


        print("")
        print(
            "=================================================="
        )

        print(
            "YALCIN PRO - YAHOO YENILEME BASLADI"
        )

        print(
            "HISSE SAYISI:",
            total
        )

        print(
            "WORKER:",
            WORKERS
        )

        print(
            "FORCE:",
            force
        )

        print(
            "=================================================="
        )


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


        # ----------------------------------------------------
        # YENI CACHE
        # ----------------------------------------------------

        new_cache = {}

        completed = 0


        # ----------------------------------------------------
        # PARALEL YAHOO
        # ----------------------------------------------------

        with ThreadPoolExecutor(

            max_workers=
                WORKERS

        ) as executor:


            futures = {

                executor.submit(
                    fetch_one,
                    symbol
                ):
                    symbol

                for symbol
                in current_symbols
            }


            for future in as_completed(
                futures
            ):

                symbol = (
                    futures[
                        future
                    ]
                )


                completed += 1


                try:

                    item = (
                        future.result()
                    )


                except Exception as e:

                    print(
                        "YALCIN PRO -",
                        symbol,
                        "FUTURE HATASI:",
                        repr(e)
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

                        "YALCIN PRO - YAHOO:",
                        f"{completed}/{total}",
                        "| YENI VERI=",
                        len(new_cache)
                    )


        # ----------------------------------------------------
        # CACHE GUNCELLE
        # ----------------------------------------------------

        if new_cache:

            cache.update(
                new_cache
            )


        # ----------------------------------------------------
        # MISSING
        # ----------------------------------------------------

        missing = max(

            total
            - len(cache),

            0
        )


        # ----------------------------------------------------
        # SON REFRESH
        # ----------------------------------------------------

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
            "CACHE:",
            f"{len(cache)}/{total}"
        )

        print(
            "YENI VERI:",
            len(new_cache)
        )

        print(
            "MISSING:",
            missing
        )

        print(
            "SAAT:",
            now_text()
        )

        print(
            "=================================================="
        )


        return len(cache)


    finally:

        refresh_lock.release()


# ============================================================
# ANA SAYFA
# ============================================================

@app.route("/")
def home():

    return jsonify({

        "success":
            True,

        "status":
            "online",

        "service":
            "Yalcin Pro BIST",

        "source":
            "Yahoo Finance Chart",

        "delay":
            "Yahoo tarafindaki mevcut piyasa gecikmesi",

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

        "success":
            True,

        "status":
            "online",

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

        "success":
            True,

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

    # --------------------------------------------------------
    # DOSYAYI KONTROL
    # --------------------------------------------------------

    refresh_symbols_from_file()


    # --------------------------------------------------------
    # ONLINE KAYNAĞI KONTROL
    # --------------------------------------------------------

    refresh_symbols_from_online_source()


    return jsonify({

        "success":
            True,

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

    # ========================================================
    # 1 - YEREL DOSYAYI KONTROL
    # ========================================================

    file_changed = (
        refresh_symbols_from_file()
    )


    # ========================================================
    # 2 - ONLINE SEMBOL KAYNAĞINI KONTROL
    #
    # ÖNEMLİ:
    #
    # Artık sadece background thread'e güvenmiyoruz.
    #
    # Android her /all istediğinde zaman dolmuşsa
    # online sembol listesi kontrol ediliyor.
    # ========================================================

    symbols_added = (
        refresh_symbols_from_online_source()
    )


    # ========================================================
    # 3 - CACHE YAŞI
    # ========================================================

    age = (

        time.time()
        - last_refresh[
            "timestamp"
        ]
    )


    # ========================================================
    # 4 - YENİ HİSSE EKLENDİYSE
    #
    # Yeni hisse fiyatını da hemen çek.
    # ========================================================

    if symbols_added:

        print(
            "YALCIN PRO - YENI HISSE EKLENDI"
        )

        print(
            "YALCIN PRO - YENI VERILER CEKILIYOR"
        )


        def background_refresh_new_symbols():

            try:

                refresh_all(
                    force=True
                )

            except Exception as e:

                print(
                    "YALCIN PRO - YENI HISSE REFRESH HATASI:",
                    repr(e)
                )


        threading.Thread(

            target=
                background_refresh_new_symbols,

            daemon=True,

            name=
                "yalcinpro-new-symbol-refresh"

        ).start()


    # ========================================================
    # 5 - NORMAL CACHE YENILEME
    # ========================================================

    elif (
        not cache
        or age >= REFRESH_SECONDS
        or file_changed
    ):


        print("")

        print(
            "YALCIN PRO - /all YENI VERI ISTEDI"
        )

        print(
            "CACHE:",
            len(cache)
        )

        print(
            "CACHE YASI:",
            round(
                age,
                1
            ),
            "saniye"
        )

        print(
            "LIMITE:",
            REFRESH_SECONDS,
            "saniye"
        )


        def background_refresh():

            try:

                refresh_all(
                    force=True
                )

            except Exception as e:

                print(
                    "YALCIN PRO - /all REFRESH HATASI:",
                    repr(e)
                )


        threading.Thread(

            target=
                background_refresh,

            daemon=True,

            name=
                "yalcinpro-all-refresh"

        ).start()


    # ========================================================
    # 6 - SEMBOL SIRASINI KORU
    # ========================================================

    with symbols_lock:

        current_symbols = (
            list(SYMBOLS)
        )


    data = [

        cache[symbol]

        for symbol
        in current_symbols

        if symbol
        in cache
    ]


    # ========================================================
    # 7 - JSON
    # ========================================================

    return jsonify({

        "success":
            True,

        "status":
            "online",

        "serverTime":
            now_text(),

        "symbols":
            len(current_symbols),

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
# TEK HISSE
# ============================================================

@app.route("/stock/<symbol>")
def stock(symbol):

    symbol = normalize_symbol(
        symbol
    )


    if not symbol:

        return jsonify({

            "success":
                False,

            "error":
                "Gecersiz sembol",

        }), 400


    # --------------------------------------------------------
    # CACHE
    # --------------------------------------------------------

    item = cache.get(
        symbol
    )


    # --------------------------------------------------------
    # CACHE YOKSA YAHOO
    # --------------------------------------------------------

    if item is None:

        print(
            "YALCIN PRO - /stock/",
            symbol,
            "YAHOO'DAN CEKILIYOR"
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

            "success":
                False,

            "symbol":
                symbol,

            "data":
                None,

        }), 404


    # --------------------------------------------------------
    # SONUC
    # --------------------------------------------------------

    return jsonify({

        "success":
            True,

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
        "YALCIN PRO - MANUEL REFRESH"
    )

    print(
        "=================================================="
    )


    # --------------------------------------------------------
    # Önce yeni sembol kontrolü
    # --------------------------------------------------------

    refresh_symbols_from_online_source(
        force=True
    )


    # --------------------------------------------------------
    # Sonra fiyatları yenile
    # --------------------------------------------------------

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

        "symbols":
            len(SYMBOLS),

        "target":
            len(SYMBOLS),

        "lastRefresh":
            last_refresh,
    })


# ============================================================
# UYGULAMA
# ============================================================

if __name__ == "__main__":

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
        "PORT:",
        port
    )

    print(
        "SEMBOL:",
        len(SYMBOLS)
    )

    print(
        "TARGET:",
        TARGET
    )

    print(
        "FİYAT REFRESH:",
        REFRESH_SECONDS,
        "saniye"
    )

    print(
        "SEMBOL KONTROL:",
        SYMBOL_SOURCE_REFRESH_SECONDS,
        "saniye"
    )

    print(
        "=================================================="
    )

    print("")


    # --------------------------------------------------------
    # Başlangıçta sembol listesini hemen kontrol et
    # --------------------------------------------------------

    try:

        refresh_symbols_from_online_source(
            force=True
        )

    except Exception as e:

        print(
            "YALCIN PRO - BASLANGIC SEMBOL HATASI:",
            repr(e)
        )


    # --------------------------------------------------------
    # İlk veri
    # --------------------------------------------------------

    try:

        refresh_all(
            force=True
        )

    except Exception as e:

        print(
            "YALCIN PRO - BASLANGIC VERI HATASI:",
            repr(e)
        )


    # --------------------------------------------------------
    # Flask
    # --------------------------------------------------------

    app.run(

        host="0.0.0.0",

        port=port,

        debug=False,

        threaded=True
    )
