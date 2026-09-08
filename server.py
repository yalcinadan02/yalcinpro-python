from flask import Flask, jsonify
import threading
import time
import json
import os
import re
import urllib.request
from html.parser import HTMLParser

# =========================================================
# YALCIN PRO - UCRETSIZ / GECIKMELI BIST VERISI
# =========================================================
# Bu surumde:
# - Yalcin Pro kendi yuzde hesabini YAPMAZ.
# - Onceki yenilemeye gore degisim hesaplanmaz.
# - Yerel Armert BIST Data Service (port 8000) /all endpoint'i kullanilir.
# - Servisin change_percent alani dogrudan degisimYuzde olarak kullanilir.
# - 614 aktif hisse korunur; kaynakta bulunanlardan doldurulur.
#
# Veri zinciri:
# Armert BIST Data Service (Yahoo Chart, gecikmeli) -> Yalcin Pro server.py -> Android

app = Flask(__name__)

# =========================================================
# YALCIN PRO - 614 HISSE / YAKLASIK 15 DK GECIKMELI VERI
# =========================================================
# Mimari:
#   BIST Data Service :8000  ->  Yalcin Pro server.py :5000  ->  Android
#
# Mevcut 614 aktif sembol listesini korur.
# Yeni servisin /all endpointinden gelen fiyat ve gunluk degisim yuzdesini
# dogrudan Android'e aktarir. Kendi yuzde hesabini yapmaz.

TARGET = 614
REFRESH_SECONDS = 30
SOURCE_URL = os.environ.get("BIST_DATA_SERVICE_URL", "http://127.0.0.1:8000")
SOURCE_ALL_URL = SOURCE_URL.rstrip("/") + "/all"
ACTIVE_CACHE_FILE = "yalcin_pro_active_symbols.json"

_symbol_list = []
_symbol_lock = threading.Lock()

_snapshot = {}
_snapshot_time = 0.0
_snapshot_lock = threading.Lock()

_background_started = False
_background_lock = threading.Lock()

last_refresh = {
    "updated": 0,
    "missing": 0,
    "total": TARGET,
    "timestamp": 0,
}


def normalize_symbol(value):
    if value is None:
        return ""
    s = str(value).upper().strip()
    s = s.replace("\xa0", "").replace("\u200b", "")
    s = s.replace("\ufeff", "")
    s = s.replace(".IS", "")
    s = re.sub(r"\s+", "", s)
    if not re.fullmatch(r"[A-Z0-9]{2,8}", s):
        return ""
    return s


