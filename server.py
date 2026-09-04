from flask import Flask, jsonify, request
import yfinance as yf
import threading
import time
import os
import json
import re
import urllib.request
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

# KAP BIST şirketleri
KAP_BIST_URL = "https://kap.org.tr/tr/bist-sirketler"

# KAP BIST şirket sayısı için güvenlik sınırı.
# Liste bu sınırı aşarsa HTML parser tekrar kontrol edilmeden kullanılmaz.
MAX_KAP_SYMBOLS = 1200

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

def get_bist_symbols():
    global _symbol_list

    # Önce RAM'de doğrulanmış liste varsa kullan.
    with _symbol_list_lock:
        current = list(_symbol_list)

    if current:
        return current

    # Önce KAP'tan güncel BIST listesini al.
    # Böylece eski/hatalı sembol cache'i yeni listeyi bozmaz.
    fresh_symbols = _download_kap_symbols()

    if fresh_symbols:
        with _symbol_list_lock:
            _symbol_list = fresh_symbols
        _save_symbol_cache(fresh_symbols)
        return fresh_symbols

    # KAP erişilemezse doğrulanmış dosya cache'ini kullan.
    _load_symbol_cache()

    with _symbol_list_lock:
        current = list(_symbol_list)

    if current:
        return current

    return []


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
# TOPLU HİSSELER - YAHOO
# =============================================================

def get_stocks_batch(
    symbols
):

    symbols = [
        normalize_symbol(s)
        for s in symbols
        if normalize_symbol(s)
    ]

    symbols = list(
        dict.fromkeys(symbols)
    )

    if not symbols:
        return [], []

    results = []
    missing = []

    tickers = [
        symbol + ".IS"
        for symbol in symbols
    ]

    try:

        print(
            "YALCIN PRO - TOPLU YAHOO:",
            len(tickers),
            "HISSE"
        )

        # -----------------------------------------------------
        # INTRADAY
        # -----------------------------------------------------

        intraday_data = yf.download(
            tickers=tickers,
            period=INTRADAY_PERIOD,
            interval=INTRADAY_INTERVAL,
            group_by="ticker",
            auto_adjust=False,
            prepost=False,
            threads=True,
            progress=False
        )

        # -----------------------------------------------------
        # GÜNLÜK
        # -----------------------------------------------------

        daily_data = yf.download(
            tickers=tickers,
            period=DAILY_PERIOD,
            interval=DAILY_INTERVAL,
            group_by="ticker",
            auto_adjust=False,
            prepost=False,
            threads=True,
            progress=False
        )

        # -----------------------------------------------------
        # HİSSELER
        # -----------------------------------------------------

        for symbol in symbols:

            ticker_name = symbol + ".IS"

            try:

                closes = _extract_close(
                    intraday_data,
                    ticker_name
                )

                if (
                    closes is None
                    or closes.empty
                ):

                    closes = _extract_close(
                        daily_data,
                        ticker_name
                    )

                if (
                    closes is None
                    or closes.empty
                ):

                    missing.append(symbol)
                    continue

                daily_closes = _extract_close(
                    daily_data,
                    ticker_name
                )

                previous_close = None

                if (
                    daily_closes is not None
                    and not daily_closes.empty
                ):

                    daily_closes = daily_closes.dropna()

                    if len(daily_closes) >= 2:

                        try:

                            value = float(
                                daily_closes.iloc[-2]
                            )

                            if value > 0:
                                previous_close = value

                        except Exception:
                            previous_close = None

                result = _make_result(
                    symbol,
                    closes,
                    previous_close
                )

                if result:
                    results.append(result)
                else:
                    missing.append(symbol)

            except Exception as e:

                print(
                    "YALCIN PRO - HISSE HATASI:",
                    symbol,
                    e
                )

                missing.append(symbol)

    except Exception as e:

        print(
            "YALCIN PRO - TOPLU YAHOO HATASI:",
            e
        )

        return [], list(symbols)

    print(
        "YALCIN PRO - GRUP SONUCU:",
        len(results),
        "/",
        len(symbols),
        "| EKSIK:",
        len(missing)
    )

    return results, missing


# =============================================================
# TEK HİSSE - YAHOO
# =============================================================

