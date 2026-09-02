from flask import Flask, jsonify, request
import yfinance as yf
import threading
import time
import os
import json
from zoneinfo import ZoneInfo


# =============================================================
# YALCIN PRO - CANLI BIST SERVER
# =============================================================

app = Flask(__name__)

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


# =============================================================
# AYARLAR
# =============================================================

BATCH_SIZE = 25
CACHE_TTL_SECONDS = 60

BACKGROUND_REFRESH_SECONDS = 30

RETRY_COUNT = 2
RETRY_WAIT_SECONDS = 2

PERSISTENT_CACHE_FILE = "yalcin_pro_cache.json"


# =============================================================
# CACHE
# =============================================================

_stock_cache = {}
_cache_lock = threading.Lock()

_background_refresh_started = False
_refresh_lock = threading.Lock()


# =============================================================
# SEMBOL NORMALİZASYONU
# =============================================================

def normalize_symbol(symbol):

    if not symbol:
        return ""

    return (
        str(symbol)
        .strip()
        .upper()
        .replace(".IS", "")
        .replace(" ", "")
    )


# =============================================================
# KALICI CACHE YÜKLE
# =============================================================

def _load_persistent_cache():

    global _stock_cache

    try:

        if not os.path.exists(PERSISTENT_CACHE_FILE):
            print(
                "YALCIN PRO - KALICI CACHE DOSYASI YOK"
            )
            return

        with open(
            PERSISTENT_CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            saved = json.load(f)

        now = time.time()

        loaded = 0

        with _cache_lock:

            for symbol, item in saved.items():

                if (
                    isinstance(item, list)
                    and len(item) == 2
                    and isinstance(item[1], dict)
                ):

                    _stock_cache[
                        normalize_symbol(symbol)
                    ] = (
                        float(item[0]),
                        item[1]
                    )

                    loaded += 1

        print(
            "YALCIN PRO - KALICI CACHE YUKLENDI:",
            loaded,
            "HISSE"
        )

    except Exception as e:

        print(
            "YALCIN PRO - CACHE OKUMA HATASI:",
            e
        )


# =============================================================
# KALICI CACHE KAYDET
# =============================================================

def _save_persistent_cache():

    try:

        with _cache_lock:

            data = {
                symbol: [
                    timestamp,
                    result
                ]
                for symbol, (
                    timestamp,
                    result
                ) in _stock_cache.items()
            }

        temp_file = (
            PERSISTENT_CACHE_FILE
            + ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False
            )

        os.replace(
            temp_file,
            PERSISTENT_CACHE_FILE
        )

    except Exception as e:

        print(
            "YALCIN PRO - CACHE YAZMA HATASI:",
            e
        )


# =============================================================
# CACHE OKU
# =============================================================

def _get_cache(symbol):

    symbol = normalize_symbol(symbol)

    now = time.time()

    with _cache_lock:

        item = _stock_cache.get(symbol)

        if not item:
            return None, False

        timestamp, result = item

    age = now - timestamp

    fresh = age < CACHE_TTL_SECONDS

    return result, fresh


# =============================================================
# CACHE YAZ
# =============================================================

def _save_stock_memory(
    symbol,
    result
):

    symbol = normalize_symbol(symbol)

    if not symbol or not result:
        return

    with _cache_lock:

        _stock_cache[symbol] = (
            time.time(),
            result
        )


# =============================================================
# YAHOO CLOSE ÇIKAR
# =============================================================

def _extract_close(
    data,
    ticker_name
):

    if data is None:
        return None

    try:

        if data.empty:
            return None

    except Exception:

        return None

    try:

        # =====================================================
        # MULTI INDEX
        # =====================================================

        if hasattr(
            data.columns,
            "levels"
        ):

            level0 = (
                data.columns
                .get_level_values(0)
            )

            level1 = (
                data.columns
                .get_level_values(1)
            )

            # Ticker -> Close

            if ticker_name in level0:

                ticker_data = data[
                    ticker_name
                ]

                if (
                    hasattr(
                        ticker_data,
                        "columns"
                    )
                    and
                    "Close"
                    in ticker_data.columns
                ):

                    return (
                        ticker_data["Close"]
                        .dropna()
                    )

            # Close -> Ticker

            if (
                "Close" in level0
                and
                ticker_name in level1
            ):

                return (
                    data["Close"][
                        ticker_name
                    ]
                    .dropna()
                )

        # =====================================================
        # NORMAL DATA
        # =====================================================

        if "Close" in data.columns:

            return (
                data["Close"]
                .dropna()
            )

    except Exception as e:

        print(
            "YALCIN PRO - CLOSE OKUMA HATASI:",
            ticker_name,
            e
        )

    return None


# =============================================================
# SONUÇ OLUŞTUR
# =============================================================

def _make_result(
    symbol,
    closes,
    previous_close=None
):

    symbol = normalize_symbol(symbol)

    if not symbol:
        return None

    if closes is None:
        return None

    try:

        closes = closes.dropna()

    except Exception:

        return None

    if closes.empty:
        return None

    # =========================================================
    # SON FİYAT
    # =========================================================

    try:

        price = float(
            closes.iloc[-1]
        )

    except Exception:

        return None

    if price <= 0:
        return None

    # =========================================================
    # ÖNCEKİ KAPANIŞ
    # =========================================================

    previous = None

    # 1 - günlük veri

    if previous_close is not None:

        try:

            value = float(
                previous_close
            )

            if value > 0:

                previous = value

        except Exception:

            previous = None

    # 2 - intraday içinden önceki gün

    if previous is None:

        try:

            if hasattr(
                closes.index,
                "date"
            ):

                dates = list(
                    dict.fromkeys(
                        closes.index.date
                    )
                )

                if len(dates) >= 2:

                    previous_date = (
                        dates[-2]
                    )

                    previous_values = (
                        closes[
                            closes.index.date
                            == previous_date
                        ]
                    )

                    if not previous_values.empty:

                        value = float(
                            previous_values.iloc[-1]
                        )

                        if value > 0:
                            previous = value

        except Exception:
            previous = None

    # 3 - son çare

    if previous is None:
        previous = price

    # =========================================================
    # DEĞİŞİM %
    # =========================================================

    try:

        if previous > 0:

            change = (
                (price - previous)
                / previous
                * 100.0
            )

        else:

            change = 0.0

    except Exception:

        change = 0.0

    # =========================================================
    # ÇOK KÜÇÜK HATALARI TEMİZLE
    # =========================================================

    if abs(change) < 0.000001:
        change = 0.0

    result = {

        "sembol": symbol,

        "fiyat": round(
            price,
            2
        ),

        "oncekiKapanis": round(
            previous,
            2
        ),

        "degisimYuzde": round(
            change,
            2
        ),

        "paraBirimi": "TRY"
    }

    print(
        "YALCIN PRO - VERI:",
        symbol,
        "| FIYAT:",
        result["fiyat"],
        "| ONCEKI:",
        result["oncekiKapanis"],
        "| DEGISIM:",
        result["degisimYuzde"]
    )

    return result


# =============================================================
# TEK HİSSE - YAHOO
# =============================================================

def get_stock_from_yahoo(
    symbol
):

    symbol = normalize_symbol(symbol)

    if not symbol:
        return None

    ticker_name = (
        symbol + ".IS"
    )

    try:

        print(
            "YALCIN PRO - YAHOO:",
            ticker_name
        )

        # =====================================================
        # INTRADAY
        # =====================================================

        intraday = yf.download(

            tickers=ticker_name,

            period="1d",

            interval="5m",

            auto_adjust=False,

            prepost=False,

            threads=False,

            progress=False
        )

        closes = _extract_close(
            intraday,
            ticker_name
        )

        # =====================================================
        # GÜNLÜK
        # =====================================================

        daily = yf.download(

            tickers=ticker_name,

            period="5d",

            interval="1d",

            auto_adjust=False,

            prepost=False,

            threads=False,

            progress=False
        )

        daily_closes = _extract_close(
            daily,
            ticker_name
        )

        # =====================================================
        # INTRADAY YOKSA GÜNLÜK KULLAN
        # =====================================================

        if (
            closes is None
            or closes.empty
        ):

            closes = daily_closes

        if (
            closes is None
            or closes.empty
        ):

            print(
                "YALCIN PRO - VERI YOK:",
                symbol
            )

            return None

        # =====================================================
        # ÖNCEKİ KAPANIŞ
        # =====================================================

        previous_close = None

        if (
            daily_closes is not None
            and
            not daily_closes.empty
        ):

            daily_closes = (
                daily_closes
                .dropna()
            )

            if len(daily_closes) >= 2:

                try:

                    previous_close = float(
                        daily_closes.iloc[-2]
                    )

                    if previous_close <= 0:
                        previous_close = None

                except Exception:

                    previous_close = None

        # =====================================================
        # SONUÇ
        # =====================================================

        return _make_result(

            symbol,

            closes,

            previous_close
        )

    except Exception as e:

        print(
            "YALCIN PRO - YAHOO HATASI:",
            symbol,
            e
        )

        return None


# =============================================================
# TOPLU HİSSELER
# =============================================================

def get_stocks_batch(
    symbols
):

    symbols = [
        normalize_symbol(s)
        for s in symbols
        if normalize_symbol(s)
    ]

    symbols = list(
        dict.fromkeys(symbols)
    )

    if not symbols:
        return [], []

    results = []
    missing = []

    tickers = [
        symbol + ".IS"
        for symbol in symbols
    ]

    try:

        print(
            "YALCIN PRO - TOPLU YAHOO:",
            len(tickers),
            "HISSE"
        )

        # =====================================================
        # INTRADAY
        # =====================================================

        intraday_data = yf.download(

            tickers=tickers,

            period="1d",

            interval="5m",

            group_by="ticker",

            auto_adjust=False,

            prepost=False,

            threads=False,

            progress=False
        )

        # =====================================================
        # GÜNLÜK
        # =====================================================

        daily_data = yf.download(

            tickers=tickers,

            period="5d",

            interval="1d",

            group_by="ticker",

            auto_adjust=False,

            prepost=False,

            threads=False,

            progress=False
        )

        # =====================================================
        # HİSSELER
        # =====================================================

        for symbol in symbols:

            ticker_name = (
                symbol + ".IS"
            )

            try:

                # -------------------------------------------------
                # INTRADAY
                # -------------------------------------------------

                closes = _extract_close(

                    intraday_data,

                    ticker_name
                )

                # -------------------------------------------------
                # GÜNLÜK SON ÇARE
                # -------------------------------------------------

                if (
                    closes is None
                    or closes.empty
                ):

                    closes = _extract_close(

                        daily_data,

                        ticker_name
                    )

                if (
                    closes is None
                    or closes.empty
                ):

                    missing.append(
                        symbol
                    )

                    print(
                        "YALCIN PRO - EKSIK:",
                        symbol
                    )

                    continue

                # -------------------------------------------------
                # ÖNCEKİ KAPANIŞ
                # -------------------------------------------------

                daily_closes = (
                    _extract_close(
                        daily_data,
                        ticker_name
                    )
                )

                previous_close = None

                if (
                    daily_closes is not None
                    and
                    not daily_closes.empty
                ):

                    daily_closes = (
                        daily_closes
                        .dropna()
                    )

                    if len(daily_closes) >= 2:

                        try:

                            value = float(
                                daily_closes.iloc[-2]
                            )

                            if value > 0:
                                previous_close = value

                        except Exception:

                            previous_close = None

                # -------------------------------------------------
                # SONUÇ
                # -------------------------------------------------

                result = _make_result(

                    symbol,

                    closes,

                    previous_close
                )

                if result:

                    results.append(
                        result
                    )

                else:

                    missing.append(
                        symbol
                    )

            except Exception as e:

                print(
                    "YALCIN PRO - HİSSE HATASI:",
                    symbol,
                    e
                )

                missing.append(
                    symbol
                )

    except Exception as e:

        print(
            "YALCIN PRO - TOPLU YAHOO HATASI:",
            e
        )

        return [], list(symbols)

    return results, missing


# =============================================================
# ARKA PLAN YENİLEME
# =============================================================

def start_background_refresh(
    symbols
):

    global _background_refresh_started

    refresh_symbols = list(
        dict.fromkeys(

            normalize_symbol(s)

            for s in symbols

            if normalize_symbol(s)
        )
    )

    if not refresh_symbols:
        return

    with _refresh_lock:

        if _background_refresh_started:
            return

        _background_refresh_started = True

    def worker():

        print(
            "YALCIN PRO - ARKA PLAN BASLADI:",
            len(refresh_symbols),
            "HISSE"
        )

        while True:

            try:

                batches = [

                    refresh_symbols[i:i + BATCH_SIZE]

                    for i in range(
                        0,
                        len(refresh_symbols),
                        BATCH_SIZE
                    )
                ]

                total_updated = 0

                print(
                    "YALCIN PRO - YENILEME TURU:",
                    len(batches),
                    "GRUP"
                )

                for index, batch in enumerate(
                    batches,
                    start=1
                ):

                    print(
                        "YALCIN PRO - GRUP:",
                        index,
                        "/",
                        len(batches),
                        "|",
                        len(batch),
                        "HISSE"
                    )

                    success = False

                    for retry in range(
                        RETRY_COUNT + 1
                    ):

                        try:

                            results, missing = (
                                get_stocks_batch(
                                    batch
                                )
                            )

                            for result in results:

                                symbol = normalize_symbol(
                                    result.get(
                                        "sembol",
                                        ""
                                    )
                                )

                                if symbol:

                                    _save_stock_memory(
                                        symbol,
                                        result
                                    )

                            total_updated += (
                                len(results)
                            )

                            success = True

                            print(
                                "YALCIN PRO - GRUP TAMAM:",
                                index,
                                "| GUNCEL:",
                                len(results),
                                "| EKSIK:",
                                len(missing)
                            )

                            break

                        except Exception as e:

                            print(
                                "YALCIN PRO - GRUP HATASI:",
                                index,
                                e
                            )

                            if retry < RETRY_COUNT:

                                time.sleep(
                                    RETRY_WAIT_SECONDS
                                )

                    # Gruplar arasında bekle

                    time.sleep(1)

                # =================================================
                # CACHE DOSYASINI TEK SEFERDE KAYDET
                # =================================================

                _save_persistent_cache()

                print(
                    "YALCIN PRO - YENILEME TAMAMLANDI:",
                    total_updated,
                    "/",
                    len(refresh_symbols)
                )

                time.sleep(
                    BACKGROUND_REFRESH_SECONDS
                )

            except Exception as e:

                print(
                    "YALCIN PRO - ARKA PLAN HATASI:",
                    e
                )

                time.sleep(5)

    thread = threading.Thread(

        target=worker,

        daemon=True,

        name="yalcin-cache-refresh"
    )

    thread.start()


# =============================================================
# CACHE YÜKLE
# =============================================================

_load_persistent_cache()


# =============================================================
# HEALTH
# =============================================================

@app.route("/health")
def health():

    with _cache_lock:
        cache_count = len(
            _stock_cache
        )

    return jsonify({

        "success": True,

        "status": "online",

        "cache": cache_count,

        "serverTime": datetime_now()
    })


# =============================================================
# ZAMAN
# =============================================================

def datetime_now():

    return time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime()
    )


