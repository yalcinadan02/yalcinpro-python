from flask import Flask, jsonify
import json
import os
import threading
import time
import urllib.request
import urllib.error
import re

app = Flask(__name__)

# ============================================================
# YALCIN PRO - UCRETSIZ / 15 DAKIKA GECIKMELI BIST
# ============================================================
# Kaynak:
#   Asenax / BIST /all
#
# Asenax dokumantasyonuna gore /bist/all/ tum hisseleri
# Midas kaynakli 15 dakika gecikmeli olarak verir.
#
# Render -> Asenax -> cache -> Android
#
# PC / PowerShell / ADB / 127.0.0.1:8000 KULLANILMAZ.
# ============================================================

TARGET = 614
REFRESH_SECONDS = 60

SOURCE_URL = "https://api.asenax.com/bist/all/"

SYMBOL_FILE = "yalcin_pro_active_symbols.json"
CACHE_FILE = "yalcin_pro_cache.json"

symbols = []
cache = {}

symbols_lock = threading.Lock()
cache_lock = threading.Lock()
refresh_lock = threading.Lock()

last_refresh = {
    "updated": 0,
    "missing": TARGET,
    "total": TARGET,
    "timestamp": 0,
    "status": "baslatiliyor",
}

background_started = False
background_lock = threading.Lock()


# ============================================================
# YARDIMCI
# ============================================================

def normalize_symbol(value):
    if value is None:
        return ""

    value = str(value).strip().upper()

    value = value.replace(".IS", "")
    value = value.replace("BIST:", "")
    value = re.sub(r"\s+", "", value)

    if not re.fullmatch(r"[A-Z0-9]{2,8}", value):
        return ""

    return value


def number(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()

    if not text:
        return None

    text = text.replace("%", "")
    text = text.replace("₺", "")
    text = text.replace("TL", "")
    text = text.replace("TRY", "")
    text = text.replace(" ", "")

    # Türkçe sayı biçimi
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "")
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")

    try:
        return float(text)
    except Exception:
        return None


def first_value(item, names):
    if not isinstance(item, dict):
        return None

    # Önce doğrudan anahtarlar
    lowered = {
        str(k).lower(): v
        for k, v in item.items()
    }

    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]

    # Daha esnek arama
    for key, value in item.items():
        k = str(key).lower().replace("_", "").replace("-", "")

        for name in names:
            n = name.lower().replace("_", "").replace("-", "")

            if k == n:
                return value

    return None


# ============================================================
# 614 SEMBOL
# ============================================================

