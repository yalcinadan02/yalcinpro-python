from flask import Flask, jsonify, request
import yfinance as yf
import concurrent.futures
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

# =============================================================
# AYARLAR
# =============================================================

# Daha küçük gruplar -> Yahoo'ya daha az yük
BATCH_SIZE = 10

# Aynı anda daha az işlem
MAX_WORKERS = 2

# Eksik hisseler için tekrar deneme
MISSING_RETRY_COUNT = 3
RETRY_WAIT_SECONDS = 2.0

# Cache
CACHE_TTL_SECONDS = 45

_stock_cache = {}
_cache_lock = __import__("threading").Lock()


# =============================================================
# SEMBOL
# =============================================================

def normalize_symbol(symbol):
    return symbol.strip().upper()


# =============================================================
# CACHE
# =============================================================

def _cached_stock(symbol):
    now = time.time()

    with _cache_lock:
        item = _stock_cache.get(symbol)

        if item and now - item[0] < CACHE_TTL_SECONDS:
            return item[1]

        if item:
            _stock_cache.pop(symbol, None)

    return None


def _save_stock(symbol, result):
    with _cache_lock:
        _stock_cache[symbol] = (time.time(), result)


# =============================================================
# SONUÇ OLUŞTUR
# =============================================================

def _make_result(symbol, closes):
    try:
        closes = closes.dropna()
    except Exception:
        return None

    if closes.empty:
        return None

    try:
        price = float(closes.iloc[-1])
    except Exception:
        return None

    # Önceki işlem gününün son kapanışı
    try:
        dates = list(dict.fromkeys(closes.index.date))

        if len(dates) >= 2:
            previous_date = dates[-2]
            previous_values = closes[
                closes.index.date == previous_date
            ]

            previous = float(previous_values.iloc[-1])
        else:
            previous = price

    except Exception:
        previous = price

    change = (
        ((price - previous) / previous * 100.0)
        if previous
        else 0.0
    )

    result = {
        "sembol": symbol,
        "fiyat": round(price, 2),
        "oncekiKapanis": round(previous, 2),
        "degisimYuzde": round(change, 2),
        "paraBirimi": "TRY"
    }

    _save_stock(symbol, result)

    return result


# =============================================================
# TEK HİSSE
# =============================================================

def get_stock(symbol, retry_count=0):
    """
    Tek hisse verisini alır.
    Önce cache kontrol edilir.
    """

    symbol = normalize_symbol(symbol)

    cached = _cached_stock(symbol)

    if cached is not None:
        print(f"YALCIN PRO - {symbol}: CACHE")
        return cached

    try:
        ticker = yf.Ticker(symbol + ".IS")

        # Daha hafif sorgu
        intraday = ticker.history(
            period="2d",
            interval="5m",
            auto_adjust=False,
            prepost=False
        )

        # Intraday yoksa günlük veriye düş
        if (
            intraday is None
            or intraday.empty
            or "Close" not in intraday.columns
        ):

            daily = ticker.history(
                period="5d",
                interval="1d",
                auto_adjust=False
            )

            if (
                daily is None
                or daily.empty
                or "Close" not in daily.columns
            ):
                raise ValueError("Veri bulunamadı")

            closes = daily["Close"].dropna()

            if closes.empty:
                raise ValueError("Kapanış verisi bulunamadı")

            price = float(closes.iloc[-1])

            previous = (
                float(closes.iloc[-2])
                if len(closes) >= 2
                else price
            )

            change = (
                ((price - previous) / previous * 100.0)
                if previous
                else 0.0
            )

            result = {
                "sembol": symbol,
                "fiyat": round(price, 2),
                "oncekiKapanis": round(previous, 2),
                "degisimYuzde": round(change, 2),
                "paraBirimi": "TRY"
            }

            _save_stock(symbol, result)

            return result

        return _make_result(
            symbol,
            intraday["Close"]
        )

    except Exception as e:

        text = str(e)

        print(
            f"YALCIN PRO - HISSE HATASI "
            f"{symbol}: {text}"
        )

        # Kontrollü tekrar deneme
        if retry_count < 2:

            if (
                "Too Many Requests" in text
                or "Rate limited" in text
                or "429" in text
            ):
                wait = 8 * (retry_count + 1)
            else:
                wait = 3 * (retry_count + 1)

            print(
                f"YALCIN PRO - {symbol} "
                f"RETRY {retry_count + 1}/2 | "
                f"{wait} sn bekle"
            )

            time.sleep(wait)

            return get_stock(
                symbol,
                retry_count + 1
            )

        return None


