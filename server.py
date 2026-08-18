from flask import Flask, jsonify, request
import yfinance as yf
import concurrent.futures
import os

app = Flask(__name__)


def get_stock(symbol):
    try:
        ticker = yf.Ticker(symbol + ".IS")
        data = ticker.history(
            period="2d",
            auto_adjust=False
        )

        if data.empty:
            return None

        data = data.dropna(subset=["Close"])

        if data.empty:
            return None

        last = data.iloc[-1]
        price = float(last["Close"])

        if len(data) >= 2:
            previous = float(data.iloc[-2]["Close"])
            change = ((price - previous) / previous) * 100
        else:
            previous = price
            change = 0.0

        return {
            "sembol": symbol,
            "fiyat": round(price, 2),
            "oncekiKapanis": round(previous, 2),
            "degisimYuzde": round(change, 2),
            "paraBirimi": "TRY"
        }

    except Exception as e:
        print(f"Hisse hatası {symbol}: {e}")
        return None


@app.route("/health")
def health():
    return jsonify({
        "success": True,
        "status": "online"
    })


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


@app.route("/stocks")
def stocks():
    symbols_text = request.args.get("symbols", "")

    if not symbols_text:
        return jsonify({
            "success": False,
            "error": "symbols parametresi gerekli",
            "data": []
        }), 400

    symbols = [
        s.strip().upper()
        for s in symbols_text.split(",")
        if s.strip()
    ]

    # Aynı sembol tekrarlarını temizle
    symbols = list(dict.fromkeys(symbols))

    print(
        "YALCIN PRO - ISTENEN HISSE SAYISI:",
        len(symbols)
    )

    results = []

    # Küçük gruplar halinde çalıştırıyoruz.
    # Böylece 439 hissenin tek seferde yüklenmesini engelliyoruz.
    batch_size = 25

    batches = [
        symbols[i:i + batch_size]
        for i in range(0, len(symbols), batch_size)
    ]

    print(
        "YALCIN PRO - TOPLAM GRUP:",
        len(batches)
    )

    def fetch_batch(batch):
        batch_results = []

        for symbol in batch:
            result = get_stock(symbol)

            if result is not None:
                batch_results.append(result)

        return batch_results

    try:
        # Aynı anda 4 grup çalıştır.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=4
        ) as executor:

            futures = [
                executor.submit(fetch_batch, batch)
                for batch in batches
            ]

            for future in concurrent.futures.as_completed(futures):
                try:
                    batch_results = future.result()
                    results.extend(batch_results)

                    print(
                        "YALCIN PRO - GRUP TAMAMLANDI:",
                        len(batch_results),
                        "| TOPLAM:",
                        len(results)
                    )

                except Exception as e:
                    print(
                        "YALCIN PRO - GRUP HATASI:",
                        e
                    )

    except Exception as e:
        print(
            "YALCIN PRO - GENEL HATA:",
            e
        )

    # Uygulamadaki sembol sırasını koru.
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
        "YALCIN PRO - PYTHON STOCKS TAMAMLANDI:",
        len(ordered_results),
        "/",
        len(symbols)
    )

    return jsonify({
        "success": True,
        "data": ordered_results
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
