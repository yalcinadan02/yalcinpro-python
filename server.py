from flask import Flask, jsonify, request
import yfinance as yf
import os

app = Flask(__name__)


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

    # Yahoo Finance sembolleri
    yahoo_symbols = [symbol + ".IS" for symbol in symbols]

    results = []

    try:
        # TÜM hisseleri tek seferde indiriyoruz.
        data = yf.download(
            yahoo_symbols,
            period="2d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True
        )

        for symbol, yahoo_symbol in zip(symbols, yahoo_symbols):
            try:
                # Tek hisse durumunda kolon yapısı farklı olabilir.
                if len(yahoo_symbols) == 1:
                    stock_data = data
                else:
                    if yahoo_symbol not in data.columns.get_level_values(0):
                        continue

                    stock_data = data[yahoo_symbol]

                stock_data = stock_data.dropna(subset=["Close"])

                if stock_data.empty:
                    continue

                last = stock_data.iloc[-1]
                price = float(last["Close"])

                if len(stock_data) >= 2:
                    previous = float(stock_data.iloc[-2]["Close"])
                    change = ((price - previous) / previous) * 100
                else:
                    previous = price
                    change = 0.0

                results.append({
                    "sembol": symbol,
                    "fiyat": round(price, 2),
                    "oncekiKapanis": round(previous, 2),
                    "degisimYuzde": round(change, 2),
                    "paraBirimi": "TRY"
                })

            except Exception as e:
                print(f"Hisse işleme hatası {symbol}: {e}")

    except Exception as e:
        print(f"Yahoo Finance toplu veri hatası: {e}")

        return jsonify({
            "success": False,
            "error": str(e),
            "data": []
        }), 500

    print(
        "YALCIN PRO - PYTHON STOCKS TAMAMLANDI:",
        len(results),
        "/",
        len(symbols)
    )

    return jsonify({
        "success": True,
        "data": results
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
