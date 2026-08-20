
  from flask import Flask, jsonify, request
import yfinance as yf
import concurrent.futures
import os
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

ISTANBUL_TZ = ZoneInfo("Europe/Istanbul")


def get_stock(symbol):
    try:
        symbol = symbol.strip().upper()

        ticker = yf.Ticker(symbol + ".IS")

        # =========================================================
        # 1 - GÜN İÇİ / CANLI FİYAT
        # =========================================================
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

            # Canlı veri gelmezse günlük veriye düş
            daily_fallback = ticker.history(
                period="5d",
                interval="1d",
                auto_adjust=False
            )

            daily_fallback = daily_fallback.dropna(
                subset=["Close"]
            )

            if daily_fallback.empty:
                return None

            price = float(
                daily_fallback.iloc[-1]["Close"]
            )

        else:
            # Gün içindeki en son fiyat
            price = float(
                intraday.iloc[-1]["Close"]
            )

        # =========================================================
        # 2 - GÜNLÜK VERİ
        # =========================================================
        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False
        )

        daily = daily.dropna(
            subset=["Close"]
        )

        if daily.empty:
            return None

        # =========================================================
        # 3 - ÖNCEKİ KAPANIŞ
        # =========================================================
        now_istanbul = datetime.now(
            ISTANBUL_TZ
        )

        today = now_istanbul.date()

        last_daily_date = daily.index[-1].date()

        if (
            last_daily_date == today
            and len(daily) >= 2
        ):
            # Bugünün devam eden seansı varsa
            # bir önceki tamamlanmış günün kapanışı
            previous = float(
                daily.iloc[-2]["Close"]
            )

        elif len(daily) >= 2:
            # Piyasa kapalıysa son işlem gününün
            # kapanışını kullan
            previous = float(
                daily.iloc[-1]["Close"]
            )

        else:
            previous = price

        # =========================================================
        # 4 - DEĞİŞİM YÜZDESİ
        # =========================================================
        if previous != 0:
            change = (
                (price - previous)
                / previous
            ) * 100.0
        else:
            change = 0.0

        # =========================================================
        # 5 - DEBUG LOG
        # =========================================================
        print(
            f"YALCIN PRO - {symbol} | "
            f"CANLI={price:.4f} | "
            f"ONCEKI={previous:.4f} | "
            f"DEGISIM={change:.4f}%"
        )

        # Son 3 günlük kapanışı göster
        try:
            print(
                f"YALCIN PRO - {symbol} "
                f"SON GUNLUK VERILER: "
                f"{daily[['Close']].tail(3).to_dict()}"
            )
        except Exception:
            pass

        # Son 3 dakikalık veriyi göster
        try:
            if not intraday.empty:
                print(
                    f"YALCIN PRO - {symbol} "
                    f"SON DAKIKALIK VERILER: "
                    f"{intraday[['Close']].tail(3).to_dict()}"
                )
        except Exception:
            pass

        # =========================================================
        # 6 - ANDROID'A GÖNDERİLECEK VERİ
        # =========================================================
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

    symbol = sembol.strip().upper()

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
    # Sembolleri temizle
    # ---------------------------------------------------------

    symbols = [
        s.strip().upper()
        for s in symbols_text.split(",")
        if s.strip()
    ]

    # Aynı sembolleri temizle
    symbols = list(
        dict.fromkeys(symbols)
    )

    print(
        "YALCIN PRO - ISTENEN HISSE SAYISI:",
        len(symbols)
    )

    results = []

    # ---------------------------------------------------------
    # GRUPLARA AYIR
    # ---------------------------------------------------------

    batch_size = 25

    batches = [
        symbols[i:i + batch_size]
        for i in range(
            0,
            len(symbols),
            batch_size
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
                batch_results.append(
                    result
                )

        return batch_results

    # ---------------------------------------------------------
    # PARALEL ÇALIŞTIR
    # ---------------------------------------------------------

    try:

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=4
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
    # ANDROID'DEKİ SIRAYI KORU
    # =========================================================

    result_map = {
        item["sembol"]: item
        for item in results
    }

    ordered_results = [
        result_map[symbol]
        for symbol in symbols
        if symbol in result_map
    ]

    print(
        "YALCIN PRO - "
        "PYTHON STOCKS TAMAMLANDI:",
        len(ordered_results),
        "/",
        len(symbols)
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
