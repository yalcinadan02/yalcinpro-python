from flask import Flask, jsonify, request
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import json
import re
import urllib.request

# =========================================================
# APINOKTAM API KEY
# =========================================================
# Gerçek API anahtarını sadece bu satıra yaz.
APINOKTAM_API_KEY = "ak_live_bb8bd2d307ac905f51429e08a8539ac65f440c13b674830f"

from html.parser import HTMLParser
from zoneinfo import ZoneInfo


# =============================================================
# YALCIN PRO - CANLI BIST SERVER
# =============================================================

app = Flask(__name__)

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


# =============================================================
# AYARLAR
# =============================================================

# Yahoo bir istekte kaç hisse işleyecek
BATCH_SIZE = 25

# Cache kaç saniye taze kabul edilecek
CACHE_TTL_SECONDS = 20

# Bir yenileme turundan sonra bekleme
BACKGROUND_REFRESH_SECONDS = 30

# KAP sembol listesini ne kadar sıklıkla yeniden kontrol edeceğiz
SYMBOL_REFRESH_SECONDS = 300

# İlk geçerli evren için güvenli paralel Yahoo işçisi sayısı
SYMBOL_DISCOVERY_WORKERS = 4

# Başarısız gruplar için tekrar deneme
RETRY_COUNT = 1

# Retry arası bekleme
RETRY_WAIT_SECONDS = 3

# Yahoo intraday
INTRADAY_PERIOD = "1d"
INTRADAY_INTERVAL = "5m"

# Önceki kapanış için günlük veri
DAILY_PERIOD = "5d"
DAILY_INTERVAL = "1d"

# Kalıcı fiyat cache
PERSISTENT_CACHE_FILE = "yalcin_pro_cache.json"

# Dinamik sembol cache
SYMBOL_CACHE_FILE = "yalcin_pro_symbols.json"

# Yahoo'dan doğrulanmış aktif BIST hisseleri
ACTIVE_SYMBOL_CACHE_FILE = "yalcin_pro_active_symbols.json"

# KAP BIST şirketleri
KAP_BIST_URL = "https://kap.org.tr/tr/bist-sirketler"

# KAP BIST şirket sayısı için güvenlik sınırı.
# Liste bu sınırı aşarsa HTML parser tekrar kontrol edilmeden kullanılmaz.
MAX_KAP_SYMBOLS = 1200

# YALCIN PRO hedef BIST hisse sayisi
TARGET_BIST_STOCK_COUNT = 614

# 614 hissenin canlı güncelleme durumunu takip et
last_refresh_stats = {
    "updated": 0,
    "missing": 0,
    "total": 0,
    "timestamp": 0
}


# KAP listesinin içine zaman zaman karışabilen denetim kuruluşu
# ve şirket dışı kodlar. Bunlar Yahoo'da BIST hissesi değildir.
INVALID_SYMBOLS = {
    "DRT", "PWC", "KPMG", "TTK", "BDO", "PKF",
    "RSM", "BD", "CNS", "HSY", "KARAR", "REFORM"
}


# =============================================================
# CACHE
# =============================================================

_stock_cache = {}

_cache_lock = threading.Lock()

_background_refresh_started = False

_refresh_lock = threading.Lock()


# =============================================================
# DİNAMİK SEMBOL CACHE
# =============================================================

_symbol_list = []

_symbol_list_lock = threading.Lock()

_last_symbol_refresh = 0.0
_symbol_refresh_lock = threading.Lock()


# =============================================================
# SEMBOL NORMALİZASYONU
# =============================================================

def normalize_symbol(symbol):

    if not symbol:
        return ""

    normalized = (
        str(symbol)
        .strip()
        .upper()
        .replace(".IS", "")
        .replace(" ", "")
    )

    if normalized in INVALID_SYMBOLS:
        return ""

    return normalized


# =============================================================
# KAP SEMBOL NORMALİZASYONU
# =============================================================

def normalize_kap_symbol(symbol):

    if not symbol:
        return ""

    symbol = str(symbol).strip().upper()

    # Bazı KAP linklerinde:
    #
    # A1CAP ACP
    # ALBRK ALK
    #
    # gibi ikinci ifade bulunabiliyor.
    #
    # İlk parçayı sembol adayı olarak alıyoruz.

    parts = symbol.split()

    if not parts:
        return ""

    symbol = parts[0]

    symbol = (
        symbol
        .replace(".IS", "")
        .replace(",", "")
        .replace(";", "")
        .strip()
    )

    # BIST sembolleri için güvenli karakter kümesi
    if not re.fullmatch(
        r"[A-Z0-9]{2,8}",
        symbol
    ):
        return ""

    return symbol


# =============================================================
# KAP HTML PARSER
# =============================================================

class KAPSymbolParser(HTMLParser):
    """
    KAP BIST şirketleri tablosundan SADECE ilk sütundaki
    hisse kodunu alır.

    ÖNEMLİ:
    KAP satırlarında şirket kodunun yanında şirket unvanı, şehir
    ve bağımsız denetim kuruluşu da bulunabilir. Eski parser
    link metinlerini taradığı için DRT, PWC, TTK, PKF gibi denetim
    kuruluşlarını da hisse sembolü sanabiliyordu.

    Bu parser sadece <tr> içindeki ilk <td>/<th> hücresini okur
    ve o hücredeki İLK geçerli kodu sembol olarak kabul eder.
    Böylece ikinci kodlar ve denetim kuruluşları listeye girmez.
    """

    def __init__(self):
        super().__init__()

        self.in_row = False
        self.in_cell = False
        self.cell_index = -1
        self.current_cell_text = []
        self.first_cell_text = ""
        self.symbols = []

    def handle_starttag(self, tag, attrs):

        tag = tag.lower()

        if tag == "tr":

            self.in_row = True
            self.in_cell = False
            self.cell_index = -1
            self.current_cell_text = []
            self.first_cell_text = ""

            return

        if (
            self.in_row
            and tag in ("td", "th")
        ):

            self.cell_index += 1
            self.in_cell = True
            self.current_cell_text = []

    def handle_data(self, data):

        if self.in_row and self.in_cell:

            self.current_cell_text.append(data)

    def handle_endtag(self, tag):

        tag = tag.lower()

        if (
            self.in_row
            and self.in_cell
            and tag in ("td", "th")
        ):

            if self.cell_index == 0:

                self.first_cell_text = (
                    " ".join(
                        self.current_cell_text
                    )
                    .strip()
                )

            self.in_cell = False
            self.current_cell_text = []

            return

        if tag != "tr" or not self.in_row:
            return

        raw_code = self.first_cell_text.strip()

        if raw_code:

            tokens = re.findall(
                r"[A-Z0-9]{2,8}",
                raw_code.upper()
            )

            if tokens:

                # İlk sütundaki ilk kod = ana BIST sembolü.
                candidate = normalize_kap_symbol(
                    tokens[0]
                )

                # Tablo başlığının yanlışlıkla sembol olmasını engelle.
                if (
                    candidate
                    and candidate != "KOD"
                    and candidate not in INVALID_SYMBOLS
                ):

                    self.symbols.append(
                        candidate
                    )

        self.in_row = False
        self.in_cell = False
        self.cell_index = -1
        self.current_cell_text = []
        self.first_cell_text = ""


