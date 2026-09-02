from flask import Flask, jsonify, request
import yfinance as yf
import concurrent.futures
import threading
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")

# =============================================================
# AYARLAR
# =============================================================

BATCH_SIZE = 25
MAX_WORKERS = 2

MISSING_RETRY_COUNT = 3
RETRY_WAIT_SECONDS = 1.0

CACHE_TTL_SECONDS = 20

_stock_cache = {}
_cache_lock = threading.Lock()

# Arka plan cache yenileme durumu
_warm_lock = threading.Lock()
_warm_thread = None
_warm_symbols = []
_warm_running = False
_warm_last_started = 0.0
WARM_INTERVAL_SECONDS = 25


# =============================================================
# CACHE DOSYASI YAZICI
# =============================================================

def _persistent_writer_loop():
    global _cache_dirty

    while True:
        try:
            time.sleep(10)

            with _cache_lock:
                dirty = _cache_dirty
                _cache_dirty = False

            if dirty:
                _save_persistent_cache()
                print(
                    "YALCIN PRO - PERSISTENT CACHE KAYDEDILDI:",
                    len(_stock_cache),
                    "HISSE"
                )

        except Exception as e:
            print("YALCIN PRO - CACHE YAZICI HATASI:", e)
            time.sleep(5)


def start_persistent_writer():
    threading.Thread(
        target=_persistent_writer_loop,
        daemon=True,
        name="yalcin-cache-writer"
    ).start()


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
    # Stale-while-revalidate: eski veri silinmez. Böylece cache yenilenirken
    # Android boş liste görmez; arka plan yeni veriyi üzerine yazar.
    with _cache_lock:
        item = _stock_cache.get(symbol)
        if item:
            return item[1]
    return None


_cache_dirty = False

def _save_stock(symbol, result):
    global _cache_dirty

    with _cache_lock:
        _stock_cache[symbol] = (time.time(), result)
        _cache_dirty = True


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
# ARKA PLAN CACHE ISITMA / YENILEME
# =============================================================

def _cache_snapshot_count(symbols):
    count = 0
    with _cache_lock:
        for symbol in symbols:
            item = _stock_cache.get(symbol)
            if item is not None:
                count += 1
    return count


def _warm_cache_once(symbols):
    """Yahoo'dan verileri bloklamadan arka planda toplar."""
    global _warm_running

    normalized = list(dict.fromkeys(
        normalize_symbol(s) for s in symbols if normalize_symbol(s)
    ))

    batches = [
        normalized[i:i + BATCH_SIZE]
        for i in range(0, len(normalized), BATCH_SIZE)
    ]

    print("YALCIN PRO - ARKA PLAN CACHE BASLIYOR:", len(normalized), "HISSE | GRUP:", len(batches))

    total_before = _cache_snapshot_count(normalized)

    try:
        # Batch'leri paralel çalıştırıyoruz; her batch kendi Yahoo isteğini yapar.
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_map = {
                executor.submit(get_stocks_batch, batch): index
                for index, batch in enumerate(batches, start=1)
            }

            completed = 0
            for future in concurrent.futures.as_completed(future_map):
                batch_index = future_map[future]
                try:
                    results, missing = future.result()
                    completed += len(results)
                    print(
                        f"YALCIN PRO - CACHE GRUP TAMAM: {batch_index}/{len(batches)} | "
                        f"GELEN={len(results)} | EKSIK={len(missing)} | "
                        f"CACHE={_cache_snapshot_count(normalized)}/{len(normalized)}"
                    )
                except Exception as e:
                    print(
                        f"YALCIN PRO - CACHE GRUP HATASI {batch_index}: {e}"
                    )

    finally:
        _warm_running = False
        final_count = _cache_snapshot_count(normalized)
        print(
            "YALCIN PRO - ARKA PLAN CACHE BITTI:",
            final_count,
            "/",
            len(normalized),
            "| ONCEDEN:",
            total_before
        )


def _fetch_and_cache_batch(batch, batch_index, total_batches):
    try:
        results, missing = get_stocks_batch(batch)
        print(
            "YALCIN PRO - CACHE GRUP TAMAM:",
            f"{batch_index}/{total_batches}",
            "| GELEN=", len(results),
            "| EKSIK=", len(missing),
            "| CACHE=", len(_stock_cache)
        )
        return len(results)
    except Exception as e:
        print("YALCIN PRO - CACHE GRUP HATASI:", e)
        return 0


_warm_lock = threading.Lock()
_warm_thread = None
_warm_symbols = []
_warm_running = False


