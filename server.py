import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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

TARGET = 614

SYMBOL_FILE = "yalcin_pro_active_symbols.json"

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

        return found[:TARGET]

    except Exception as e:

        print(
            "YALCIN PRO - SEMBOL OKUMA HATASI:",
            repr(e)
        )

        return []


# ============================================================
# SEMBOL LISTESI
# ============================================================

SYMBOLS = load_symbols()


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
                    "range": "5d",

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
            # CHART KAPANISLARI
            # ------------------------------------------------

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
            # PRICE FALLBACK
            # ------------------------------------------------

            if price is None and closes:

                price = closes[-1]


            if price is None:

                print(
                    f"YALCIN PRO - {symbol}: "
                    f"FIYAT BULUNAMADI"
                )

                return None


            price = float(price)


            # ------------------------------------------------
            # PREVIOUS CLOSE FALLBACK
            # ------------------------------------------------

            if (
                previous is None
                and len(closes) >= 2
            ):

                previous = closes[-2]


            if previous is not None:

                previous = float(previous)


            # ------------------------------------------------
            # DEGISIM YUZDESI
            # ------------------------------------------------

            change = None


            if previous not in (
                None,
                0
            ):

                change = (
                    (price - previous)
                    / previous
                ) * 100.0


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


        refresh_all(
            force=True
        )


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