# =============================================================
# KAP SEMBOLLERİNİ İNDİR
# =============================================================

def _download_kap_symbols():

    print(
        "YALCIN PRO - KAP BIST SEMBOLLERI ALINIYOR..."
    )

    try:

        req = urllib.request.Request(

            KAP_BIST_URL,

            headers={
                "User-Agent":
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36",
                "Accept":
                    "text/html,application/xhtml+xml"
            }
        )

        with urllib.request.urlopen(
            req,
            timeout=20
        ) as response:

            html = response.read().decode(
                "utf-8",
                errors="ignore"
            )

        parser = KAPSymbolParser()

        parser.feed(html)

        symbols = list(
            dict.fromkeys(
                parser.symbols
            )
        )

        print(
            "YALCIN PRO - KAP HAM SEMBOL:",
            len(symbols),
            "HISSE"
        )

        # KAP sayfası başarısız veya HTML
        # yapısı değişmişse mevcut listeyi bozma.

        if len(symbols) < 100:

            print(
                "YALCIN PRO - KAP SEMBOL LISTESI YETERSIZ:",
                len(symbols)
            )

            return []

        # Aşırı büyük liste genellikle KAP HTML yapısının yanlış
        # parse edildiğini gösterir. Eski sürüm 1045 sembol üretiyordu.
        if len(symbols) > MAX_KAP_SYMBOLS:

            print(
                "YALCIN PRO - KAP SEMBOL LISTESI SUPHELI:",
                len(symbols),
                "HISSE"
            )

            return []

        print(
            "YALCIN PRO - KAP SEMBOL LISTESI HAZIR:",
            len(symbols),
            "HISSE"
        )

        return symbols

    except Exception as e:

        print(
            "YALCIN PRO - KAP SEMBOL HATASI:",
            e
        )

        return []


# =============================================================
# SEMBOL CACHE YÜKLE
# =============================================================