def fetch_source_all():
    """Yerel BIST Data Service /all endpointinden toplu quote verisini al."""
    req = urllib.request.Request(
        SOURCE_ALL_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "YalcinPro/1.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read().decode("utf-8", errors="ignore")

    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return {}

    quotes = payload.get("quotes")
    if not isinstance(quotes, list):
        quotes = payload.get("data")
    if not isinstance(quotes, list):
        return {}

    result = {}

    for q in quotes:
        if not isinstance(q, dict):
            continue

        symbol = normalize_symbol(
            q.get("symbol") or q.get("sembol") or q.get("ticker")
        )
        if not symbol:
            continue

        price = q.get("price", q.get("fiyat"))
        previous = q.get(
            "previous_close",
            q.get("previousClose", q.get("oncekiKapanis")),
        )
        change_percent = q.get(
            "change_percent",
            q.get("changePercent", q.get("degisimYuzde")),
        )
        currency = q.get("currency", q.get("paraBirimi", "TRY"))

        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None

        try:
            previous = float(previous) if previous is not None else None
        except (TypeError, ValueError):
            previous = None

        try:
            change_percent = (
                float(change_percent)
                if change_percent is not None
                else None
            )
        except (TypeError, ValueError):
            change_percent = None

        if price is None or price <= 0 or change_percent is None:
            continue

        result[symbol] = {
            "sembol": symbol,
            "fiyat": round(price, 2),
            "oncekiKapanis": round(previous, 2) if previous is not None else None,
            "degisimYuzde": round(change_percent, 2),
            "paraBirimi": str(currency or "TRY"),
        }

    return result


def download_snapshot():
    global _snapshot, _snapshot_time

    now = time.time()

    with _snapshot_lock:
        if _snapshot and now - _snapshot_time < 5:
            return dict(_snapshot)

    print("YALCIN PRO - YENI BIST DATA SERVICE /all ALINIYOR...")
    print("YALCIN PRO - KAYNAK:", SOURCE_ALL_URL)

    try:
        result = fetch_source_all()

        print(
            "YALCIN PRO - YENI KAYNAKTAN GELEN GECERLI HISSE:",
            len(result),
        )

        for s in ("ADEL", "THYAO", "AKBNK", "GARAN", "ASELS"):
            if s in result:
                x = result[s]
                print(
                    f"YALCIN PRO - KAYNAK {s}: "
                    f"FIYAT={x['fiyat']} | "
                    f"GUNLUK_DEG=%{x['degisimYuzde']}"
                )

        if len(result) < TARGET:
            print(
                "YALCIN PRO - KAYNAKTA YETERLI HISSE YOK:",
                len(result), "/", TARGET
            )
            return {}

        with _snapshot_lock:
            _snapshot = result
            _snapshot_time = time.time()

        return dict(result)

    except Exception as e:
        print("YALCIN PRO - YENI KAYNAK HATASI:", e)
        return {}


def load_active_symbols():
    global _symbol_list
    try:
        if not os.path.exists(ACTIVE_CACHE_FILE):
            return []
        with open(ACTIVE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        symbols = []
        for x in data:
            s = normalize_symbol(x)
            if s and s not in symbols:
                symbols.append(s)
        with _symbol_lock:
            _symbol_list = symbols[:TARGET]
        print("YALCIN PRO - AKTIF SEMBOL CACHE YUKLENDI:", len(_symbol_list), "HISSE")
        return list(_symbol_list)
    except Exception as e:
        print("YALCIN PRO - AKTIF CACHE HATASI:", e)
        return []


def save_active_symbols(symbols):
    try:
        symbols = list(dict.fromkeys(normalize_symbol(x) for x in symbols if normalize_symbol(x)))
        symbols = symbols[:TARGET]
        tmp = ACTIVE_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(symbols, f, ensure_ascii=False, indent=2)
        os.replace(tmp, ACTIVE_CACHE_FILE)
    except Exception as e:
        print("YALCIN PRO - CACHE YAZMA HATASI:", e)


def sync_symbols(snapshot):
    global _symbol_list

    source = list(snapshot.keys())
    source_set = set(source)

    with _symbol_lock:
        current = list(_symbol_list)

    # Mevcut 614 listesini, kaynakta bulunanlarla koru.
    ordered = [s for s in current if s in source_set]

    # Eksiklerin yerine kaynakta bulunan yeni sembolleri ekle.
    for s in source:
        if s not in ordered:
            ordered.append(s)
        if len(ordered) == TARGET:
            break

    if len(ordered) != TARGET:
        print("YALCIN PRO - 614 SEMBOL OLUSTURULAMADI:", len(ordered), "/", TARGET)
        return []

    with _symbol_lock:
        _symbol_list = ordered

    save_active_symbols(ordered)
    print("YALCIN PRO - AKTIF EVREN:", len(ordered), "/", TARGET)
    return list(ordered)


def get_symbols():
    with _symbol_lock:
        return list(_symbol_list[:TARGET])


def refresh_once():
    global last_refresh

    snapshot = download_snapshot()
    if not snapshot:
        return

    symbols = sync_symbols(snapshot)
    if not symbols:
        return

    # ONEMLI: 614 hissenin tamamini AYNI snapshot'tan dolduruyoruz.
    # Grup/batch yok. Bu nedenle 504/614 gibi bir durum olusmamali.
    missing = [s for s in symbols if s not in snapshot]

    updated = len(symbols) - len(missing)

    last_refresh = {
        "updated": updated,
        "missing": len(missing),
        "total": TARGET,
        "timestamp": time.time(),
    }

    print(
        "YALCIN PRO - YENILEME TAMAMLANDI:",
        updated, "/", TARGET,
        "| EKSIK:", len(missing)
    )

    if missing:
        print("YALCIN PRO - EKSIK:", ", ".join(missing))


def background_worker():
    while True:
        try:
            refresh_once()
        except Exception as e:
            print("YALCIN PRO - ARKA PLAN HATASI:", e)
        time.sleep(REFRESH_SECONDS)


def start_background():
    global _background_started
    with _background_lock:
        if _background_started:
            return
        _background_started = True

    t = threading.Thread(
        target=background_worker,
        daemon=True,
        name="yalcin-refresh"
    )
    t.start()


@app.route("/health")
def health():
    return jsonify({
        "success": True,
        "status": "online",
        "symbols": len(get_symbols()),
        "lastRefresh": last_refresh,
        "serverTime": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/stats")
def stats():
    return jsonify({
        "success": True,
        "target": TARGET,
        "symbols": len(get_symbols()),
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


@app.route("/stocks")
def stocks():
    # Android isteginde de taze snapshot kullan.
    snapshot = download_snapshot()

    if not snapshot:
        return jsonify({
            "success": False,
            "data": [],
            "error": "Canli veri kaynagi okunamadi"
        }), 503

    symbols_now = sync_symbols(snapshot)

    if len(symbols_now) != TARGET:
        return jsonify({
            "success": False,
            "data": [],
            "error": f"614 hisse eslesmedi: {len(symbols_now)}"
        }), 503

    data = [snapshot[s] for s in symbols_now]

    print(
        "YALCIN PRO - ANDROID CEVAP:",
        len(data), "/", TARGET
    )

    for s in ("ADEL", "THYAO", "AKBNK"):
        x = snapshot.get(s)
        if x:
            print(
                f"YALCIN PRO - ANDROID {s}: "
                f"FIYAT={x['fiyat']} | DEG=%{x['degisimYuzde']}"
            )

    return jsonify({
        "success": True,
        "data": data
    })


@app.route("/stock/<symbol>")
def stock(symbol):
    snapshot = download_snapshot()
    s = normalize_symbol(symbol)
    item = snapshot.get(s) if snapshot else None

    if item is None:
        return jsonify({"success": True, "data": []})

    return jsonify({
        "success": True,
        "data": [item]
    })


if __name__ == "__main__":
    load_active_symbols()

    print("=" * 60)
    print("YALCIN PRO SERVER - UCRETSIZ / GECIKMELI BIST")
    print("HEDEF:", TARGET, "HISSE")
    print("KAYNAK: LOCAL BIST DATA SERVICE / Yahoo Chart")
    print("YENILEME:", REFRESH_SECONDS, "SANIYE")
    print("BATCH: YOK - TEK /all SNAPSHOT")
    print("=" * 60)

    # Ilk snapshot'i server acilisinda al.
    snap = download_snapshot()
    if snap:
        sync_symbols(snap)

    start_background()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True
    )