def _warm_cache_once(symbols):
    normalized = list(dict.fromkeys(
        normalize_symbol(s) for s in symbols if normalize_symbol(s)
    ))

    todo = []
    now = time.time()

    with _cache_lock:
        for symbol in normalized:
            item = _stock_cache.get(symbol)
            if item is None or now - item[0] >= CACHE_TTL_SECONDS:
                todo.append(symbol)

    if not todo:
        print("YALCIN PRO - CACHE GUNCEL:", len(normalized), "/", len(normalized))
        return

    batches = [
        todo[i:i + BATCH_SIZE]
        for i in range(0, len(todo), BATCH_SIZE)
    ]

    print(
        "YALCIN PRO - CACHE ISITMA:",
        len(todo), "HISSE |", len(batches), "GRUP"
    )

    # Yahoo'yu boğmamak için aynı anda en fazla 2 grup.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:
        futures = [
            executor.submit(
                _fetch_and_cache_batch,
                batch,
                i,
                len(batches)
            )
            for i, batch in enumerate(batches, start=1)
        ]

        for future in futures:
            try:
                future.result()
            except Exception as e:
                print("YALCIN PRO - CACHE FUTURE HATASI:", e)

    print(
        "YALCIN PRO - CACHE ISITMA TURU BITTI:",
        len(_stock_cache), "/", len(normalized)
    )


def _warm_cache_loop():
    global _warm_running

    print("YALCIN PRO - ARKA PLAN CACHE ISITICI BASLADI")

    while True:
        try:
            with _warm_lock:
                symbols = list(_warm_symbols)

            if not symbols:
                _warm_running = False
                time.sleep(2)
                continue

            _warm_running = True
            _warm_cache_once(symbols)

            # Bir tam tur bittikten sonra yeniden güncelle.
            time.sleep(BACKGROUND_REFRESH_SECONDS)

        except Exception as e:
            _warm_running = False
            print("YALCIN PRO - ISITICI HATASI:", e)
            time.sleep(5)


def start_background_warm(symbols):
    global _warm_thread, _warm_symbols

    normalized = list(dict.fromkeys(
        normalize_symbol(s) for s in symbols if normalize_symbol(s)
    ))

    if not normalized:
        return

    with _warm_lock:
        _warm_symbols = normalized

        if _warm_thread is not None and _warm_thread.is_alive():
            print("YALCIN PRO - CACHE ISITICI ZATEN CALISIYOR")
            return

        _warm_thread = threading.Thread(
            target=_warm_cache_loop,
            daemon=True,
            name="yalcin-cache-warmer"
        )
        _warm_thread.start()

        print(
            "YALCIN PRO - CACHE ISITICI BASLATILDI:",
            len(normalized), "HISSE"
        )


_load_persistent_cache()
start_persistent_writer()


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
# HIZLI CACHE
# =============================================================

@app.route("/stocks/cache")
def stocks_cache():
    """
    Android'a hazır cache'i döndürür.
    Cache boş/eksikse Yahoo sorgusu burada BEKLETİLMEZ; arka plan ısıtıcısı
    başlatılır ve mevcut veriler anında döndürülür.
    """
    symbols_text = request.args.get("symbols", "")

    if not symbols_text:
        return jsonify({
            "success": False,
            "error": "symbols parametresi gerekli",
            "data": []
        }), 400

    symbols = list(dict.fromkeys(
        normalize_symbol(s)
        for s in symbols_text.split(",")
        if s.strip()
    ))

    # İlk Android isteği cache ısıtıcısına sembol evrenini öğretir.
    start_background_warm(symbols)

    cached_results = []
    missing = []

    for symbol in symbols:
        cached = _cached_stock(symbol)
        if cached is not None:
            cached_results.append(cached)
        else:
            missing.append(symbol)

    cache_map = {item["sembol"]: item for item in cached_results}
    ordered = [cache_map[symbol] for symbol in symbols if symbol in cache_map]

    print(
        "YALCIN PRO - CACHE CEVAP:",
        len(ordered),
        "/",
        len(symbols),
        "| EKSIK:",
        len(missing),
        "| ISITICI:",
        "CALISIYOR" if _warm_running else "HAZIR"
    )

    return jsonify({
        "success": True,
        "cached": len(ordered),
        "missing": len(missing),
        "warming": _warm_running,
        "data": ordered
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

            # Yahoo'yu yormamak için bekle
            if batch_index < len(batches):

                time.sleep(
                    1.5
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