def _load_symbol_cache():

    global _symbol_list

    try:

        if not os.path.exists(
            SYMBOL_CACHE_FILE
        ):

            print(
                "YALCIN PRO - SEMBOL CACHE DOSYASI YOK"
            )

            return

        with open(
            SYMBOL_CACHE_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            saved = json.load(f)

        if not isinstance(
            saved,
            list
        ):

            return

        cleaned = []

        for symbol in saved:

            normalized = normalize_symbol(
                symbol
            )

            if normalized and normalized not in INVALID_SYMBOLS:

                cleaned.append(
                    normalized
                )

        cleaned = list(dict.fromkeys(cleaned))

        # Eski sürümde KAP'ın denetim kuruluşları da sembol olarak
        # cache'e girmiş olabilir (ör. DRT, PWC). Böyle bir cache'i
        # kullanma; yeni KAP listesini yeniden oluştur.
        if len(cleaned) > MAX_KAP_SYMBOLS or any(
            code in INVALID_SYMBOLS for code in cleaned
        ):
            print(
                "YALCIN PRO - ESKI/GEÇERSIZ SEMBOL CACHE ATILDI:",
                len(cleaned),
                "HISSE"
            )

            # Eski hatalı cache'in tekrar kullanılmasını engelle.
            try:
                os.remove(SYMBOL_CACHE_FILE)
            except Exception:
                pass

            return

        with _symbol_list_lock:
            _symbol_list = cleaned

        print(
            "YALCIN PRO - SEMBOL CACHE YUKLENDI:",
            len(cleaned),
            "HISSE"
        )

    except Exception as e:

        print(
            "YALCIN PRO - SEMBOL CACHE OKUMA HATASI:",
            e
        )


# =============================================================
# SEMBOL CACHE KAYDET
# =============================================================

def _save_symbol_cache(
    symbols
):

    try:

        cleaned = []

        for symbol in symbols:

            normalized = normalize_symbol(
                symbol
            )

            if normalized and normalized not in INVALID_SYMBOLS:

                cleaned.append(
                    normalized
                )

        cleaned = list(
            dict.fromkeys(
                cleaned
            )
        )

        temp_file = (
            SYMBOL_CACHE_FILE
            + ".tmp"
        )

        with open(
            temp_file,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                cleaned,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(
            temp_file,
            SYMBOL_CACHE_FILE
        )

        print(
            "YALCIN PRO - SEMBOL CACHE KAYDEDILDI:",
            len(cleaned)
        )

    except Exception as e:

        print(
            "YALCIN PRO - SEMBOL CACHE YAZMA HATASI:",
            e
        )


# =============================================================
# BIST SEMBOLLERİNİ GETİR
# =============================================================

def _load_active_symbol_cache():
    """Yahoo'dan daha önce doğrulanmış aktif BIST evrenini yükler."""
    global _symbol_list

    try:
        if not os.path.exists(ACTIVE_SYMBOL_CACHE_FILE):
            return []

        with open(ACTIVE_SYMBOL_CACHE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        if not isinstance(saved, list):
            return []

        cleaned = list(dict.fromkeys(
            normalize_symbol(s)
            for s in saved
            if normalize_symbol(s)
        ))

        if not cleaned:
            return []

        # Eski/eksik cache (or. 19 hisse) aktif evren olarak kullanilmaz.
        if len(cleaned) < TARGET_BIST_STOCK_COUNT:
            print(
                "YALCIN PRO - AKTIF CACHE EKSIK:",
                len(cleaned), "/", TARGET_BIST_STOCK_COUNT,
                "- YENIDEN KESFEDILECEK"
            )
            return []

        cleaned = cleaned[:TARGET_BIST_STOCK_COUNT]

        with _symbol_list_lock:
            _symbol_list = cleaned

        print(
            "YALCIN PRO - AKTIF SEMBOL CACHE YUKLENDI:",
            len(cleaned),
            "HISSE"
        )

        return cleaned

    except Exception as e:
        print(
            "YALCIN PRO - AKTIF SEMBOL CACHE HATASI:",
            e
        )
        return []


def _save_active_symbol_cache(symbols):
    """Doğrulanmış aktif sembolleri atomik olarak kaydeder."""
    try:
        cleaned = list(dict.fromkeys(
            normalize_symbol(s)
            for s in symbols
            if normalize_symbol(s)
        ))

        if len(cleaned) > TARGET_BIST_STOCK_COUNT:
            cleaned = cleaned[:TARGET_BIST_STOCK_COUNT]

        temp_file = ACTIVE_SYMBOL_CACHE_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(
                cleaned,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temp_file, ACTIVE_SYMBOL_CACHE_FILE)

        print(
            "YALCIN PRO - AKTIF SEMBOL CACHE KAYDEDILDI:",
            len(cleaned),
            "HISSE"
        )

    except Exception as e:
        print(
            "YALCIN PRO - AKTIF SEMBOL CACHE YAZMA HATASI:",
            e
        )


def _discover_active_symbols_from_kap(kap_symbols):
    """
    KAP listesindeki sembolleri Yahoo'dan doğrular.
    Böylece KAP'taki yatırım/borçlanma/işlem görmeyen benzeri kayıtlar
    aktif hisse evrenine girmez.

    Başlangıçtaki doğrulanmış evrenin yaklaşık 741 olması beklenir;
    sayı sabitlenmez. Yeni şirketler eklendiğinde sonraki keşifte otomatik
    olarak dahil edilir.
    """
    candidates = list(dict.fromkeys(
        normalize_symbol(s)
        for s in kap_symbols
        if normalize_symbol(s)
    ))

    if not candidates:
        return []

    batches = [
        candidates[i:i + BATCH_SIZE]
        for i in range(0, len(candidates), BATCH_SIZE)
    ]

    active = set()

    print(
        "YALCIN PRO - SEMBOL DOGRULAMA:",
        len(candidates),
        "ADAY |",
        len(batches),
        "GRUP |",
        SYMBOL_DISCOVERY_WORKERS,
        "ISCI"
    )

    def check_batch(batch):
        try:
            results, missing = get_stocks_batch(batch)
            return [
                normalize_symbol(item.get("sembol", ""))
                for item in results
                if normalize_symbol(item.get("sembol", ""))
            ]
        except Exception as e:
            print(
                "YALCIN PRO - SEMBOL DOGRULAMA GRUP HATASI:",
                e
            )
            return []

    with ThreadPoolExecutor(
        max_workers=SYMBOL_DISCOVERY_WORKERS,
        thread_name_prefix="yalcin-symbol"
    ) as executor:

        futures = {
            executor.submit(check_batch, batch): index
            for index, batch in enumerate(batches, start=1)
        }

        completed = 0

        for future in as_completed(futures):
            completed += 1
            index = futures[future]

            try:
                valid = future.result()
                active.update(valid)
                print(
                    "YALCIN PRO - SEMBOL DOGRULAMA:",
                    completed,
                    "/",
                    len(batches),
                    "| GRUP:",
                    index,
                    "| AKTIF:",
                    len(active)
                )
            except Exception as e:
                print(
                    "YALCIN PRO - SEMBOL FUTURE HATASI:",
                    index,
                    e
                )

    # KAP sırasını koru; set sırası kullanma.
    verified_ordered = [
        symbol
        for symbol in candidates
        if symbol in active
    ]

    # Yahoo bazı geçerli BIST sembollerini geçici olarak döndürmeyebilir.
    # Ana evrenin 614 hissede kalması için doğrulanamayan KAP
    # sembollerini tamamlayıcı olarak ekliyoruz. Bu hisselerin fiyatı
    # o anda yoksa /stocks içinde 0.0 ile korunur; arka plan yenilemesi
    # sonraki turlarda gerçek fiyatı tekrar dener.
    ordered = verified_ordered[:TARGET_BIST_STOCK_COUNT]

    if len(ordered) < TARGET_BIST_STOCK_COUNT:
        verified_set = set(ordered)
        fallback_symbols = [
            symbol
            for symbol in candidates
            if symbol not in verified_set
        ]

        needed = TARGET_BIST_STOCK_COUNT - len(ordered)
        ordered.extend(fallback_symbols[:needed])

        print(
            "YALCIN PRO - KAP TAMAMLAMA:",
            min(len(fallback_symbols), needed),
            "EK SEMBOL | DOGRULANMIS:",
            len(verified_ordered),
            "| HEDEF:",
            TARGET_BIST_STOCK_COUNT
        )

    if len(ordered) > TARGET_BIST_STOCK_COUNT:
        ordered = ordered[:TARGET_BIST_STOCK_COUNT]

    print(
        "YALCIN PRO - AKTIF BIST EVRENI:",
        len(ordered),
        "/", TARGET_BIST_STOCK_COUNT,
        "HISSE | YAHOO DOGRULANMIS:",
        len(verified_ordered)
    )

    return ordered

def _refresh_symbol_universe(force=False):
    """KAP listesini periyodik olarak yeniden alır ve Yahoo ile doğrular."""
    global _last_symbol_refresh, _symbol_list

    now = time.time()

    with _symbol_refresh_lock:
        if (
            not force
            and _last_symbol_refresh > 0
            and now - _last_symbol_refresh < SYMBOL_REFRESH_SECONDS
        ):
            with _symbol_list_lock:
                return list(_symbol_list)

        # Başka bir istek keşif yapıyorsa ikinci kez yapma.
        _last_symbol_refresh = now

    kap_symbols = _download_kap_symbols()

    if not kap_symbols:
        with _symbol_list_lock:
            return list(_symbol_list)

    active = _discover_active_symbols_from_kap(kap_symbols)

    if not active:
        with _symbol_list_lock:
            return list(_symbol_list)

    with _symbol_list_lock:
        old = list(_symbol_list)
        _symbol_list = active

    _save_active_symbol_cache(active)
    _save_symbol_cache(kap_symbols)

    added = [s for s in active if s not in old]
    removed = [s for s in old if s not in active]

    print(
        "YALCIN PRO - SEMBOL EVRENI GUNCELLENDI:",
        len(active),
        "HISSE | YENI:",
        len(added),
        "CIKAN:",
        len(removed)
    )

    return active


def get_bist_symbols():
    """
    Server'ın Android'e vereceği ana BIST evrenini döndürür.
    Evren 614 ile sınırlandırılır; fiyatı henüz cache'e gelmemiş
    semboller de listeden çıkarılmaz.
    """
    with _symbol_list_lock:
        current = list(_symbol_list)

    if not current:
        current = _load_active_symbol_cache()

    if len(current) > TARGET_BIST_STOCK_COUNT:
        current = current[:TARGET_BIST_STOCK_COUNT]

    return current


# =============================================================
# FİYAT CACHE DOSYASINI YÜKLE
# =============================================================

def _load_persistent_cache():

    global _stock_cache

    try:

        if not os.path.exists(
            PERSISTENT_CACHE_FILE
        ):

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

        loaded = 0

        with _cache_lock:

            for symbol, item in saved.items():

                try:

                    if (
                        isinstance(
                            item,
                            list
                        )
                        and
                        len(item) == 2
                        and
                        isinstance(
                            item[1],
                            dict
                        )
                    ):

                        normalized = (
                            normalize_symbol(
                                symbol
                            )
                        )

                        if not normalized:

                            continue

                        timestamp = float(
                            item[0]
                        )

                        result = item[1]

                        _stock_cache[
                            normalized
                        ] = (
                            timestamp,
                            result
                        )

                        loaded += 1

                except Exception:

                    continue

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
# FİYAT CACHE DOSYASINI KAYDET
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
                )
                in _stock_cache.items()

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

def _get_cache(
    symbol
):

    symbol = normalize_symbol(
        symbol
    )

    if not symbol:

        return None, False

    now = time.time()

    with _cache_lock:

        item = _stock_cache.get(
            symbol
        )

        if not item:

            return None, False

        timestamp, result = item

    age = (
        now
        - timestamp
    )

    fresh = (
        age
        < CACHE_TTL_SECONDS
    )

    return result, fresh


# =============================================================
# CACHE YAZ
# =============================================================

def _save_stock_memory(
    symbol,
    result
):

    symbol = normalize_symbol(
        symbol
    )

    if not symbol or not result:

        return

    with _cache_lock:

        _stock_cache[
            symbol
        ] = (
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

        # -----------------------------------------------------
        # MULTI INDEX
        # -----------------------------------------------------

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

            # -------------------------------------------------
            # Ticker -> Close
            # -------------------------------------------------

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
                        ticker_data[
                            "Close"
                        ]
                        .dropna()
                    )

            # -------------------------------------------------
            # Close -> Ticker
            # -------------------------------------------------

            if (
                "Close"
                in level0
                and
                ticker_name
                in level1
            ):

                return (
                    data[
                        "Close"
                    ][
                        ticker_name
                    ]
                    .dropna()
                )

        # -----------------------------------------------------
        # NORMAL DATA
        # -----------------------------------------------------

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

    symbol = normalize_symbol(
        symbol
    )

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

    # ---------------------------------------------------------
    # SON FİYAT
    # ---------------------------------------------------------

    try:

        price = float(
            closes.iloc[-1]
        )

    except Exception:

        return None

    if price <= 0:

        return None

    # ---------------------------------------------------------
    # ÖNCEKİ KAPANIŞ
    # ---------------------------------------------------------

    previous = None

    # ---------------------------------------------------------
    # 1 - Günlük veri
    # ---------------------------------------------------------

    if previous_close is not None:

        try:

            value = float(
                previous_close
            )

            if value > 0:

                previous = value

        except Exception:

            previous = None

    # ---------------------------------------------------------
    # 2 - Intraday içinden önceki gün
    # ---------------------------------------------------------

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

    # ---------------------------------------------------------
    # 3 - Son çare
    # ---------------------------------------------------------

    if previous is None:

        previous = price

    # ---------------------------------------------------------
    # DEĞİŞİM %
    # ---------------------------------------------------------

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

    # ---------------------------------------------------------
    # Çok küçük değerleri temizle
    # ---------------------------------------------------------

    if abs(change) < 0.000001:

        change = 0.0

    # ---------------------------------------------------------
    # SONUÇ
    # ---------------------------------------------------------

    return {

        "sembol":
            symbol,

        "fiyat":
            round(
                price,
                2
            ),

        "oncekiKapanis":
            round(
                previous,
                2
            ),

        "degisimYuzde":
            round(
                change,
                2
            ),

        "paraBirimi":
            "TRY"

    }


# =============================================================
# IS YATIRIM - CANLI BIST TABLOSU
# =============================================================
# Günlük değişim yüzdesi burada HESAPLANMAZ.
# İş Yatırım tablosunda yayınlanan "Değişim (%)" doğrudan alınır.

ISYATIRIM_URL = "https://www.isyatirim.com.tr/tr-tr/Analiz/hisse/Sayfalar/default.aspx"
ISYATIRIM_SNAPSHOT_TTL = 20

_isyatirim_snapshot = {}
_isyatirim_snapshot_time = 0.0
_isyatirim_snapshot_lock = threading.Lock()


def _parse_tr_number(value):
    if value is None:
        return None
    text = str(value)
    text = (
        text.replace("\\xa0", " ")
            .replace("\\u200b", "")
            .replace("\\ufeff", "")
            .replace("%", "")
            .strip()
    )
    if not text:
        return None
    # Türkçe sayı biçimi: 1.234,56 -> 1234.56
    text = text.replace(".", "").replace(",", ".")
    try:
        return float(text)
    except Exception:
        return None



class IsYatirimTableParser(HTMLParser):
    """
    İş Yatırım sayfasındaki hisse tablosunu nested table yapısından
    bağımsız olarak okur.

    Eski parser sadece table_depth == 1 kabul ettiği için sayfanın
    güncel HTML yapısında tablo satırlarını buluyor gibi görünse de
    sembol eşleştirmesi 0/25 kalabiliyordu.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.in_tr = False
        self.cell_depth = 0
        self.current_row = []
        self.current_cell = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()

        if tag == "table":
            self.table_depth += 1
            return

        if tag == "tr":
            if self.in_tr:
                self._finish_row()
            self.in_tr = True
            self.current_row = []
            self.current_cell = []
            self.cell_depth = 0
            return

        if self.in_tr and tag in ("td", "th"):
            self.cell_depth += 1
            if self.cell_depth == 1:
                self.current_cell = []

    def handle_data(self, data):
        if self.in_tr and self.cell_depth > 0:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag in ("td", "th") and self.in_tr and self.cell_depth > 0:
            self.cell_depth -= 1
            if self.cell_depth == 0:
                value = " ".join(self.current_cell).strip()
                self.current_row.append(value)
                self.current_cell = []
            return

        if tag == "tr" and self.in_tr:
            self._finish_row()
            return

        if tag == "table" and self.table_depth > 0:
            self.table_depth -= 1

    def _finish_row(self):
        row = [
            re.sub(r"\s+", " ", str(x)).strip()
            for x in self.current_row
        ]
        row = [x for x in row if x != ""]
        if row:
            self.rows.append(row)

        self.in_tr = False
        self.current_row = []
        self.current_cell = []
        self.cell_depth = 0


def _clean_symbol_from_table(value):
    if not value:
        return ""

    text = str(value).upper()
    text = (
        text.replace("\xa0", " ")
            .replace("\u200b", "")
            .replace("\ufeff", "")
            .strip()
    )

    # İlk hücre bazen "ADEL - ADEL KALEMCİLİK..." biçiminde gelebilir.
    # Sembolü sadece ilk geçerli BIST kodundan çıkar.
    match = re.search(r"\b[A-Z0-9]{2,8}\b", text)
    if not match:
        return ""

    return normalize_symbol(match.group(0))


def _download_isyatirim_snapshot():
    """
    İş Yatırım'ın yayınladığı Son Fiyat + Değişim (%) değerlerini
    doğrudan alır.

    ÖNEMLİ:
    - Günlük değişim burada yeniden hesaplanmaz.
    - Yahoo kullanılmaz.
    - Önceki yenilemeye göre yüzde hesaplanmaz.
    - Kaynağın verdiği DEĞİŞİM (%) doğrudan degisimYuzde olur.
    """
    global _isyatirim_snapshot, _isyatirim_snapshot_time

    now = time.time()

    with _isyatirim_snapshot_lock:
        if (
            _isyatirim_snapshot
            and now - _isyatirim_snapshot_time < ISYATIRIM_SNAPSHOT_TTL
        ):
            return dict(_isyatirim_snapshot)

        try:
            print("YALCIN PRO - IS YATIRIM SNAPSHOT ALINIYOR...")

            req = urllib.request.Request(
                ISYATIRIM_URL,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                    "Accept-Language":
                        "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )

            with urllib.request.urlopen(req, timeout=20) as response:
                html = response.read().decode(
                    "utf-8",
                    errors="ignore"
                )

            parser = IsYatirimTableParser()
            parser.feed(html)

            rows = parser.rows

            print(
                "YALCIN PRO - IS YATIRIM HTML SATIR:",
                len(rows)
            )

            # -----------------------------------------------------
            # TABLO BAŞLIĞINI BUL
            # -----------------------------------------------------
            header_index = None
            price_index = None
            change_index = None

            for row_index, row in enumerate(rows[:500]):
                normalized = [
                    re.sub(r"\s+", " ", str(cell))
                    .strip()
                    .lower()
                    for cell in row
                ]

                for idx, cell in enumerate(normalized):
                    if (
                        cell == "hisse"
                        or cell.startswith("hisse ")
                    ):
                        header_index = row_index
                        break

                if header_index is None:
                    continue

                for idx, cell in enumerate(normalized):
                    if "son fiyat" in cell:
                        price_index = idx

                    if (
                        "değişim (%)" in cell
                        or "degisim (%)" in cell
                    ):
                        change_index = idx

                if (
                    price_index is not None
                    and change_index is not None
                ):
                    break

            print(
                "YALCIN PRO - IS YATIRIM HEADER:",
                header_index,
                "| FIYAT:",
                price_index,
                "| DEG:",
                change_index
            )

            if (
                header_index is None
                or price_index is None
                or change_index is None
            ):
                print(
                    "YALCIN PRO - IS YATIRIM TABLO BASLIGI BULUNAMADI"
                )

                # Yanlış/eski veri kullanma.
                return {}

            snapshot = {}

            # -----------------------------------------------------
            # SATIRLARI OKU
            # -----------------------------------------------------
            for row in rows[header_index + 1:]:
                if not row:
                    continue

                required_index = max(
                    price_index,
                    change_index
                )

                if required_index >= len(row):
                    continue

                symbol = _clean_symbol_from_table(
                    row[0]
                )

                if not symbol:
                    continue

                price = _parse_tr_number(
                    row[price_index]
                )

                change = _parse_tr_number(
                    row[change_index]
                )

                if (
                    price is None
                    or price <= 0
                    or change is None
                ):
                    continue

                # Kaynağın verdiği günlük değişim.
                # Burada hiçbir yüzde hesabı yapılmıyor.
                previous_close = price

                denominator = (
                    1.0 + change / 100.0
                )

                if abs(denominator) > 1e-12:
                    previous_close = (
                        price / denominator
                    )

                snapshot[symbol] = {
                    "sembol": symbol,
                    "fiyat": round(price, 2),
                    "oncekiKapanis": round(
                        previous_close,
                        2
                    ),
                    "degisimYuzde": round(
                        change,
                        2
                    ),
                    "paraBirimi": "TRY",
                }

            print(
                "YALCIN PRO - IS YATIRIM SNAPSHOT:",
                len(snapshot),
                "HISSE"
            )

            # -----------------------------------------------------
            # KRİTİK KONTROL
            # -----------------------------------------------------
            for debug_symbol in (
                "ADEL",
                "THYAO",
                "AKBNK",
                "GARAN",
                "YUNSA",
                "VERUS",
                "USHOL",
            ):
                item = snapshot.get(
                    debug_symbol
                )

                if item:
                    print(
                        "YALCIN PRO - KAYNAK:",
                        debug_symbol,
                        "| FIYAT=",
                        item["fiyat"],
                        "| GUNLUK_DEG=%",
                        item["degisimYuzde"]
                    )

            # 500'den az veri geldiyse snapshot'ı başarılı
            # kabul etmiyoruz. Böylece eski yanlış cache'in
            # üzerine bozuk veri yazılmaz.
            if len(snapshot) < 500:
                print(
                    "YALCIN PRO - IS YATIRIM SNAPSHOT YETERSIZ:",
                    len(snapshot),
                    "HISSE"
                )
                return {}

            _isyatirim_snapshot = snapshot
            _isyatirim_snapshot_time = time.time()

            return dict(snapshot)

        except Exception as e:
            print(
                "YALCIN PRO - IS YATIRIM SNAPSHOT HATASI:",
                e
            )
            return {}


def _sync_active_symbols_from_snapshot(snapshot):
    """
    İş Yatırım snapshot'ında gerçekten bulunan sembollerden 614'lük
    aktif evreni oluşturur. Böylece KAP/Yahoo cache'inde kalmış ancak
    kaynak tabloda bulunmayan semboller 614'lük canlı turu bloke etmez.

    Mevcut sırayı mümkün olduğunca korur; eksilen sembollerin yerine
    snapshot'ta bulunan yeni semboller eklenir.
    """
    global _symbol_list

    if not snapshot:
        return []

    source_symbols = list(dict.fromkeys(
        normalize_symbol(s)
        for s in snapshot.keys()
        if normalize_symbol(s)
    ))

    if len(source_symbols) < TARGET_BIST_STOCK_COUNT:
        print(
            "YALCIN PRO - KAYNAKTA 614'TEN AZ HISSE VAR:",
            len(source_symbols),
            "/", TARGET_BIST_STOCK_COUNT
        )
        return []

    source_set = set(source_symbols)

    with _symbol_list_lock:
        current = list(_symbol_list)

    # Önce mevcut 614 evrenden kaynakta bulunanları koru.
    ordered = [s for s in current if s in source_set]

    # Eksilenlerin yerini kaynakta bulunan yeni sembollerle doldur.
    for symbol in source_symbols:
        if symbol not in ordered:
            ordered.append(symbol)
        if len(ordered) >= TARGET_BIST_STOCK_COUNT:
            break

    ordered = ordered[:TARGET_BIST_STOCK_COUNT]

    with _symbol_list_lock:
        _symbol_list = ordered

    missing_from_source = [s for s in current if s not in source_set]
    added_from_source = [s for s in ordered if s not in current]

    print(
        "YALCIN PRO - CANLI EVREN SENKRONIZE:",
        len(ordered),
        "/", TARGET_BIST_STOCK_COUNT,
        "| KAYNAK:", len(source_symbols),
        "| CIKAN:", len(missing_from_source),
        "| EKLENEN:", len(added_from_source)
    )

    if missing_from_source:
        print(
            "YALCIN PRO - KAYNAKTA OLMAYAN ESKI SEMBOLLER:",
            ", ".join(missing_from_source[:50])
        )

    _save_active_symbol_cache(ordered)
    return ordered


def get_stocks_batch(symbols):
    """614'lük evrenden gelen grup için İş Yatırım değerlerini döndürür."""
    symbols = list(dict.fromkeys(
        normalize_symbol(s) for s in symbols if normalize_symbol(s)
    ))
    if not symbols:
        return [], []

    snapshot = _download_isyatirim_snapshot()
    results = []
    missing = []

    for symbol in symbols:
        result = snapshot.get(symbol)
        if result is None:
            missing.append(symbol)
        else:
            results.append(dict(result))

    print(
        "YALCIN PRO - IS YATIRIM GRUP SONUCU:",
        len(results), "/", len(symbols),
        "| EKSIK:", len(missing)
    )
    return results, missing


def get_stock_from_yahoo(symbol):
    """Uyumluluk için tek hisse çağrısı; kaynak yine İş Yatırım'dır."""
    symbol = normalize_symbol(symbol)
    if not symbol:
        return None
    snapshot = _download_isyatirim_snapshot()
    return snapshot.get(symbol)


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

        global _background_refresh_started, last_refresh_stats
        nonlocal refresh_symbols

        print(
            "YALCIN PRO - ARKA PLAN BASLADI:",
            len(refresh_symbols),
            "HISSE"
        )

        while True:

            try:

                # -------------------------------------------------
                # GÜNCEL BIST EVRENİNİ KONTROL ET
                # Yeni eklenen hisseler otomatik dahil edilir.
                # -------------------------------------------------

                latest_symbols = _refresh_symbol_universe()

                if latest_symbols:
                    refresh_symbols = list(latest_symbols)

                # -------------------------------------------------
                # CANLI KAYNAKTA GERCEKTEN BULUNAN SEMBOLLERDEN
                # 614'LUK EVRENİ OLUŞTUR.
                # -------------------------------------------------
                live_snapshot = _download_isyatirim_snapshot()
                synced_symbols = _sync_active_symbols_from_snapshot(live_snapshot)

                if synced_symbols:
                    refresh_symbols = list(synced_symbols)
                else:
                    print(
                        "YALCIN PRO - CANLI EVREN SENKRONIZASYONU BASARISIZ; MEVCUT EVREN KORUNUYOR"
                    )

                # -------------------------------------------------
                # GRUPLARI OLUŞTUR
                # -------------------------------------------------

                batches = [

                    refresh_symbols[
                        i:i + BATCH_SIZE
                    ]

                    for i in range(
                        0,
                        len(refresh_symbols),
                        BATCH_SIZE
                    )

                ]

                total_updated = 0

                total_missing = 0

                print(
                    "YALCIN PRO - YENILEME TURU:",
                    len(batches),
                    "GRUP"
                )

                # -------------------------------------------------
                # -------------------------------------------------
                # TÜM GRUPLARI KONTROLLÜ PARALEL YENİLE
                # -------------------------------------------------
                # 25 grup artık 4 işçiyle aynı anda çalışır.
                # Böylece listenin sonundaki VERUS gibi hisseler
                # ilk grubun bitmesini beklemez.
                # -------------------------------------------------

                def refresh_one_batch(index, batch):
                    print(
                        "YALCIN PRO - GRUP:",
                        index,
                        "/",
                        len(batches),
                        "|",
                        len(batch),
                        "HISSE"
                    )

                    for retry in range(RETRY_COUNT + 1):
                        try:
                            results, missing = get_stocks_batch(batch)

                            for result in results:
                                symbol = normalize_symbol(
                                    result.get("sembol", "")
                                )

                                if symbol:
                                    _save_stock_memory(
                                        symbol,
                                        result
                                    )

                            print(
                                "YALCIN PRO - GRUP TAMAM:",
                                index,
                                "| GUNCEL:",
                                len(results),
                                "| EKSIK:",
                                len(missing),
                                "| DENEME:",
                                retry + 1
                            )

                            return len(results), len(missing)

                        except Exception as e:
                            print(
                                "YALCIN PRO - GRUP HATASI:",
                                index,
                                "| DENEME:",
                                retry + 1,
                                e
                            )

                            if retry < RETRY_COUNT:
                                time.sleep(RETRY_WAIT_SECONDS)

                    print(
                        "YALCIN PRO - GRUP BASARISIZ:",
                        index,
                        "| HISSE:",
                        len(batch)
                    )

                    return 0, len(batch)

                with ThreadPoolExecutor(
                    max_workers=4,
                    thread_name_prefix="yalcin-price"
                ) as executor:

                    futures = {
                        executor.submit(
                            refresh_one_batch,
                            index,
                            batch
                        ): index
                        for index, batch in enumerate(
                            batches,
                            start=1
                        )
                    }

                    for future in as_completed(futures):
                        index = futures[future]

                        try:
                            updated, missing = future.result()
                            total_updated += updated
                            total_missing += missing

                        except Exception as e:
                            print(
                                "YALCIN PRO - GRUP FUTURE HATASI:",
                                index,
                                e
                            )
                            total_missing += len(
                                batches[index - 1]
                            )

                # CACHE DOSYASINI TEK SEFERDE KAYDET
                # -------------------------------------------------

                _save_persistent_cache()

                # -------------------------------------------------
                # CACHE DURUMU
                # -------------------------------------------------

                with _cache_lock:

                    cache_count = len(
                        _stock_cache
                    )

                last_refresh_stats = {
                    "updated": total_updated,
                    "missing": total_missing,
                    "total": len(refresh_symbols),
                    "timestamp": time.time()
                }

                print(
                    "YALCIN PRO - YENILEME TAMAMLANDI:",
                    total_updated,
                    "/",
                    len(refresh_symbols),
                    "| CACHE:",
                    cache_count,
                    "| EKSIK:",
                    total_missing
                )

                print(
                    "YALCIN PRO - 614 HISSE TURU BİTTİ | "
                    "GUNCELLENEN:",
                    total_updated,
                    "/",
                    len(refresh_symbols)
                )

                # -------------------------------------------------
                # SONRAKİ TUR
                # -------------------------------------------------

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
# CACHE / SEMBOL CACHE YÜKLE
# =============================================================

_load_symbol_cache()
_load_active_symbol_cache()
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

    with _symbol_list_lock:

        symbol_count = len(
            _symbol_list
        )

    return jsonify({

        "success":
            True,

        "status":
            "online",

        "cache":
            cache_count,

        "symbols":
            symbol_count,

        "serverTime":
            datetime_now()

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
# DİNAMİK BIST SEMBOLLERİ
# =============================================================

@app.route("/stats")
def stats():
    """614 hissenin canlı veri güncelleme durumunu gösterir."""
    symbols = get_bist_symbols()
    with _cache_lock:
        cache_count = len(_stock_cache)
    with _symbol_list_lock:
        symbol_count = len(_symbol_list)
    return jsonify({
        "success": True,
        "target": TARGET_BIST_STOCK_COUNT,
        "symbols": len(symbols),
        "cache": cache_count,
        "lastRefresh": last_refresh_stats
    })


@app.route("/symbols")
def symbols():

    symbol_list = (
        get_bist_symbols()
    )

    if not symbol_list:

        return jsonify({

            "success":
                False,

            "count":
                0,

            "symbols":
                [],

            "error":
                "BIST sembol listesi alınamadı"

        }), 503

    return jsonify({

        "success":
            True,

        "count":
            len(symbol_list),

        "symbols":
            symbol_list

    })


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

            "success":
                False,

            "data":
                []

        }), 400

    result, fresh = _get_cache(
        symbol
    )

    # ---------------------------------------------------------
    # CACHE TAZE
    # ---------------------------------------------------------

    if (
        result is not None
        and
        fresh
    ):

        return jsonify({

            "success":
                True,

            "data": [
                result
            ]

        })

    # ---------------------------------------------------------
    # CACHE ESKİYSE ARKA PLANDA GÜNCELLE
    # ---------------------------------------------------------

    def update_one():

        try:

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

        except Exception as e:

            print(
                "YALCIN PRO - TEK HISSE GUNCELLEME HATASI:",
                symbol,
                e
            )

    thread = threading.Thread(

        target=update_one,

        daemon=True

    )

    thread.start()

    # ---------------------------------------------------------
    # ESKİ VERİ VARSA HEMEN GÖNDER
    # ---------------------------------------------------------

    if result is not None:

        return jsonify({

            "success":
                True,

            "data": [
                result
            ]

        })

    # ---------------------------------------------------------
    # VERİ YOKSA
    # ---------------------------------------------------------

    return jsonify({

        "success":
            True,

        "data":
            []

    })


# =============================================================
# TÜM HİSSELER
# =============================================================

@app.route("/stocks")
def stocks():

    # SERVER OTORITESI:
    # Android eski sürümde 19 sembol gönderse bile server
    # kendi 614 hisselik aktif BIST evrenini kullanır.
    symbols = get_bist_symbols()

    if not symbols:

        return jsonify({

            "success":
                False,

            "error":
                "Hisse sembol listesi bulunamadı",

            "data":
                []

        }), 503

    print(
        "================================================="
    )

    print(
        "YALCIN PRO - ANDROID ISTEGI:",
        len(symbols),
        "HISSE"
    )

    # ---------------------------------------------------------
    # ARKA PLAN YENİLEME
    # ---------------------------------------------------------

    start_background_refresh(
        symbols
    )

    # ---------------------------------------------------------
    # CACHE
    # ---------------------------------------------------------

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

            result_map[
                symbol
            ] = result

            if (
                age
                <
                CACHE_TTL_SECONDS
            ):

                fresh_count += 1

            else:

                stale_count += 1

    # ---------------------------------------------------------
    # ANDROID SIRASINI KORU + EKSIKLERI DE GONDER
    # ---------------------------------------------------------
    # Bazı hisselerin Yahoo verisi o anda yoksa listeyi 438'e
    # düşürmüyoruz. Sembol kaydı 0.0 değerleriyle korunuyor.
    # Arka plan yenilemesi sonraki turlarda gerçek fiyatı doldurur.
    ordered_results = []

    for symbol in symbols:
        result = result_map.get(symbol)

        if result is not None:
            ordered_results.append(result)
        else:
            ordered_results.append({
                "sembol": symbol,
                "fiyat": 0.0,
                "oncekiKapanis": 0.0,
                "degisimYuzde": 0.0,
                "paraBirimi": "TRY"
            })

    # ---------------------------------------------------------
    # EKSİKLER
    # ---------------------------------------------------------

    missing = [

        symbol

        for symbol in symbols

        if symbol not in result_map

    ]

    print(
        "YALCIN PRO - CACHE:",
        len(result_map),
        "/",
        len(symbols),
        "| TAZE:",
        fresh_count,
        "| ESKI:",
        stale_count,
        "| EKSIK:",
        len(missing)
    )

    if missing:

        print(
            "YALCIN PRO - VERISI OLMAYAN:",
            ", ".join(
                missing[:50]
            )
        )

    print(
        "YALCIN PRO - CEVAP:",
        len(ordered_results),
        "/",
        len(symbols)
    )

    for debug_symbol in (
        "THYAO",
        "VERUS",
        "USHOL",
        "YUNSA",
        "AKBNK"
    ):
        debug_item = result_map.get(debug_symbol)

        if debug_item:
            print(
                "YALCIN PRO - /stocks FIYAT:",
                debug_symbol,
                "=",
                debug_item.get("fiyat"),
                "| DEG:",
                debug_item.get("degisimYuzde")
            )

    print(
        "================================================="
    )

    return jsonify({

        "success":
            True,

        "data":
            ordered_results

    })


# =============================================================
# SERVER
# =============================================================

def start_server_bootstrap():
    """
    Server portu açıldıktan sonra KAP -> Yahoo sembol keşfini
    arka planda yapar. Keşif tamamlanınca canlı fiyat yenilemesi
    başlatılır. Böylece Flask 5000 portunu bekletmez.
    """

    def bootstrap_worker():
        global _last_symbol_refresh

        try:
            print(
                "YALCIN PRO - ARKA PLAN SEMBOL KESFI BASLADI"
            )

            with _symbol_list_lock:
                current = list(_symbol_list)

            if len(current) >= TARGET_BIST_STOCK_COUNT:
                print(
                    "YALCIN PRO - HAZIR AKTIF EVREN:",
                    len(current),
                    "HISSE"
                )

                _last_symbol_refresh = time.time()
                start_background_refresh(current)
                return

            discovered = _refresh_symbol_universe(force=True)

            if discovered:
                print(
                    "YALCIN PRO - ARKA PLAN SEMBOL KESFI TAMAM:",
                    len(discovered),
                    "HISSE"
                )
                start_background_refresh(discovered)
            else:
                print(
                    "YALCIN PRO - SEMBOL KESFI SONUC VERMEDI"
                )

        except Exception as e:
            print(
                "YALCIN PRO - BASLANGIC SEMBOL KESFI HATASI:",
                e
            )

    thread = threading.Thread(
        target=bootstrap_worker,
        daemon=True,
        name="yalcin-symbol-bootstrap"
    )
    thread.start()


# =============================================================
# SERVER
# =============================================================

if __name__ == "__main__":

    with _cache_lock:
        cache_count = len(_stock_cache)

    with _symbol_list_lock:
        symbol_count = len(_symbol_list)

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
        "SEMBOL:",
        symbol_count,
        "HISSE"
    )

    print(
        "PORT: 5000"
    )

    print(
        "CANLI YENILEME:",
        BACKGROUND_REFRESH_SECONDS,
        "SANIYE"
    )

    print(
        "SEMBOL YENILEME:",
        SYMBOL_REFRESH_SECONDS,
        "SANIYE"
    )

    print(
        "CACHE TTL:",
        CACHE_TTL_SECONDS,
        "SANIYE"
    )

    print(
        "BATCH:",
        BATCH_SIZE,
        "HISSE"
    )

    print(
        "================================================="
    )

    # Flask önce 5000 portunu açacak.
    # KAP/Yahoo sembol keşfi ayrı thread'de çalışacak.
    start_server_bootstrap()

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

