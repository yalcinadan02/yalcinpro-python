from flask import Flask, jsonify, request
import yfinance as yf
import concurrent.futures
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
MAX_WORKERS = 4

# İlk istekte gelmeyen hisseler için tekrar deneme
MISSING_RETRY_COUNT = 3
RETRY_WAIT_SECONDS = 1.5


def normalize_symbol(symbol):
    return symbol.strip().upper()


def get_stock(symbol, retry_count=0):
    """
    Tek hisseyi Yahoo Finance'tan alır.

    Öncelik:
    1) 1 dakikalık gün içi veri
    2) 5 günlük günlük veri
    3) Eksik/boş sonuçta tekrar deneme
    """

    symbol = normalize_symbol(symbol)

    try:
        ticker = yf.Ticker(symbol + ".IS")

        # =====================================================
        # 1 - GÜN İÇİ / CANLI FİYAT
        # =====================================================

        intraday = ticker.history(
            period="1d",
            interval="1m",
            auto_adjust=False,
            prepost=False
        )

        intraday = intraday.dropna(
            subset=["Close"]
        )

        if intraday.empty:

            print(
                f"YALCIN PRO - {symbol}: "
                f"1 DAKIKALIK VERI YOK"
            )

            # Canlı veri yoksa günlük veriye düş
            daily_fallback = ticker.history(
                period="5d",
                interval="1d",
                auto_adjust=False
            )

            daily_fallback = daily_fallback.dropna(
                subset=["Close"]
            )

            if daily_fallback.empty:
                raise ValueError(
                    "Günlük veri de bulunamadı"
                )

            price = float(
                daily_fallback.iloc[-1]["Close"]
            )

        else:

            # Gün içindeki en son fiyat
            price = float(
                intraday.iloc[-1]["Close"]
            )

        # =====================================================
        # 2 - GÜNLÜK VERİ
        # =====================================================

        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False
        )

        daily = daily.dropna(
            subset=["Close"]
        )

        if daily.empty:
            raise ValueError(
                "Günlük veri bulunamadı"
            )

        # =====================================================
        # 3 - ÖNCEKİ KAPANIŞ
        # =====================================================

        now_istanbul = datetime.now(
            ISTANBUL_TZ
        )

        today = now_istanbul.date()

        last_daily_date = daily.index[-1].date()

        if (
            last_daily_date == today
            and len(daily) >= 2
        ):
            # Bugünün devam eden seansı varsa,
            # bir önceki tamamlanmış günün kapanışı.
            previous = float(
                daily.iloc[-2]["Close"]
            )

        elif len(daily) >= 2:
            # Piyasa kapalıysa son günlük verinin
            # kapanışı mevcut fiyatla karşılaştırılır.
            previous = float(
                daily.iloc[-1]["Close"]
            )

        else:
            previous = price

        # =====================================================
        # 4 - DEĞİŞİM YÜZDESİ
        # =====================================================

        if previous != 0:
            change = (
                (price - previous)
                / previous
            ) * 100.0
        else:
            change = 0.0

        # =====================================================
        # 5 - DEBUG LOG
        # =====================================================

        print(
            f"YALCIN PRO - {symbol} | "
            f"CANLI={price:.4f} | "
            f"ONCEKI={previous:.4f} | "
            f"DEGISIM={change:.4f}%"
        )

        # Son 3 günlük kapanış
        try:
            print(
                f"YALCIN PRO - {symbol} "
                f"SON GUNLUK VERILER: "
                f"{daily[['Close']].tail(3).to_dict()}"
            )
        except Exception:
            pass

        # Son 3 dakikalık veri
        try:
            if not intraday.empty:
                print(
                    f"YALCIN PRO - {symbol} "
                    f"SON DAKIKALIK VERILER: "
                    f"{intraday[['Close']].tail(3).to_dict()}"
                )
        except Exception:
            pass

        # =====================================================
        # 6 - ANDROID'A GÖNDERİLECEK VERİ
        # =====================================================

        return {
            "sembol": symbol,
            "fiyat": round(price, 2),
            "oncekiKapanis": round(previous, 2),
            "degisimYuzde": round(change, 2),
            "paraBirimi": "TRY"
        }

    except Exception as e:

        print(
            f"YALCIN PRO - HISSE HATASI "
            f"{symbol}: {e}"
        )

        # =====================================================
        # EKSİK HİSSEYİ TEKRAR DENE
        # =====================================================

        if retry_count < MISSING_RETRY_COUNT:

            attempt = retry_count + 1

            print(
                f"YALCIN PRO - {symbol} "
                f"TEKRAR DENENIYOR "
                f"({attempt}/{MISSING_RETRY_COUNT})"
            )

            time.sleep(
                RETRY_WAIT_SECONDS * attempt
            )

            return get_stock(
                symbol,
                retry_count=attempt
            )

        print(
            f"YALCIN PRO - {symbol}: "
            f"3 DENEME SONRASI VERI YOK"
        )

        return None


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

    symbol = normalize_symbol(sembol)

    result = get_stock(symbol)

    if result is None:

        return jsonify({
            "success": False,
            "data": []
        }), 404

    return jsonify({
        "success": True,
        "data": [result]
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
    # BİR GRUBU ÇALIŞTIR
    # ---------------------------------------------------------

    def fetch_batch(batch):

        batch_results = []

        for symbol in batch:

            result = get_stock(symbol)

            if result is not None:
                batch_results.append(result)

        return batch_results

    # ---------------------------------------------------------
    # İLK TUR - PARALEL
    # ---------------------------------------------------------

    try:

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_WORKERS
        ) as executor:

            futures = [
                executor.submit(
                    fetch_batch,
                    batch
                )
                for batch in batches
            ]

            for future in concurrent.futures.as_completed(
                futures
            ):

                try:

                    batch_results = future.result()

                    results.extend(
                        batch_results
                    )

                    print(
                        "YALCIN PRO - "
                        "GRUP TAMAMLANDI:",
                        len(batch_results),
                        "| TOPLAM:",
                        len(results)
                    )

                except Exception as e:

                    print(
                        "YALCIN PRO - "
                        "GRUP HATASI:",
                        e
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
            item.get("sembol", "")
        )

        if symbol:
            result_map[symbol] = item

    # =========================================================
    # ÇOK ÖNEMLİ:
    # İLK TURDA GELMEYEN HİSSELERİ TESPİT ET
    # =========================================================

    missing_symbols = [
        symbol
        for symbol in symbols
        if symbol not in result_map
    ]

    print(
        "YALCIN PRO - ILK TUR SONRASI:",
        len(result_map),
        "/",
        len(symbols)
    )

    print(
        "YALCIN PRO - EKSIK HISSE SAYISI:",
        len(missing_symbols)
    )

    if missing_symbols:

        print(
            "YALCIN PRO - EKSIK HISSELER:",
            ", ".join(missing_symbols)
        )

        # =====================================================
        # İKİNCİ TUR
        # Sadece eksik hisseleri tekrar sorgula.
        # Bu turda daha düşük eşzamanlılık kullanıyoruz.
        # =====================================================

        def retry_missing(symbol):

            print(
                f"YALCIN PRO - "
                f"EKSIK HISSE TEKRAR SORULUYOR: "
                f"{symbol}"
            )

            # get_stock kendi içinde 3 kez deniyor.
            return get_stock(symbol)

        try:

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=2
            ) as retry_executor:

                retry_futures = {
                    retry_executor.submit(
                        retry_missing,
                        symbol
                    ): symbol
                    for symbol in missing_symbols
                }

                for future in concurrent.futures.as_completed(
                    retry_futures
                ):

                    symbol = retry_futures[future]

                    try:

                        retry_result = future.result()

                        if retry_result is not None:

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

                        else:

                            print(
                                "YALCIN PRO - "
                                "EKSIK HISSE HALA YOK:",
                                symbol
                            )

                    except Exception as e:

                        print(
                            "YALCIN PRO - "
                            "EKSIK HISSE HATASI:",
                            symbol,
                            e
                        )

        except Exception as e:

            print(
                "YALCIN PRO - "
                "IKINCI TUR HATASI:",
                e
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
            ", ".join(final_missing)
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
        debug=False
    )

