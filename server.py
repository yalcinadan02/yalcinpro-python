import json
import os
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify

app = Flask(__name__)

# ============================================================
# YALCIN PRO - UCRETSIZ / GECIKMELI BIST SUNUCUSU
# Kaynak: Asenax BIST /all (Midas kaynakli, yaklasik 15 dk gecikmeli)
# ============================================================

SOURCE_URL = "https://api.asenax.com/bist/all/"
SYMBOL_FILE = "yalcin_pro_active_symbols.json"

TARGET = 614
REFRESH_SECONDS = 60
REQUEST_TIMEOUT = 25

cache = {}
last_refresh = {
    "timestamp": 0,
    "updated": 0,
    "missing": TARGET,
    "total": TARGET,
    "status": "baslatılıyor",
}

refresh_lock = threading.Lock()


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_symbols():
    """GitHub'daki 614 sembollük JSON dosyasını mümkün olduğunca esnek okur."""
    candidates = [
        os.path.join(os.path.dirname(__file__), SYMBOL_FILE),
        SYMBOL_FILE,
    ]

    path = next((p for p in candidates if os.path.exists(p)), None)
    if not path:
        print("YALCIN PRO - SEMBOL DOSYASI BULUNAMADI")
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)

        values = []

        def walk(x):
            if isinstance(x, str):
                s = x.strip().upper()
                if 2 <= len(s) <= 8 and s.replace(".", "").isalnum():
                    values.append(s)
            elif isinstance(x, list):
                for item in x:
                    walk(item)
            elif isinstance(x, dict):
                # Once "symbol/code/sembol" gibi alanlara bak.
                for key in ("symbol", "code", "sembol", "ticker"):
                    if key in x:
                        walk(x[key])
                # Gerekirse tum alt yapilari tara.
                for key, value in x.items():
                    if key not in ("symbol", "code", "sembol", "ticker"):
                        if isinstance(value, (list, dict)):
                            walk(value)

        walk(obj)

        # Sirayi koru, tekrarları kaldır.
        result = []
        seen = set()
        for s in values:
            if s not in seen:
                seen.add(s)
                result.append(s)

        if result:
            print(f"YALCIN PRO - AKTIF SEMBOL CACHE: {len(result)} HISSE")
        return result

    except Exception as e:
        print("YALCIN PRO - SEMBOL OKUMA HATASI:", repr(e))
        return []


SYMBOLS = load_symbols()
if not SYMBOLS:
    # Dosya yoksa uygulamanın tamamen çalışmasını engelleme.
    SYMBOLS = []


def first_value(d, keys, default=None):
    if not isinstance(d, dict):
        return default
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


