
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
