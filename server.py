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
# - Yahoo yok
# - Apinoktam yok
# - kendi yuzde hesabimiz yok
# - onceki yenilemeye gore yuzde yok
# - 614 hisse tek bir canli kaynak snapshot'ından doldurulur
# - kaynakta yayinlanan "Degisim (%)" dogrudan kullanilir
#
# Veri kaynagi: Is Yatirim web tablosu.
# Bu kaynak Borsa Istanbul'un resmi anlik veri akisinin kendisi degildir;
# dolayisiyla gecikme/veri farki olabilir.

app = Flask(__name__)

TARGET = 614
REFRESH_SECONDS = 30
SNAPSHOT_TTL = 5
ACTIVE_CACHE_FILE = "yalcin_pro_active_symbols.json"

ISYATIRIM_URL = (
    "https://www.isyatirim.com.tr/tr-tr/Analiz/hisse/"
    "Sayfalar/default.aspx"
)

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
    "timestamp": 0
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


def parse_number(value):
    if value is None:
        return None
    s = str(value).replace("\xa0", " ").replace("%", "").strip()
    if not s:
        return None
    # Is Yatirim Turkce sayi bicimi: 1.234,56
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_tr = False
        self.in_cell = False
        self.cell_index = -1
        self.cell_text = []
        self.row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "tr":
            if self.in_tr:
                self.finish_row()
            self.in_tr = True
            self.in_cell = False
            self.cell_index = -1
            self.cell_text = []
            self.row = []
        elif self.in_tr and tag in ("td", "th"):
            self.cell_index += 1
            self.in_cell = True
            self.cell_text = []

    def handle_data(self, data):
        if self.in_tr and self.in_cell:
            self.cell_text.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.in_tr and self.in_cell and tag in ("td", "th"):
            value = re.sub(r"\s+", " ", " ".join(self.cell_text)).strip()
            self.row.append(value)
            self.in_cell = False
            self.cell_text = []
        elif tag == "tr" and self.in_tr:
            self.finish_row()

    def finish_row(self):
        if self.row:
            self.rows.append(self.row)
        self.in_tr = False
        self.in_cell = False
        self.cell_index = -1
        self.cell_text = []
        self.row = []


def download_snapshot():
    global _snapshot, _snapshot_time

    now = time.time()

    with _snapshot_lock:
        if _snapshot and now - _snapshot_time < SNAPSHOT_TTL:
            return dict(_snapshot)

        print("YALCIN PRO - IS YATIRIM SNAPSHOT ALINIYOR...")

        try:
            url = ISYATIRIM_URL + "?yalcin_live=" + str(int(now * 1000))
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )

            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf-8", errors="ignore")

            parser = TableParser()
            parser.feed(html)

            print("YALCIN PRO - IS YATIRIM HTML SATIR:", len(parser.rows))

            header = None
            price_i = None
            change_i = None

            for i, row in enumerate(parser.rows):
                cells = [re.sub(r"\s+", " ", str(x)).strip().lower() for x in row]
                has_hisse = any(x == "hisse" or x.startswith("hisse ") for x in cells)
                if not has_hisse:
                    continue

                p = next((j for j, x in enumerate(cells) if "son fiyat" in x), None)
                c = next(
                    (j for j, x in enumerate(cells)
                     if "değişim (%)" in x or "degisim (%)" in x),
                    None
                )
                if p is not None and c is not None:
                    header, price_i, change_i = i, p, c
                    break

            print(
                "YALCIN PRO - IS YATIRIM HEADER:",
                header, "| FIYAT:", price_i, "| DEG:", change_i
            )

            if header is None:
                return {}

            result = {}

            for row in parser.rows[header + 1:]:
                if max(price_i, change_i) >= len(row):
                    continue

                raw = str(row[0])
                # Ilk gecerli sembol.
                candidates = re.findall(r"\b[A-Z0-9]{2,8}\b", raw.upper())
                symbol = ""
                for candidate in candidates:
                    n = normalize_symbol(candidate)
                    if n:
                        symbol = n
                        break

                if not symbol:
                    continue

                price = parse_number(row[price_i])
                change = parse_number(row[change_i])

                if price is None or price <= 0 or change is None:
                    continue

                # Onceki kapanis sadece bilgi amacli geri hesaplanir.
                # Android'deki degisim YUZDESI bu degerden hesaplanmaz.
                denom = 1.0 + change / 100.0
                previous = price / denom if denom > 0 else price

                result[symbol] = {
                    "sembol": symbol,
                    "fiyat": round(price, 2),
                    "oncekiKapanis": round(previous, 2),
                    "degisimYuzde": round(change, 2),
                    "paraBirimi": "TRY",
                }

            print("YALCIN PRO - IS YATIRIM SNAPSHOT:", len(result), "HISSE")

            for s in ("ADEL", "THYAO", "AKBNK", "GARAN", "YUNSA", "VERUS", "USHOL"):
                if s in result:
                    x = result[s]
                    print(
                        f"YALCIN PRO - KAYNAK: {s} | "
                        f"FIYAT={x['fiyat']} | "
                        f"GUNLUK_DEG=%{x['degisimYuzde']}"
                    )

            if len(result) < TARGET:
                print(
                    "YALCIN PRO - KAYNAKTA YETERLI HISSE YOK:",
                    len(result), "/", TARGET
                )
                return {}

            _snapshot = result
            _snapshot_time = time.time()
            return dict(result)

        except Exception as e:
            print("YALCIN PRO - SNAPSHOT HATASI:", e)
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
    print("KAYNAK: IS YATIRIM")
    print("YENILEME:", REFRESH_SECONDS, "SANIYE")
    print("BATCH: YOK - TEK SNAPSHOT")
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