# =============================================================
# TOPLU HİSSE
# =============================================================

def get_stocks_batch(symbols):
    """
    Küçük bir sembol grubunu Yahoo'dan alır.
    """

    results = []
    missing = []

    tickers = [
        symbol + ".IS"
        for symbol in symbols
    ]

    try:

        print(
            "YALCIN PRO - YAHOO TOPLU İSTEK:",
            len(tickers),
            "HISSE"
        )

        data = yf.download(
            tickers=tickers,

            # Daha hafif veri
            period="2d",
            interval="5m",

            group_by="ticker",
            auto_adjust=False,
            prepost=False,

            # Aynı anda fazla bağlantı açma
            threads=False,

            progress=False
        )

        if data is None or data.empty:
            print(
                "YALCIN PRO - TOPLU VERİ BOŞ"
            )

            return [], list(symbols)

        for symbol in symbols:

            ticker_name = symbol + ".IS"

            try:

                # MultiIndex
                if hasattr(data.columns, "levels"):

                    level0 = data.columns.get_level_values(0)
                    level1 = data.columns.get_level_values(1)

                    if ticker_name in level0:

                        closes = data[
                            ticker_name
                        ]["Close"]

                    elif (
                        "Close" in level0
                        and ticker_name in level1
                    ):

                        closes = data[
                            "Close"
                        ][ticker_name]

                    else:
                        raise KeyError(
                            "Ticker kolonu yok"
                        )

                else:

                    if "Close" not in data.columns:
                        raise KeyError(
                            "Close kolonu yok"
                        )

                    closes = data["Close"]

                result = _make_result(
                    symbol,
                    closes
                )

                if result is not None:
                    results.append(result)
                else:
                    missing.append(symbol)

            except Exception as e:

                print(
                    f"YALCIN PRO - "
                    f"{symbol} TOPLU VERİ HATASI: {e}"
                )

                missing.append(symbol)

    except Exception as e:

        print(
            "YALCIN PRO - "
            "TOPLU YAHOO HATASI:",
            e
        )

        missing = list(symbols)

    return results, missing


# =============================================================
# HEALTH
# =============================================================

@app.route("/health")
def health():

    return jsonify({
        "success": True,
        "status": "online"
    })


# =============================================================
# TEK HİSSE
# =============================================================

