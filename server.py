from flask import Flask, jsonify, request
import yfinance as yf

app = Flask(__name__)


def get_stock(symbol):
    ticker = yf.Ticker(symbol + ".IS")

    data = ticker.history(period="2d")

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

@app.route("/stocks")
def stocks():
    symbols_text = request.args.get("symbols", "")

    if not symbols_text:
        return jsonify({
            "success": False,
            "error": "symbols parametresi gerekli"
        }), 400

    symbols = [
        s.strip().upper()
        for s in symbols_text.split(",")
        if s.strip()
    ]

    results = []

    # Aynı anda 10 hisse çek
    import concurrent.futures

    def fetch(symbol):
        try:
            return get_stock(symbol)
        except Exception as e:
            print(f"Hisse hatası {symbol}: {e}")
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [
            executor.submit(fetch, symbol)
            for symbol in symbols
        ]

        for future in concurrent.futures.as_completed(futures):
            result = future.result()

            if result is not None:
                results.append(result)

    print("YALCIN PRO - PYTHON STOCKS TAMAMLANDI:", len(results))

    return jsonify({
        "success": True,
        "data": results
    })
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )
