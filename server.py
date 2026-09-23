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
# Bilgisayar / PowerShell / ADB gerekmez.
# ============================================================

TARGET = 614
SYMBOL_FILE = "yalcin_pro_active_symbols.json"

# Yahoo'yu gereksiz yere hizli bombardimana tutmamak icin
# kontrollu paralellik.
WORKERS = 6
REQUEST_TIMEOUT = 15
RETRY_COUNT = 2
REFRESH_SECONDS = 60

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}.IS"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
})

cache = {}
last_refresh = {
    "timestamp": 0,
    "updated": 0,
    "missing": TARGET,
    "total": TARGET,
    "status": "baslatiliyor",
}
refresh_lock = threading.Lock()


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_symbols():
    path = os.path.join(os.path.dirname(__file__), SYMBOL_FILE)

    if not os.path.exists(path):
        print("YALCIN PRO - SEMBOL DOSYASI YOK:", path)
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)

        found = []

        def add(value):
            if isinstance(value, str):
                s = value.strip().upper()
                s = s.replace(".IS", "")
                if 2 <= len(s) <= 8 and s.isalnum():
                    if s not in found:
                        found.append(s)

        if isinstance(obj, list):
            for x in obj:
                if isinstance(x, str):
                    add(x)
                elif isinstance(x, dict):
                    add(x.get("symbol") or x.get("sembol") or x.get("code"))

        elif isinstance(obj, dict):
            # Liste iceren ilk uygun alani bul
            for key in ("symbols", "semboller", "data", "stocks"):
                value = obj.get(key)
                if isinstance(value, list):
                    for x in value:
                        if isinstance(x, str):
                            add(x)
                        elif isinstance(x, dict):
                            add(x.get("symbol") or x.get("sembol") or x.get("code"))
                    break

        print(f"YALCIN PRO - AKTIF SEMBOL: {len(found)} / {TARGET}")
        return found[:TARGET]

    except Exception as e:
        print("YALCIN PRO - SEMBOL OKUMA HATASI:", repr(e))
        return []


SYMBOLS = load_symbols()


def fetch_one(symbol):
    """Tek hisse icin Yahoo Chart verisini alir."""
    url = YAHOO_URL.format(symbol)

    for attempt in range(RETRY_COUNT + 1):
        try:
            r = session.get(
                url,
                params={
                    "range": "1d",
                    "interval": "1d",
                    "events": "div,splits",
                    "includeAdjustedClose": "true",
                },
                timeout=REQUEST_TIMEOUT,
            )

            if r.status_code == 429:
                # Rate limit: biraz bekleyip tekrar dene.
                time.sleep(1.5 * (attempt + 1))
                continue

            if r.status_code != 200:
                return None

            payload = r.json()
            result = (
                payload.get("chart", {})
                .get("result", [])
            )

            if not result:
                return None

            meta = result[0].get("meta", {})

            price = meta.get("regularMarketPrice")
            previous = meta.get("previousClose")

            # Bazı cevaplarda regularMarketPrice olmayabilir.
            if price is None:
                indicators = result[0].get("indicators", {})
                quote = indicators.get("quote", [])

                if quote:
                    closes = quote[0].get("close", [])
                    closes = [x for x in closes if x is not None]
                    if closes:
                        price = closes[-1]

            if price is None:
                return None

            price = float(price)

            if previous is not None:
                previous = float(previous)

            change = None
            if previous not in (None, 0):
                change = ((price - previous) / previous) * 100.0

            return {
                "sembol": symbol,
                "kod": symbol,
                "symbol": symbol,
                "code": symbol,
                "fiyat": round(price, 4),
                "price": round(price, 4),
                "oncekiKapanis": (
                    round(previous, 4)
                    if previous is not None else None
                ),
                "previousClose": (
                    round(previous, 4)
                    if previous is not None else None
                ),
                "degisimYuzde": (
                    round(change, 4)
                    if change is not None else 0.0
                ),
                "changePercent": (
                    round(change, 4)
                    if change is not None else 0.0
                ),
                "paraBirimi": "TRY",
                "currency": "TRY",
            }

        except Exception:
            if attempt < RETRY_COUNT:
                time.sleep(0.8 * (attempt + 1))
            else:
                return None

    return None