@app.route("/stock/<sembol>")
def single_stock(sembol):

    symbol = normalize_symbol(sembol)

    result = get_stock(symbol)

    if result is None:

        return jsonify({
            "success": False,
            "data": []
        }), 404

    return jsonify({
        "success": True,
        "data": [result]
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
            "error": "symbols parametresi gerekli",
            "data": []
        }), 400

    # ---------------------------------------------------------
    # SEMBOLLERİ TEMİZLE
    # ---------------------------------------------------------

    symbols = [
        normalize_symbol(s)
        for s in symbols_text.split(",")
        if s.strip()
    ]

    # Aynı sembolleri temizle
    symbols = list(
        dict.fromkeys(symbols)
    )

    print(
        "================================================="
    )

    print(
        "YALCIN PRO - ISTENEN HISSE SAYISI:",
        len(symbols)
    )

    results = []

    # ---------------------------------------------------------
    # GRUPLARA AYIR
    # ---------------------------------------------------------

    batches = [
        symbols[i:i + BATCH_SIZE]
        for i in range(
            0,
            len(symbols),
            BATCH_SIZE
        )
    ]

    print(
        "YALCIN PRO - TOPLAM GRUP:",
        len(batches)
    )

    # ---------------------------------------------------------
    # İLK TUR
    # ---------------------------------------------------------

    try:

        for batch_index, batch in enumerate(
            batches,
            start=1
        ):

            print(
                "YALCIN PRO - "
                "TOPLU GRUP:",
                batch_index,
                "/",
                len(batches),
                "|",
                len(batch),
                "HISSE"
            )

            batch_results, batch_missing = (
                get_stocks_batch(batch)
            )

            results.extend(batch_results)

            print(
                "YALCIN PRO - "
                "GRUP TAMAMLANDI:",
                len(batch_results),
                "| EKSIK:",
                len(batch_missing),
                "| TOPLAM:",
                len(results)
            )

            # Yahoo'yu yormamak için bekle
            if batch_index < len(batches):
                time.sleep(1.5)

    except Exception as e:

        print(
            "YALCIN PRO - GENEL HATA:",
            e
        )


    # =========================================================
    # İLK SONUÇLARI SEMBOLE GÖRE HARİTALA
    # =========================================================

    result_map = {}

    for item in results:

        symbol = normalize_symbol(
            item.get(
                "sembol",
                ""
            )
        )

        if symbol:
            result_map[symbol] = item


    # =========================================================
    # EKSİK HİSSELER
    # =========================================================

    missing_symbols = [
        symbol
        for symbol in symbols
        if symbol not in result_map
    ]

    print(
        "YALCIN PRO - "
        "ILK TUR SONRASI:",
        len(result_map),
        "/",
        len(symbols)
    )

    print(
        "YALCIN PRO - "
        "EKSIK HISSE SAYISI:",
        len(missing_symbols)
    )


    # =========================================================
    # İKİNCİ TUR
    # =========================================================

    if missing_symbols:

        print(
            "YALCIN PRO - "
            "EKSIK HISSELER:",
            ", ".join(missing_symbols)
        )

        for retry_round in range(
            1,
            MISSING_RETRY_COUNT + 1
        ):

            # Hala eksik olanları yeniden hesapla
            current_missing = [
                symbol
                for symbol in symbols
                if symbol not in result_map
            ]

            if not current_missing:
                break

            print(
                "YALCIN PRO - "
                "EKSIK TUR:",
                retry_round,
                "/",
                MISSING_RETRY_COUNT,
                "|",
                len(current_missing),
                "HISSE"
            )

            retry_batches = [
                current_missing[i:i + BATCH_SIZE]
                for i in range(
                    0,
                    len(current_missing),
                    BATCH_SIZE
                )
            ]

            for retry_index, retry_batch in enumerate(
                retry_batches,
                start=1
            ):

                print(
                    "YALCIN PRO - "
                    "EKSIK GRUP:",
                    retry_index,
                    "/",
                    len(retry_batches),
                    "|",
                    len(retry_batch),
                    "HISSE"
                )

                try:

                    retry_results, still_missing = (
                        get_stocks_batch(
                            retry_batch
                        )
                    )

                    for retry_result in retry_results:

                        normalized = normalize_symbol(
                            retry_result["sembol"]
                        )

                        result_map[
                            normalized
                        ] = retry_result

                        print(
                            "YALCIN PRO - "
                            "EKSIK HISSE BULUNDU:",
                            normalized
                        )

                    if still_missing:

                        print(
                            "YALCIN PRO - "
                            "BU TURDA HALA EKSIK:",
                            ", ".join(
                                still_missing
                            )
                        )

                except Exception as e:

                    print(
                        "YALCIN PRO - "
                        "EKSIK GRUP HATASI:",
                        e
                    )

                # Gruplar arasında bekle
                if retry_index < len(retry_batches):

                    time.sleep(
                        RETRY_WAIT_SECONDS
                    )

            # Bir sonraki eksik turdan önce bekle
            if retry_round < MISSING_RETRY_COUNT:

                time.sleep(
                    RETRY_WAIT_SECONDS
                )


    # =========================================================
    # ANDROID'DEKİ SIRAYI KORU
    # =========================================================

    ordered_results = [
        result_map[symbol]
        for symbol in symbols
        if symbol in result_map
    ]

    final_missing = [
        symbol
        for symbol in symbols
        if symbol not in result_map
    ]

    print(
        "YALCIN PRO - "
        "PYTHON STOCKS TAMAMLANDI:",
        len(ordered_results),
        "/",
        len(symbols)
    )

    if final_missing:

        print(
            "YALCIN PRO - "
            "SON HALDE VERİSİ OLMAYANLAR:",
            ", ".join(final_missing)
        )

    else:

        print(
            "YALCIN PRO - "
            "TÜM HISSELERDEN VERI GELDI."
        )

    print(
        "================================================="
    )


    # =========================================================
    # JSON
    # =========================================================

    return jsonify({
        "success": True,
        "data": ordered_results
    })


# =============================================================
# SERVER
# =============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