def to_float(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    try:
        text = str(value).strip()
        if not text:
            return None
        # Türkçe sayı formatı desteği
        text = text.replace("%", "").replace(" ", "")
        if "," in text and "." in text:
            # 1.234,56
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", ".")
        return float(text)
    except Exception:
        return None


def unwrap_source(obj):
    """Asenax cevabı liste, data alanı veya iç içe JSON olabilir."""
    if isinstance(obj, str):
        try:
            return unwrap_source(json.loads(obj))
        except Exception:
            return []

    if isinstance(obj, list):
        return obj

    if isinstance(obj, dict):
        for key in ("data", "result", "stocks", "items", "rows"):
            value = obj.get(key)
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except Exception:
                    pass
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = unwrap_source(value)
                if isinstance(nested, list):
                    return nested

        # Bazı API'ler sembolü anahtar olarak kullanabilir.
        dict_items = []
        for k, v in obj.items():
            if isinstance(v, dict):
                item = dict(v)
                item.setdefault("symbol", k)
                dict_items.append(item)
        if dict_items:
            return dict_items

    return []


def normalize_item(item):
    """Farklı alan isimlerini Android uygulamamızın beklediği ortak yapıya çevirir."""
    if not isinstance(item, dict):
        return None

    symbol = first_value(
        item,
        ["symbol", "code", "sembol", "ticker", "name", "kod"]
    )

    if symbol is None:
        return None

    symbol = str(symbol).strip().upper()
    if not symbol or len(symbol) > 20:
        return None

    price = to_float(first_value(
        item,
        [
            "price", "fiyat", "last", "lastPrice", "close",
            "kapanis", "son", "current", "currentPrice"
        ]
    ))

    previous = to_float(first_value(
        item,
        [
            "previousClose", "oncekiKapanis", "previous",
            "prevClose", "prev", "previous_price", "onceki"
        ]
    ))

    change = to_float(first_value(
        item,
        [
            "changePercent", "degisimYuzde", "change_percentage",
            "percent", "percentage", "changePercentDaily",
            "yuzde", "dailyChangePercent"
        ]
    ))

    # Degisim yüzdesi API'de yoksa fiyat ve önceki kapanıştan hesapla.
    if change is None and price is not None and previous not in (None, 0):
        change = ((price - previous) / previous) * 100.0

    currency = first_value(
        item,
        ["currency", "paraBirimi", "currencyCode", "unit"],
        "TRY"
    )

    # Android tarafıyla uyumlu alanlar + İngilizce alias'lar.
    result = dict(item)
    result.update({
        "sembol": symbol,
        "kod": symbol,
        "symbol": symbol,
        "code": symbol,
        "fiyat": price,
        "price": price,
        "oncekiKapanis": previous,
        "previousClose": previous,
        "degisimYuzde": change,
        "changePercent": change,
        "paraBirimi": str(currency),
        "currency": str(currency),
    })

    return result


def fetch_source():
    print("YALCIN PRO - ASENAX /all ALINIYOR...")

    headers = {
        "User-Agent": "YalcinPro/1.0",
        "Accept": "application/json,text/plain,*/*",
        "Cache-Control": "no-cache",
    }

    try:
        response = requests.get(
            SOURCE_URL,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        try:
            raw = response.json()
        except Exception:
            raw = json.loads(response.text)

        rows = unwrap_source(raw)

        normalized = []
        for row in rows:
            item = normalize_item(row)
            if item and item.get("fiyat") is not None:
                normalized.append(item)

        # Sadece aktif 614 sembolü tut.
        if SYMBOLS:
            wanted = set(SYMBOLS)
            filtered = [
                x for x in normalized
                if x.get("symbol") in wanted
            ]

            # API sembol eşleşmesi farklıysa, yine de gelen gerçek veriyi kaybetme.
            if filtered:
                normalized = filtered

        # Aynı sembolü tek kez tut.
        unique = {}
        for item in normalized:
            unique[item["symbol"]] = item

        data = list(unique.values())

        print(
            f"YALCIN PRO - ASENAX SONUC: "
            f"{len(data)} HISSE | HTTP {response.status_code}"
        )

        return data

    except Exception as e:
        print("YALCIN PRO - ASENAX HATASI:", repr(e))
        return []


def refresh_once(force=False):
    global cache, last_refresh

    # Aynı anda iki telefon isteğinin iki kez kaynak çağırmasını önle.
    if not refresh_lock.acquire(blocking=False):
        return len(cache)

    try:
        if not force and cache:
            age = time.time() - last_refresh["timestamp"]
            if age < REFRESH_SECONDS:
                return len(cache)

        last_refresh["status"] = "veri_alınıyor"
        last_refresh["total"] = len(SYMBOLS) if SYMBOLS else TARGET

        data = fetch_source()

        if data:
            new_cache = {x["symbol"]: x for x in data}
            cache = new_cache

            wanted_count = len(SYMBOLS) if SYMBOLS else TARGET
            missing = max(wanted_count - len(cache), 0)

            last_refresh.update({
                "timestamp": time.time(),
                "updated": len(cache),
                "missing": missing,
                "total": wanted_count,
                "status": "güncel",
            })

            print(
                f"YALCIN PRO - CACHE: {len(cache)} | "
                f"MISSING: {missing}"
            )
        else:
            # Eski cache varsa koru; yoksa hata durumunu açıkça göster.
            wanted_count = len(SYMBOLS) if SYMBOLS else TARGET
            last_refresh.update({
                "missing": max(wanted_count - len(cache), 0),
                "total": wanted_count,
                "status": "veri_alınamadı",
            })

        return len(cache)

    finally:
        refresh_lock.release()


def background_loop():
    print("YALCIN PRO - ARKA PLAN BASLADI")

    while True:
        try:
            # İlk yükleme ve sonrasında 60 saniyede bir.
            refresh_once(force=(not cache))
        except Exception as e:
            print("YALCIN PRO - ARKA PLAN HATASI:", repr(e))

        time.sleep(REFRESH_SECONDS)


@app.route("/")
def home():
    return jsonify({
        "success": True,
        "status": "online",
        "service": "Yalcin Pro Borsa",
        "data": len(cache),
        "symbols": len(SYMBOLS) if SYMBOLS else TARGET,
        "target": TARGET,
        "source": SOURCE_URL,
        "delay": "yaklasik 15 dakika",
    })


@app.route("/health")
def health():
    total = len(SYMBOLS) if SYMBOLS else TARGET
    return jsonify({
        "success": True,
        "status": "online",
        "serverTime": now_text(),
        "symbols": total,
        "target": TARGET,
        "data": len(cache),
        "lastRefresh": last_refresh,
    })


@app.route("/stats")
def stats():
    total = len(SYMBOLS) if SYMBOLS else TARGET
    return jsonify({
        "success": True,
        "status": "online",
        "serverTime": now_text(),
        "target": total,
        "symbols": total,
        "cached": len(cache),
        "missing": max(total - len(cache), 0),
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
    # KRITIK: Arka planın hazır olmasını bekleme.
    # Telefon /all istediğinde veri yoksa doğrudan çek.
    if not cache:
        print("YALCIN PRO - /all CACHE BOS -> ANLIK VERI CEKILIYOR")
        refresh_once(force=True)

    data = list(cache.values())

    return jsonify({
        "success": True,
        "status": "online",
        "serverTime": now_text(),
        "symbols": len(SYMBOLS) if SYMBOLS else TARGET,
        "target": TARGET,
        "data": data,
        "count": len(data),
        "lastRefresh": last_refresh,
    })


@app.route("/stock/<symbol>")
def stock(symbol):
    symbol = symbol.strip().upper()

    item = cache.get(symbol)

    # Tek hisse istenip cache boşsa da kaynak yenile.
    if item is None:
        refresh_once(force=not bool(cache))
        item = cache.get(symbol)

    if item is None:
        return jsonify({
            "success": False,
            "status": "not_found",
            "symbol": symbol,
            "data": None,
        }), 404

    return jsonify({
        "success": True,
        "status": "online",
        "serverTime": now_text(),
        "data": item,
    })


# Gunicorn bunu kullanacak.
# Yerelde: python server.py
if __name__ == "__main__":
    print("=" * 60)
    print("YALCIN PRO SERVER")
    print(f"HEDEF: {len(SYMBOLS) if SYMBOLS else TARGET} HISSE")
    print(f"KAYNAK: {SOURCE_URL}")
    print("GECIKME: YAKLASIK 15 DAKIKA")
    print(f"YENILEME: {REFRESH_SECONDS} SANIYE")
    print("=" * 60)

    # Yerel çalıştırmada arka planı başlat.
    t = threading.Thread(target=background_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
