from flask import Flask, jsonify, request
import yfinance as yf
import concurrent.futures
import os
import json
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

# =============================================================
# AYARLAR
# =============================================================

BATCH_SIZE = 25
MAX_WORKERS = 3

MISSING_RETRY_COUNT = 3
RETRY_WAIT_SECONDS = 1.0

CACHE_TTL_SECONDS = 60

_stock_cache = {}
_cache_lock = __import__("threading").Lock()

# =============================================================
# KALICI CACHE
# =============================================================
# Sunucu yeniden başlasa bile son başarılı verileri korur.
PERSISTENT_CACHE_FILE = "yalcin_pro_cache.json"
BACKGROUND_REFRESH_SECONDS = 30

_refresh_lock = threading.Lock()
_background_refresh_started = False

def _load_persistent_cache():
    global _stock_cache

    try:
        if not os.path.exists(PERSISTENT_CACHE_FILE):
            return

        with open(
            PERSISTENT_CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            saved = json.load(f)

        now = time.time()

        with _cache_lock:
            for symbol, item in saved.items():
                if (
                    isinstance(item, list)
                    and len(item) == 2
                    and isinstance(item[1], dict)
                ):
                    # Kalıcı cache'i açılışta doğrudan kullanılabilir yap.
                    _stock_cache[symbol] = (
                        now,
                        item[1]
                    )

        print(
            "YALCIN PRO - KALICI CACHE YUKLENDI:",
            len(_stock_cache),
            "HISSE"
        )

    except Exception as e:
        print(
            "YALCIN PRO - CACHE OKUMA HATASI:",
            e
        )


def _save_persistent_cache():
    try:
        with _cache_lock:
            data = {
                symbol: [timestamp, result]
                for symbol, (timestamp, result)
                in _stock_cache.items()
            }

        temp_file = PERSISTENT_CACHE_FILE + ".tmp"

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
# SEMBOL
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
# CACHE
# =============================================================

def _cached_stock(symbol):
    now = time.time()

    with _cache_lock:
        item = _stock_cache.get(symbol)

        if not item:
            return None

        age = now - item[0]

        if age < CACHE_TTL_SECONDS:
            return item[1]

        # Yahoo geçici hata verirse ekran boşalmasın.
        # Son başarılı veri, yeni veri gelene kadar korunur.
        print(
            f"YALCIN PRO - {symbol}: "
            f"STALE CACHE KULLANILIYOR | YAS: {age:.1f} sn"
        )

        return item[1]


def _save_stock(symbol, result):
    with _cache_lock:
        _stock_cache[symbol] = (time.time(), result)

    # Dosyaya her hisse için yazmak yerine güvenli şekilde güncelle.
    _save_persistent_cache()


# =============================================================
# YAHOO CLOSE ÇIKAR
# =============================================================

def _extract_close(data, ticker_name):
    """
    yfinance MultiIndex yapısında Close kolonunu güvenli şekilde bulur.
    """

    if data is None or data.empty:
        return None

    try:

        # MultiIndex
        if hasattr(data.columns, "levels"):

            level0 = data.columns.get_level_values(0)
            level1 = data.columns.get_level_values(1)

            # Yapı:
            # Ticker -> Close
            if ticker_name in level0:

                ticker_data = data[ticker_name]

                if "Close" in ticker_data.columns:
                    return ticker_data["Close"].dropna()

            # Yapı:
            # Close -> Ticker
            if "Close" in level0 and ticker_name in level1:

                return data["Close"][ticker_name].dropna()

        # Tek hisse / normal kolon
        if "Close" in data.columns:
            return data["Close"].dropna()

    except Exception as e:

        print(
            f"YALCIN PRO - CLOSE OKUMA HATASI "
            f"{ticker_name}: {e}"
        )

    return None


# =============================================================
# SONUÇ OLUŞTUR
# =============================================================

def _make_result(symbol, closes, previous_close=None):

    try:
        if closes is None:
            return None

        closes = closes.dropna()

    except Exception:
        return None

    if closes.empty:
        return None

    # ---------------------------------------------------------
    # CANLI / SON FİYAT
    # ---------------------------------------------------------

    try:
        price = float(closes.iloc[-1])
    except Exception:
        return None

    if price <= 0:
        return None

    # ---------------------------------------------------------
    # ÖNCEKİ KAPANIŞ
    # ---------------------------------------------------------
    #
    # Öncelik:
    # 1) Günlük Yahoo verisinden gelen previous_close
    # 2) Eğer yoksa intraday içinden farklı gün aranır
    # 3) Son çare fiyatın kendisi
    #

    previous = None

    # 1. Günlük veriden gelen kesin önceki kapanış
    if previous_close is not None:

        try:

            previous_value = float(previous_close)

            if previous_value > 0:
                previous = previous_value

        except Exception:
            previous = None

    # 2. Günlük veri gelmezse intraday içinden bul
    if previous is None:

        try:

            dates = list(
                dict.fromkeys(
                    closes.index.date
                )
            )

            if len(dates) >= 2:

                previous_date = dates[-2]

                previous_values = closes[
                    closes.index.date == previous_date
                ]

                if not previous_values.empty:

                    previous_value = float(
                        previous_values.iloc[-1]
                    )

                    if previous_value > 0:
                        previous = previous_value

        except Exception:
            previous = None

    # 3. Son çare
    if previous is None:
        previous = price

    # ---------------------------------------------------------
    # DEĞİŞİM YÜZDESİ
    # ---------------------------------------------------------

    try:

        change = (
            (price - previous)
            / previous
            * 100.0
        ) if previous > 0 else 0.0

    except Exception:
        change = 0.0

    # Çok küçük sayısal hataları temizle
    if abs(change) < 0.000001:
        change = 0.0

    result = {
        "sembol": normalize_symbol(symbol),
        "fiyat": round(price, 2),
        "oncekiKapanis": round(previous, 2),
        "degisimYuzde": round(change, 2),
        "paraBirimi": "TRY"
    }

    print(
        "YALCIN PRO - VERI:",
        result["sembol"],
        "| FIYAT:",
        result["fiyat"],
        "| ONCEKI:",
        result["oncekiKapanis"],
        "| DEGISIM:",
        result["degisimYuzde"]
    )

    _save_stock(
        normalize_symbol(symbol),
        result
    )

    return result


# =============================================================
# GÜNLÜK ÖNCEKİ KAPANIŞ
# =============================================================

def get_previous_close(symbol):

    symbol = normalize_symbol(symbol)

    ticker_name = symbol + ".IS"

    try:

        daily = yf.download(
            tickers=ticker_name,
            period="5d",
            interval="1d",
            auto_adjust=False,
            prepost=False,
            threads=False,
            progress=False
        )

        closes = _extract_close(
            daily,
            ticker_name
        )

        if closes is None or closes.empty:
            return None

        closes = closes.dropna()

        if len(closes) < 2:

            print(
                f"YALCIN PRO - {symbol}: "
                "GUNLUK ONCEKI KAPANIS YOK"
            )

            return None

        # Son günlük kayıt bugün,
        # bir önceki kayıt önceki işlem günüdür.
        previous = float(
            closes.iloc[-2]
        )

        if previous <= 0:
            return None

        print(
            f"YALCIN PRO - {symbol}: "
            f"ONCEKI KAPANIS = {previous:.2f}"
        )

        return previous

    except Exception as e:

        print(
            f"YALCIN PRO - {symbol}: "
            f"ONCEKI KAPANIS HATASI: {e}"
        )

        return None


# =============================================================
# TEK HİSSE
# =============================================================

def get_stock(symbol, retry_count=0):

    symbol = normalize_symbol(symbol)

    cached = _cached_stock(symbol)

    if cached is not None:

        print(
            f"YALCIN PRO - {symbol}: CACHE"
        )

        return cached

    try:

        ticker_name = symbol + ".IS"

        ticker = yf.Ticker(
            ticker_name
        )

        # -----------------------------------------------------
        # 1. CANLI / INTRADAY
        # -----------------------------------------------------

        intraday = ticker.history(
            period="1d",
            interval="5m",
            auto_adjust=False,
            prepost=False
        )

        closes = None

        if (
            intraday is not None
            and not intraday.empty
            and "Close" in intraday.columns
        ):

            closes = (
                intraday["Close"]
                .dropna()
            )

        # -----------------------------------------------------
        # 2. INTRADAY YOKSA GÜNLÜK VERİ
        # -----------------------------------------------------

        if closes is None or closes.empty:

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

                raise ValueError(
                    "Veri bulunamadı"
                )

            closes = (
                daily["Close"]
                .dropna()
            )

        if closes.empty:
            raise ValueError(
                "Kapanış verisi bulunamadı"
            )

        # -----------------------------------------------------
        # 3. ÖNCEKİ KAPANIŞI GÜNLÜK VERİDEN AL
        # -----------------------------------------------------

        previous_close = (
            get_previous_close(symbol)
        )

        # -----------------------------------------------------
        # 4. SONUCU OLUŞTUR
        # -----------------------------------------------------

        result = _make_result(
            symbol,
            closes,
            previous_close
        )

        if result is None:
            raise ValueError(
                "Sonuç oluşturulamadı"
            )

        return result

    except Exception as e:

        text = str(e)

        print(
            f"YALCIN PRO - HISSE HATASI "
            f"{symbol}: {text}"
        )

        # -----------------------------------------------------
        # RETRY
        # -----------------------------------------------------

        if retry_count < 2:

            if (
                "Too Many Requests" in text
                or "Rate limited" in text
                or "429" in text
            ):

                wait = 8 * (
                    retry_count + 1
                )

            else:

                wait = 3 * (
                    retry_count + 1
                )

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

    results = []
    missing = []

    tickers = [
        normalize_symbol(symbol) + ".IS"
        for symbol in symbols
    ]

    try:

        print(
            "YALCIN PRO - YAHOO TOPLU ISTEK:",
            len(tickers),
            "HISSE"
        )

        # -----------------------------------------------------
        # 1. INTRADAY VERİ
        # -----------------------------------------------------

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

        # -----------------------------------------------------
        # 2. GÜNLÜK VERİ
        # -----------------------------------------------------
        #
        # Önceki kapanışları burada ayrı alıyoruz.
        #

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

        if (
            intraday_data is None
            or intraday_data.empty
        ):

            print(
                "YALCIN PRO - "
                "TOPLU INTRADAY VERİ BOŞ"
            )

            return [], list(symbols)

        # -----------------------------------------------------
        # 3. HİSSELERİ TEK TEK İŞLE
        # -----------------------------------------------------

        for symbol in symbols:

            symbol = normalize_symbol(
                symbol
            )

            ticker_name = symbol + ".IS"

            try:

                # ---------------------------------------------
                # INTRADAY CLOSE
                # ---------------------------------------------

                closes = _extract_close(
                    intraday_data,
                    ticker_name
                )

                if (
                    closes is None
                    or closes.empty
                ):

                    # Günlük veriyi son çare olarak kullan
                    closes = _extract_close(
                        daily_data,
                        ticker_name
                    )

                if (
                    closes is None
                    or closes.empty
                ):

                    print(
                        f"YALCIN PRO - "
                        f"{symbol}: "
                        f"INTRADAY VERI YOK"
                    )

                    missing.append(
                        symbol
                    )

                    continue

                # ---------------------------------------------
                # ÖNCEKİ KAPANIŞ
                # ---------------------------------------------

                daily_closes = _extract_close(
                    daily_data,
                    ticker_name
                )

                previous_close = None

                if (
                    daily_closes is not None
                    and not daily_closes.empty
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

                # ---------------------------------------------
                # SONUCU OLUŞTUR
                # ---------------------------------------------

                result = _make_result(
                    symbol,
                    closes,
                    previous_close
                )

                if result is not None:

                    results.append(
                        result
                    )

                else:

                    missing.append(
                        symbol
                    )

            except Exception as e:

                print(
                    f"YALCIN PRO - "
                    f"{symbol} TOPLU VERİ HATASI: "
                    f"{e}"
                )

                missing.append(
                    symbol
                )

    except Exception as e:

        print(
            "YALCIN PRO - "
            "TOPLU YAHOO HATASI:",
            e
        )

        missing = list(symbols)

    return results, missing


# =============================================================
# ARKA PLAN CACHE YENILEME
# =============================================================

def start_background_refresh(symbols):
    global _background_refresh_started

    if _background_refresh_started:
        return

    with _refresh_lock:
        if _background_refresh_started:
            return

        _background_refresh_started = True

    refresh_symbols = list(dict.fromkeys(
        normalize_symbol(s)
        for s in symbols
        if normalize_symbol(s)
    ))

    def worker():
        print(
            "YALCIN PRO - ARKA PLAN CACHE YENILEME BASLADI:",
            len(refresh_symbols),
            "HISSE"
        )

        while True:
            try:
                batches = [
                    refresh_symbols[i:i + BATCH_SIZE]
                    for i in range(0, len(refresh_symbols), BATCH_SIZE)
                ]

                total_updated = 0

                print(
                    "YALCIN PRO - CANLI YENILEME TURU BASLADI:",
                    len(refresh_symbols),
                    "HISSE /",
                    len(batches),
                    "GRUP"
                )

                for index, batch in enumerate(batches, start=1):
                    try:
                        print(
                            "YALCIN PRO - CANLI GRUP:",
                            index,
                            "/",
                            len(batches),
                            "|",
                            len(batch),
                            "HISSE"
                        )

                        results, missing = get_stocks_batch(batch)

                        for result in results:
                            symbol = normalize_symbol(
                                result.get("sembol")
                            )
                            if symbol:
                                _save_stock(symbol, result)
                                total_updated += 1

                        print(
                            "YALCIN PRO - CANLI GRUP TAMAM:",
                            index,
                            "/",
                            len(batches),
                            "| GUNCEL:",
                            len(results),
                            "| EKSIK:",
                            len(missing)
                        )

                    except Exception as e:
                        print(
                            "YALCIN PRO - CANLI GRUP HATASI:",
                            index,
                            e
                        )

                    time.sleep(1)

                print(
                    "YALCIN PRO - CANLI YENILEME TURU TAMAMLANDI:",
                    total_updated,
                    "/",
                    len(refresh_symbols),
                    "HISSE"
                )

                time.sleep(BACKGROUND_REFRESH_SECONDS)

            except Exception as e:
                print(
                    "YALCIN PRO - ARKA PLAN THREAD HATASI:",
                    e
                )
                time.sleep(5)

    thread = threading.Thread(
        target=worker,
        daemon=True,
        name="yalcin-cache-refresh"
    )
    thread.start()


_load_persistent_cache()


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

    symbol = normalize_symbol(
        sembol
    )

    result = get_stock(
        symbol
    )

    if result is None:

        return jsonify({
            "success": False,
            "data": []
        }), 404

    return jsonify({
        "success": True,
        "data": [
            result
        ]
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

    # İlk Android isteğinde sürekli canlı yenileme başlar.
    # Aynı server sürecinde ikinci bir thread açılmaz.
    start_background_refresh(symbols)

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
                get_stocks_batch(
                    batch
                )
            )

            results.extend(
                batch_results
            )

            print(
                "YALCIN PRO - "
                "GRUP TAMAMLANDI:",
                len(batch_results),
                "| EKSIK:",
                len(batch_missing),
                "| TOPLAM:",
                len(results)
            )


    except Exception as e:

        print(
            "YALCIN PRO - "
            "GENEL HATA:",
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
            result_map[
                symbol
            ] = item

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
            ", ".join(
                missing_symbols
            )
        )

        for retry_round in range(
            1,
            MISSING_RETRY_COUNT + 1
        ):

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
                if retry_index < len(
                    retry_batches
                ):

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
            ", ".join(
                final_missing
            )
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

    print(
        "YALCIN PRO - CACHE HAZIR:",
        len(_stock_cache),
        "HISSE"
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