def load_symbols():
    global symbols

    loaded = []

    try:
        with open(
            SYMBOL_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if isinstance(data, list):
            for item in data:
                s = normalize_symbol(item)

                if s and s not in loaded:
                    loaded.append(s)

    except Exception as e:
        print(
            "YALCIN PRO - SEMBOL DOSYASI HATASI:",
            repr(e),
        )

    with symbols_lock:
        symbols = loaded[:TARGET]

    print(
        "YALCIN PRO - AKTIF SEMBOL:",
        len(symbols),
        "/",
        TARGET,
    )


def load_cache():
    global cache

    try:
        if not os.path.exists(CACHE_FILE):
            return

        with open(
            CACHE_FILE,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if isinstance(data, dict):
            with cache_lock:
                cache = data

        print(
            "YALCIN PRO - CACHE YUKLENDI:",
            len(cache),
        )

    except Exception as e:
        print(
            "YALCIN PRO - CACHE HATASI:",
            repr(e),
        )


def save_cache():
    try:
        with cache_lock:
            data = dict(cache)

        temp = CACHE_FILE + ".tmp"

        with open(
            temp,
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
            )

        os.replace(
            temp,
            CACHE_FILE,
        )

    except Exception as e:
        print(
            "YALCIN PRO - CACHE KAYDETME HATASI:",
            repr(e),
        )


# ============================================================
# ASENAX /ALL
# ============================================================

def download_source():
    request = urllib.request.Request(
        SOURCE_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "Chrome/153.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=20,
        ) as response:

            raw = response.read()

        text = raw.decode(
            "utf-8-sig",
            errors="replace",
        )

        return json.loads(text)

    except Exception as e:
        print(
            "YALCIN PRO - ASENAX HATASI:",
            repr(e),
        )
        return None


def find_stock_array(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    # Olası wrapper alanları
    for key in (
        "data",
        "stocks",
        "results",
        "result",
        "items",
        "quotes",
    ):
        value = payload.get(key)

        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for nested_key in (
                "data",
                "stocks",
                "results",
                "items",
            ):
                nested = value.get(nested_key)

                if isinstance(nested, list):
                    return nested

    # İlk list alanını bul
    for value in payload.values():
        if isinstance(value, list):
            return value

    return []


def parse_source(payload):
    rows = find_stock_array(payload)

    parsed = {}

    for item in rows:
        if not isinstance(item, dict):
            continue

        symbol = first_value(
            item,
            [
                "symbol",
                "sembol",
                "code",
                "kod",
                "ticker",
                "hisse",
                "name",
            ],
        )

        symbol = normalize_symbol(symbol)

        if not symbol:
            continue

        price = first_value(
            item,
            [
                "price",
                "fiyat",
                "last",
                "lastPrice",
                "close",
                "kapanis",
                "current",
                "currentPrice",
            ],
        )

        previous = first_value(
            item,
            [
                "previousClose",
                "previous_close",
                "oncekiKapanis",
                "onceki_kapanis",
                "prevClose",
                "prev",
                "previous",
            ],
        )

        change = first_value(
            item,
            [
                "changePercent",
                "change_percent",
                "degisimYuzde",
                "degisim",
                "change",
                "percent",
                "percentage",
            ],
        )

        price = number(price)
        previous = number(previous)
        change = number(change)

        if price is None or price <= 0:
            continue

        # Degisim kaynaktan gelmiyorsa hesapla
        if change is None and previous and previous > 0:
            change = (
                (price - previous)
                / previous
                * 100
            )

        if change is None:
            change = 0.0

        parsed[symbol] = {
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

    return parsed


# ============================================================
# REFRESH
# ============================================================

def refresh_once():
    global cache
    global last_refresh

    if not refresh_lock.acquire(
        blocking=False
    ):
        return False

    try:
        print(
            "YALCIN PRO - ASENAX /ALL ALINIYOR..."
        )

        payload = download_source()

        if payload is None:
            last_refresh = {
                **last_refresh,
                "status": "kaynak_hatasi",
                "timestamp": time.time(),
            }
            return False

        parsed = parse_source(payload)

        print(
            "YALCIN PRO - ASENAX VERISI:",
            len(parsed),
            "HISSE",
        )

        with symbols_lock:
            wanted = set(symbols)

        # Sadece bizim 614'lük listeyi al
        selected = {
            s: parsed[s]
            for s in wanted
            if s in parsed
        }

        with cache_lock:
            # Yeni gelenleri ekle, eskileri koru
            cache.update(selected)
            count = len(cache)

        save_cache()

        last_refresh = {
            "updated": len(selected),
            "missing": max(
                0,
                TARGET - len(selected),
            ),
            "total": TARGET,
            "timestamp": time.time(),
            "status": (
                "guncel"
                if len(selected) > 0
                else "veri_yok"
            ),
        }

        print(
            "YALCIN PRO - CACHE:",
            count,
            "/",
            TARGET,
        )

        return len(selected) > 0

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
        refresh_lock.release()


def background_worker():
    print(
        "YALCIN PRO - ARKA PLAN BASLADI"
    )

    # İlk veri
    refresh_once()

    while True:
        time.sleep(
            REFRESH_SECONDS
        )
        refresh_once()


def start_background():
    global background_started

    with background_lock:
        if background_started:
            return

        background_started = True

    thread = threading.Thread(
        target=background_worker,
        daemon=True,
        name="yalcin-asenax",
    )

    thread.start()


# ============================================================
# API
# ============================================================

@app.route("/")
def home():
    with cache_lock:
        count = len(cache)

    return jsonify({
        "success": True,
        "message": "YALCIN PRO BIST SERVISI AKTIF",
        "source": "Asenax /all",
        "delay": "15 dakika",
        "target": TARGET,
        "data": count,
    })


@app.route("/health")
def health():
    with cache_lock:
        count = len(cache)

    with symbols_lock:
        symbol_count = len(symbols)

    return jsonify({
        "success": True,
        "status": "online",
        "symbols": symbol_count,
        "data": count,
        "target": TARGET,
        "lastRefresh": last_refresh,
        "serverTime": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    })


@app.route("/stats")
def stats():
    with cache_lock:
        count = len(cache)

    return jsonify({
        "success": True,
        "target": TARGET,
        "symbols": len(symbols),
        "data": count,
        "lastRefresh": last_refresh,
    })


@app.route("/symbols")
def symbol_list():
    return jsonify({
        "success": len(symbols) == TARGET,
        "count": len(symbols),
        "symbols": symbols,
    })


def current_data():
    with cache_lock:
        snapshot = dict(cache)

    with symbols_lock:
        wanted = list(symbols)

    return [
        snapshot[s]
        for s in wanted
        if s in snapshot
    ]


@app.route("/all")
def all_stocks():
    data = current_data()

    if not data:
        # Kullanici /all acinca ilk veriyi beklemeden
        # bir kez daha dene.
        refresh_once()
        data = current_data()

    if not data:
        return jsonify({
            "success": False,
            "data": [],
            "error": "Veri henuz hazir degil",
        }), 503

    return jsonify({
        "success": True,
        "count": len(data),
        "data": data,
    })


@app.route("/stocks")
def stocks():
    return all_stocks()


@app.route("/stock/<symbol>")
def stock(symbol):
    s = normalize_symbol(symbol)

    with cache_lock:
        item = cache.get(s)

    if not item:
        refresh_once()

        with cache_lock:
            item = cache.get(s)

    if not item:
        return jsonify({
            "success": False,
            "data": [],
            "error": f"{s} bulunamadi",
        }), 404

    return jsonify({
        "success": True,
        "data": [item],
    })


@app.route("/refresh")
def manual_refresh():
    ok = refresh_once()

    with cache_lock:
        count = len(cache)

    return jsonify({
        "success": ok,
        "data": count,
        "target": TARGET,
        "lastRefresh": last_refresh,
    })


# ============================================================
# RENDER / GUNICORN
# ============================================================

load_symbols()
load_cache()
start_background()


if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            "5000",
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
    )