def refresh_all(force=False):
    global cache, last_refresh

    if not refresh_lock.acquire(blocking=False):
        return len(cache)

    try:
        if not force and cache:
            age = time.time() - last_refresh["timestamp"]
            if age < REFRESH_SECONDS:
                return len(cache)

        total = len(SYMBOLS)

        if total == 0:
            last_refresh.update({
                "status": "sembol_yok",
                "timestamp": time.time(),
                "total": TARGET,
                "missing": TARGET,
            })
            return 0

        last_refresh.update({
            "status": "veri_aliniyor",
            "total": total,
            "missing": total,
        })

        print(
            f"YALCIN PRO - YAHOO BASLIYOR: "
            f"{total} HISSE | {WORKERS} WORKER"
        )

        new_cache = {}
        completed = 0

        # 6 paralel istek. 429 gorulurse fetch_one geri cekiliyor.
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = {
                executor.submit(fetch_one, s): s
                for s in SYMBOLS
            }

            for future in as_completed(futures):
                symbol = futures[future]
                completed += 1

                try:
                    item = future.result()
                except Exception:
                    item = None

                if item:
                    new_cache[symbol] = item

                if completed % 25 == 0 or completed == total:
                    print(
                        f"YALCIN PRO - YAHOO: "
                        f"{completed}/{total} | "
                        f"VERI={len(new_cache)}"
                    )

        # Eski cache'de veri varsa tamamen silme.
        # Yeni basarili verileri guncelle.
        if new_cache:
            cache.update(new_cache)

        missing = max(total - len(cache), 0)

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

        print(
            f"YALCIN PRO - CACHE: "
            f"{len(cache)}/{total} | "
            f"YENI: {len(new_cache)} | "
            f"MISSING: {missing}"
        )

        return len(cache)

    finally:
        refresh_lock.release()


def background_loop():
    print("YALCIN PRO - ARKA PLAN BASLADI")

    while True:
        try:
            # Ilk veriyi hemen almaya calis.
            refresh_all(force=(not cache))
        except Exception as e:
            print("YALCIN PRO - ARKA PLAN HATASI:", repr(e))

        time.sleep(REFRESH_SECONDS)


@app.route("/")
def home():
    return jsonify({
        "success": True,
        "status": "online",
        "service": "Yalcin Pro BIST",
        "source": "Yahoo Finance Chart",
        "delay": "Yahoo tarafindaki mevcut piyasa gecikmesi",
        "symbols": len(SYMBOLS),
        "target": TARGET,
        "data": len(cache),
    })


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


@app.route("/stats")
def stats():
    return jsonify({
        "success": True,
        "symbols": len(SYMBOLS),
        "target": TARGET,
        "cached": len(cache),
        "missing": max(len(SYMBOLS) - len(cache), 0),
        "lastRefresh": last_refresh,
    })


@app.route("/symbols")
def symbols():
    return jsonify({
        "success": True,
        "count": len(SYMBOLS),
        "symbols": SYMBOLS,
    })


@app.route("/all")
def all_stocks():
    # Cache bos ise istegi yapan telefona veri hazirla.
    if not cache:
        print("YALCIN PRO - /all CACHE BOS -> YAHOO VERISI CEKILIYOR")
        refresh_all(force=True)

    data = [
        cache[s]
        for s in SYMBOLS
        if s in cache
    ]

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


@app.route("/stock/<symbol>")
def stock(symbol):
    symbol = symbol.upper().replace(".IS", "").strip()

    item = cache.get(symbol)

    if item is None:
        item = fetch_one(symbol)

        if item:
            cache[symbol] = item

    if item is None:
        return jsonify({
            "success": False,
            "symbol": symbol,
            "data": None,
        }), 404

    return jsonify({
        "success": True,
        "status": "online",
        "serverTime": now_text(),
        "data": item,
    })


@app.route("/refresh")
def manual_refresh():
    count = refresh_all(force=True)

    return jsonify({
        "success": count > 0,
        "status": last_refresh["status"],
        "data": count,
        "target": len(SYMBOLS),
        "lastRefresh": last_refresh,
    })


if __name__ == "__main__":
    # Yerel calistirma icin.
    t = threading.Thread(
        target=background_loop,
        daemon=True,
    )
    t.start()

    port = int(os.environ.get("PORT", "5000"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        threaded=True,
    )
