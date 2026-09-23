from flask import Flask, jsonify
import concurrent.futures
import json
import os
import re
import threading
import time

import yfinance as yf

app = Flask(__name__)

# ============================================================
# YALCIN PRO - UCRETSIZ / GECIKMELI BIST VERI
# ============================================================
# Render + Gunicorn icin hazir.
#
# Telefon:
#   Wi-Fi / Mobil Veri
#          |
#          v
#   https://yalcinpro-python.onrender.com
#          |
#          v
#      /stocks veya /all
#
# Veri kaynagi: Yahoo Finance / yfinance (gecikmeli)
#
# ONEMLI:
# - PC'deki 127.0.0.1:8000 KULLANILMAZ.
# - ADB KULLANILMAZ.
# - Render'da "gunicorn server:app" ile calisir.
# - 614 aktif sembol cache'i kullanilir.
# ============================================================

TARGET = 614
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "300"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "25"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))

ACTIVE_CACHE_FILE = os.environ.get(
    "ACTIVE_CACHE_FILE",
    "yalcin_pro_active_symbols.json",
)

PERSISTENT_CACHE_FILE = os.environ.get(
    "PERSISTENT_CACHE_FILE",
    "yalcin_pro_cache.json",
)

INVALID_SYMBOLS = {
    "DRT", "PWC", "KPMG", "TTK", "BDO", "PKF",
    "RSM", "BD", "CNS", "HSY", "KARAR", "REFORM",
}

_symbol_list = []
_symbol_lock = threading.Lock()

_snapshot = {}
_snapshot_time = 0.0
_snapshot_lock = threading.Lock()

_background_started = False
_background_lock = threading.Lock()

_refresh_lock = threading.Lock()

last_refresh = {
    "updated": 0,
    "missing": TARGET,
    "total": TARGET,
    "timestamp": 0,
    "status": "baslatiliyor",
}


# ============================================================
# YARDIMCI FONKSIYONLAR
# ============================================================

def normalize_symbol(value):
    if not value:
        return ""

    s = (
        str(value)
        .strip()
        .upper()
        .replace(".IS", "")
        .replace("\xa0", "")
        .replace("\u200b", "")
        .replace("\ufeff", "")
    )

    s = re.sub(r"\s+", "", s)

    if s in INVALID_SYMBOLS:
        return ""

    if not re.fullmatch(r"[A-Z0-9]{2,8}", s):
        return ""

    return s


def safe_float(value):
    try:
        if value is None:
            return None

        value = float(value)

        if value != value:
            return None

        return value

    except Exception:
        return None


def chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ============================================================
# AKTIF 614 HISSE LISTESI
# ============================================================

def load_active_symbols():
    global _symbol_list

    symbols = []

    try:
        if os.path.exists(ACTIVE_CACHE_FILE):
            with open(
                ACTIVE_CACHE_FILE,
                "r",
                encoding="utf-8",
            ) as f:
                data = json.load(f)

            if isinstance(data, list):
                for item in data:
                    symbol = normalize_symbol(item)

                    if symbol and symbol not in symbols:
                        symbols.append(symbol)

    except Exception as e:
        print(
            "YALCIN PRO - AKTIF SEMBOL CACHE HATASI:",
            repr(e),
        )

    with _symbol_lock:
        _symbol_list = symbols[:TARGET]

    print(
        "YALCIN PRO - AKTIF SEMBOL CACHE:",
        len(_symbol_list),
        "/",
        TARGET,
    )

    return list(_symbol_list)


def save_active_symbols(symbols):
    try:
        cleaned = []

        for item in symbols:
            symbol = normalize_symbol(item)

            if symbol and symbol not in cleaned:
                cleaned.append(symbol)

        cleaned = cleaned[:TARGET]

        temp_file = ACTIVE_CACHE_FILE + ".tmp"

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                cleaned,
                f,
                ensure_ascii=False,
                indent=2,
            )

        os.replace(
            temp_file,
            ACTIVE_CACHE_FILE,
        )

    except Exception as e:
        print(
            "YALCIN PRO - SEMBOL CACHE KAYDETME HATASI:",
            repr(e),
        )