def get_stock_from_yahoo(
    symbol
):

    symbol = normalize_symbol(
        symbol
    )

    if not symbol:

        return None

    ticker_name = (
        symbol + ".IS"
    )

    try:

        print(
            "YALCIN PRO - TEK YAHOO:",
            ticker_name
        )

        # -----------------------------------------------------
        # INTRADAY
        # -----------------------------------------------------

        intraday = yf.download(

            tickers=ticker_name,

            period=INTRADAY_PERIOD,

            interval=INTRADAY_INTERVAL,

            auto_adjust=False,

            prepost=False,

            threads=False,

            progress=False

        )

        closes = _extract_close(

            intraday,

            ticker_name

        )

        # -----------------------------------------------------
        # GÜNLÜK
        # -----------------------------------------------------

        daily = yf.download(

            tickers=ticker_name,

            period=DAILY_PERIOD,

            interval=DAILY_INTERVAL,

            auto_adjust=False,

            prepost=False,

            threads=False,

            progress=False

        )

        daily_closes = _extract_close(

            daily,

            ticker_name

        )

        # -----------------------------------------------------
        # INTRADAY YOKSA GÜNLÜK
        # -----------------------------------------------------

        if (
            closes is None
            or
            closes.empty
        ):

            closes = daily_closes

        if (
            closes is None
            or
            closes.empty
        ):

            print(
                "YALCIN PRO - VERI YOK:",
                symbol
            )

            return None

        # -----------------------------------------------------
        # ÖNCEKİ KAPANIŞ
        # -----------------------------------------------------

        previous_close = None

        if (
            daily_closes is not None
            and
            not daily_closes.empty
        ):

            daily_closes = (
                daily_closes
                .dropna()
            )

            if len(
                daily_closes
            ) >= 2:

                try:

                    previous_close = float(
                        daily_closes.iloc[-2]
                    )

                    if previous_close <= 0:

                        previous_close = None

                except Exception:

                    previous_close = None

        # -----------------------------------------------------
        # SONUÇ
        # -----------------------------------------------------

        return _make_result(

            symbol,

            closes,

            previous_close

        )

    except Exception as e:

        print(
            "YALCIN PRO - YAHOO HATASI:",
            symbol,
            e
        )

        return None


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

        global _background_refresh_started

        print(
            "YALCIN PRO - ARKA PLAN BASLADI:",
            len(refresh_symbols),
            "HISSE"
        )

        while True:

            try:

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
                # HER GRUP
                # -------------------------------------------------

                for index, batch in enumerate(
                    batches,
                    start=1
                ):

                    print(
                        "YALCIN PRO - GRUP:",
                        index,
                        "/",
                        len(batches),
                        "|",
                        len(batch),
                        "HISSE"
                    )

                    success = False

                    # -------------------------------------------------
                    # RETRY
                    # -------------------------------------------------

                    for retry in range(
                        RETRY_COUNT + 1
                    ):

                        try:

                            results, missing = (
                                get_stocks_batch(
                                    batch
                                )
                            )

                            # -----------------------------------------
                            # BAŞARILI VERİLERİ CACHE
                            # -----------------------------------------

                            for result in results:

                                symbol = normalize_symbol(

                                    result.get(
                                        "sembol",
                                        ""
                                    )

                                )

                                if symbol:

                                    _save_stock_memory(

                                        symbol,

                                        result

                                    )

                            total_updated += (
                                len(results)
                            )

                            total_missing += (
                                len(missing)
                            )

                            success = True

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

                            break

                        except Exception as e:

                            print(
                                "YALCIN PRO - GRUP HATASI:",
                                index,
                                "| DENEME:",
                                retry + 1,
                                e
                            )

                            if retry < RETRY_COUNT:

                                time.sleep(
                                    RETRY_WAIT_SECONDS
                                )

                    # -------------------------------------------------
                    # BAŞARISIZSA ESKİ CACHE KORUNUR
                    # -------------------------------------------------

                    if not success:

                        print(
                            "YALCIN PRO - GRUP BASARISIZ:",
                            index,
                            "| HISSE:",
                            len(batch)
                        )

                    # Gruplar arası kısa bekleme
                    time.sleep(0.5)

                # -------------------------------------------------
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
# CACHE YÜKLE
# =============================================================

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

    symbols_text = request.args.get(
        "symbols",
        ""
    )

    # ---------------------------------------------------------
    # ANDROID SEMBOL GÖNDERMEDİYSE
    # DİNAMİK LİSTEYİ KULLAN
    # ---------------------------------------------------------

    if not symbols_text:

        symbols = (
            get_bist_symbols()
        )

    else:

        symbols = [

            normalize_symbol(s)

            for s in symbols_text.split(",")

            if s.strip()

        ]

        symbols = list(
            dict.fromkeys(
                symbols
            )
        )

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
    # ANDROID SIRASINI KORU
    # ---------------------------------------------------------

    ordered_results = [

        result_map[symbol]

        for symbol in symbols

        if symbol in result_map

    ]

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

if __name__ == "__main__":

    with _cache_lock:

        cache_count = len(
            _stock_cache
        )

    with _symbol_list_lock:

        symbol_count = len(
            _symbol_list
        )

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