# =============================================================
# TEK HİSSE
# =============================================================

@app.route(
    "/stock/<sembol>"
)
def single_stock(
    sembol
):

    symbol = normalize_symbol(
        sembol
    )

    if not symbol:

        return jsonify({

            "success": False,

            "data": []
        }), 400

    result, fresh = _get_cache(
        symbol
    )

    # =========================================================
    # CACHE TAZE
    # =========================================================

    if result is not None and fresh:

        return jsonify({

            "success": True,

            "data": [
                result
            ]
        })

    # =========================================================
    # CACHE ESKİYSE ARKA PLANDA GÜNCELLE
    # =========================================================

    def update_one():

        new_result = (
            get_stock_from_yahoo(
                symbol
            )
        )

        if new_result:

            _save_stock_memory(
                symbol,
                new_result
            )

            _save_persistent_cache()

    thread = threading.Thread(
        target=update_one,
        daemon=True
    )

    thread.start()

    # Eski veri varsa hemen gönder

    if result is not None:

        return jsonify({

            "success": True,

            "data": [
                result
            ]
        })

    # Veri yoksa

    return jsonify({

        "success": True,

        "data": []
    })


# =============================================================
# TÜM HİSSELER
# =============================================================

@app.route("/stocks")
def stocks():

    symbols_text = request.args.get(
        "symbols",
        ""
    )

    if not symbols_text:

        return jsonify({

            "success": False,

            "error": (
                "symbols parametresi gerekli"
            ),

            "data": []
        }), 400

    # =========================================================
    # SEMBOLLERİ TEMİZLE
    # =========================================================

    symbols = [

        normalize_symbol(s)

        for s in symbols_text.split(",")

        if s.strip()
    ]

    symbols = list(
        dict.fromkeys(symbols)
    )

    print(
        "================================================="
    )

    print(
        "YALCIN PRO - ANDROID ISTEGI:",
        len(symbols),
        "HISSE"
    )

    # =========================================================
    # ARKA PLAN YENİLEMEYİ BAŞLAT
    # =========================================================

    start_background_refresh(
        symbols
    )

    # =========================================================
    # CACHE'DEN HEMEN CEVAP
    # =========================================================

    result_map = {}

    fresh_count = 0
    stale_count = 0

    with _cache_lock:

        for symbol in symbols:

            item = _stock_cache.get(
                symbol
            )

            if not item:
                continue

            timestamp, result = item

            age = (
                time.time()
                - timestamp
            )

            result_map[symbol] = result

            if age < CACHE_TTL_SECONDS:

                fresh_count += 1

            else:

                stale_count += 1

    # =========================================================
    # ESKİ CACHE OLAN HİSSELERİ ARKA PLAN YENİLEYECEK
    # =========================================================

    print(
        "YALCIN PRO - CACHE:",
        len(result_map),
        "/",
        len(symbols),

        "| TAZE:",
        fresh_count,

        "| ESKI:",
        stale_count
    )

    # =========================================================
    # ANDROID SIRASINI KORU
    # =========================================================

    ordered_results = [

        result_map[symbol]

        for symbol in symbols

        if symbol in result_map
    ]

    # =========================================================
    # VERİSİ OLMAYANLAR
    # =========================================================

    missing = [

        symbol

        for symbol in symbols

        if symbol not in result_map
    ]

    print(
        "YALCIN PRO - CEVAP:",
        len(ordered_results),
        "/",
        len(symbols)
    )

    if missing:

        print(
            "YALCIN PRO - VERISI OLMAYAN:",
            len(missing)
        )

    print(
        "================================================="
    )

    # =========================================================
    # ANINDA JSON
    # =========================================================

    return jsonify({

        "success": True,

        "data": ordered_results
    })


# =============================================================
# SERVER
# =============================================================

if __name__ == "__main__":

    with _cache_lock:

        cache_count = len(
            _stock_cache
        )

    print(
        "================================================="
    )

    print(
        "YALCIN PRO SERVER"
    )

    print(
        "CACHE:",
        cache_count,
        "HISSE"
    )

    print(
        "PORT: 5000"
    )

    print(
        "CANLI YENILEME:",
        BACKGROUND_REFRESH_SECONDS,
        "SANİYE"
    )

    print(
        "================================================="
    )

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(

        host="0.0.0.0",

        port=port,

        debug=False,

        threaded=True
    )