def get_symbols():
    with _symbol_lock:
        return list(_symbol_list[:TARGET])


# ============================================================
# KALICI FIYAT CACHE
# ============================================================

def load_persistent_cache():
    global _snapshot, _snapshot_time

    try:
        if not os.path.exists(PERSISTENT_CACHE_FILE):
            return

        with open(
            PERSISTENT_CACHE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return

        cleaned = {}

        for key, value in data.items():
            symbol = normalize_symbol(key)

            if not symbol or not isinstance(value, dict):
                continue

            price = safe_float(value.get("fiyat"))
            previous = safe_float(
                value.get("oncekiKapanis")
            )
            change = safe_float(
                value.get("degisimYuzde")
            )

            if price is None or price <= 0:
                continue

            if change is None:
                change = 0.0

            cleaned[symbol] = {
                "sembol": symbol,
                "fiyat": round(price, 2),
                "oncekiKapanis": (
                    round(previous, 2)
                    if previous is not None
                    else None
                ),
                "degisimYuzde": round(change, 2),
                "paraBirimi": "TRY",
            }

        if cleaned:
            with _snapshot_lock:
                _snapshot = cleaned
                _snapshot_time = time.time()

            print(
                "YALCIN PRO - KALICI CACHE:",
                len(cleaned),
                "HISSE",
            )

    except Exception as e:
        print(
            "YALCIN PRO - KALICI CACHE HATASI:",
            repr(e),
        )


def save_persistent_cache(snapshot):
    try:
        temp_file = PERSISTENT_CACHE_FILE + ".tmp"

        with open(
            temp_file,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                snapshot,
                f,
                ensure_ascii=False,
            )

        os.replace(
            temp_file,
            PERSISTENT_CACHE_FILE,
        )

    except Exception as e:
        print(
            "YALCIN PRO - FIYAT CACHE KAYDETME HATASI:",
            repr(e),
        )


# ============================================================
# YAHOO FINANCE
# ============================================================

def fetch_batch(symbols):
    if not symbols:
        return {}

    tickers = [
        symbol + ".IS"
        for symbol in symbols
    ]

    print(
        "YALCIN PRO - YAHOO BATCH:",
        len(tickers),
    )

    try:
        data = yf.download(
            tickers=tickers,
            period="5d",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            actions=False,
            threads=False,
            progress=False,
        )

    except Exception as e:
        print(
            "YALCIN PRO - YAHOO BATCH HATASI:",
            repr(e),
        )
        return {}

    result = {}

    try:
        is_multi = (
            hasattr(data.columns, "levels")
            and len(data.columns.levels) >= 2
        )

        for symbol in symbols:
            ticker = symbol + ".IS"

            try:
                if is_multi:
                    if ticker not in data.columns.levels[0]:
                        continue

                    frame = data[ticker]
                else:
                    frame = data

                if frame is None or frame.empty:
                    continue

                if "Close" not in frame.columns:
                    continue

                closes = frame["Close"].dropna()

                if len(closes) < 1:
                    continue

                price = safe_float(
                    closes.iloc[-1]
                )

                if price is None or price <= 0:
                    continue

                previous = None

                if len(closes) >= 2:
                    previous = safe_float(
                        closes.iloc[-2]
                    )

                if previous is not None and previous > 0:
                    change = (
                        (price - previous)
                        / previous
                        * 100.0
                    )
                else:
                    change = 0.0

                result[symbol] = {
                    "sembol": symbol,
                    "fiyat": round(price, 2),
                    "oncekiKapanis": (
                        round(previous, 2)
                        if previous is not None
                        else None
                    ),
                    "degisimYuzde": round(
                        change,
                        2,
                    ),
                    "paraBirimi": "TRY",
                }

            except Exception as e:
                print(
                    "YALCIN PRO - HISSE HATASI:",
                    symbol,
                    repr(e),
                )

    except Exception as e:
        print(
            "YALCIN PRO - BATCH PARSE HATASI:",
            repr(e),
        )

    print(
        "YALCIN PRO - BATCH SONUCU:",
        len(result),
        "/",
        len(symbols),
    )

    return result


def fetch_all_stocks():
    symbols = get_symbols()

    if len(symbols) < TARGET:
        print(
            "YALCIN PRO - 614 SEMBOL YOK:",
            len(symbols),
            "/",
            TARGET,
        )
        return {}

    all_result = {}

    batches = list(
        chunked(
            symbols,
            BATCH_SIZE,
        )
    )

    print(
        "YALCIN PRO - YENILEME:",
        len(symbols),
        "HISSE |",
        len(batches),
        "BATCH",
    )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = [
            executor.submit(
                fetch_batch,
                batch,
            )
            for batch in batches
        ]

        for future in concurrent.futures.as_completed(
            futures
        ):
            try:
                batch_result = future.result()

                if batch_result:
                    all_result.update(
                        batch_result
                    )

            except Exception as e:
                print(
                    "YALCIN PRO - FUTURE HATASI:",
                    repr(e),
                )

    print(
        "YALCIN PRO - TOPLAM VERI:",
        len(all_result),
        "/",
        TARGET,
    )

    return all_result


# ============================================================
# REFRESH
# ============================================================

def refresh_once(force=False):
    global _snapshot
    global _snapshot_time
    global last_refresh

    if not _refresh_lock.acquire(
        blocking=False
    ):
        print(
            "YALCIN PRO - ZATEN YENILENIYOR"
        )
        return False

    try:
        with _snapshot_lock:
            old_snapshot = dict(_snapshot)

        new_snapshot = fetch_all_stocks()

        if len(new_snapshot) >= TARGET:
            with _snapshot_lock:
                _snapshot = new_snapshot
                _snapshot_time = time.time()

            save_persistent_cache(
                new_snapshot
            )

            last_refresh = {
                "updated": len(new_snapshot),
                "missing": TARGET - len(new_snapshot),
                "total": TARGET,
                "timestamp": time.time(),
                "status": "guncel",
            }

            print(
                "YALCIN PRO - YENILEME TAMAMLANDI:",
                len(new_snapshot),
                "/",
                TARGET,
            )

            return True

        # Eksik veri varsa eski cache'i koru.
        merged = dict(old_snapshot)
        merged.update(new_snapshot)

        if len(merged) > len(old_snapshot):
            with _snapshot_lock:
                _snapshot = merged
                _snapshot_time = time.time()

            save_persistent_cache(
                merged
            )

        last_refresh = {
            "updated": len(new_snapshot),
            "missing": TARGET - len(new_snapshot),
            "total": TARGET,
            "timestamp": time.time(),
            "status": (
                "kismi_veri"
                if new_snapshot
                else "veri_alinamadi"
            ),
        }

        print(
            "YALCIN PRO - KISMI YENILEME:",
            len(new_snapshot),
            "/",
            TARGET,
            "| CACHE:",
            len(merged),
        )

        return bool(new_snapshot)

    except Exception as e:
        print(
            "YALCIN PRO - REFRESH HATASI:",
            repr(e),
        )

        last_refresh = {
            **last_refresh,
            "status": "hata",
            "timestamp": time.time(),
        }

        return False

    finally:
        _refresh_lock.release()


def background_worker():
    print(
        "YALCIN PRO - ARKA PLAN BASLADI"
    )

    refresh_once(force=True)

    while True:
        time.sleep(
            REFRESH_SECONDS
        )

        try:
            refresh_once()
        except Exception as e:
            print(
                "YALCIN PRO - ARKA PLAN HATASI:",
                repr(e),
            )


def start_background():
    global _background_started

    with _background_lock:
        if _background_started:
            return

        _background_started = True

    thread = threading.Thread(
        target=background_worker,
        daemon=True,
        name="yalcin-refresh",
    )

    thread.start()


# ============================================================
# API
# ============================================================

@app.route("/")
def home():
    with _snapshot_lock:
        data_count = len(_snapshot)

    return jsonify({
        "success": True,
        "message": "Yalcin Pro BIST Veri Servisi calisiyor",
        "target": TARGET,
        "symbols": len(get_symbols()),
        "data": data_count,
    })


@app.route("/health")
def health():
    with _snapshot_lock:
        data_count = len(_snapshot)

    return jsonify({
        "success": True,
        "status": "online",
        "symbols": len(get_symbols()),
        "data": data_count,
        "target": TARGET,
        "lastRefresh": last_refresh,
        "serverTime": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    })


@app.route("/stats")
def stats():
    with _snapshot_lock:
        data_count = len(_snapshot)

    return jsonify({
        "success": True,
        "target": TARGET,
        "symbols": len(get_symbols()),
        "data": data_count,
        "lastRefresh": last_refresh,
    })


@app.route("/symbols")
def symbols():
    data = get_symbols()

    return jsonify({
        "success": len(data) == TARGET,
        "count": len(data),
        "symbols": data,
    })


def get_stock_data():
    with _snapshot_lock:
        snapshot = dict(_snapshot)

    symbols = get_symbols()

    if len(snapshot) < TARGET:
        refresh_once(force=True)

        with _snapshot_lock:
            snapshot = dict(_snapshot)

    data = []

    for symbol in symbols:
        item = snapshot.get(symbol)

        if item:
            data.append(item)

    return data


@app.route("/stocks")
def stocks():
    data = get_stock_data()

    if not data:
        return jsonify({
            "success": False,
            "data": [],
            "error": (
                "Henuz veri hazir degil. "
                "Lutfen tekrar deneyin."
            ),
        }), 503

    print(
        "YALCIN PRO - /stocks:",
        len(data),
        "/",
        TARGET,
    )

    return jsonify({
        "success": True,
        "data": data,
    })


@app.route("/all")
def all_stocks():
    data = get_stock_data()

    if not data:
        return jsonify({
            "success": False,
            "data": [],
            "error": "Henuz veri hazir degil.",
        }), 503

    print(
        "YALCIN PRO - /all:",
        len(data),
        "/",
        TARGET,
    )

    return jsonify({
        "success": True,
        "data": data,
    })


@app.route("/stock/<symbol>")
def stock(symbol):
    s = normalize_symbol(symbol)

    with _snapshot_lock:
        item = _snapshot.get(s)

    if item is None:
        refresh_once(force=True)

        with _snapshot_lock:
            item = _snapshot.get(s)

    if item is None:
        return jsonify({
            "success": False,
            "data": [],
            "error": f"{s} icin veri bulunamadi",
        }), 404

    return jsonify({
        "success": True,
        "data": [item],
    })


@app.route("/refresh")
def manual_refresh():
    ok = refresh_once(force=True)

    with _snapshot_lock:
        count = len(_snapshot)

    return jsonify({
        "success": ok,
        "dataCount": count,
        "target": TARGET,
        "lastRefresh": last_refresh,
    })


# ============================================================
# GUNICORN / RENDER BASLANGICI
# ============================================================
# Render:
#   gunicorn server:app
#
# __main__ bloguna guvenmiyoruz; Gunicorn bu dosyayi import
# ettiginde cache ve arka plan yenilemesi burada baslar.
# ============================================================

load_active_symbols()
load_persistent_cache()
start_background()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                "5000",
            )
        ),
        debug=False,
        threaded=True,
    )
