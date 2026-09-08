```
package com.yalcnpro.ai

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.lifecycleScope
import com.yalcnpro.ai.network.BistStock
import com.yalcnpro.ai.network.RetrofitClient
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.isActive
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class MainActivity : ComponentActivity() {

    // =========================================================
    // ANA STATE
    // =========================================================

    private var stocks by mutableStateOf<List<BistStock>>(emptyList())

    private var isRefreshing by mutableStateOf(false)

    private var loadError by mutableStateOf("")

    private var lastUpdateTime by mutableStateOf("--:--:--")

    private var serverSymbolCount by mutableStateOf(0)

    private var refreshJob: Job? = null


    // =========================================================
    // SEMBOL NORMALİZASYONU
    // =========================================================

    private fun normalizeSymbol(symbol: String?): String {

        return symbol
            ?.trim()
            ?.uppercase(Locale.ROOT)
            ?.removeSuffix(".IS")
            ?.replace(" ", "")
            ?: ""
    }


    // =========================================================
    // AI ANALİZİ
    // =========================================================

    data class AiAnalysis(
        val signal: String,
        val confidence: Int,
        val risk: String
    )


    private fun analyzeStock(
        change: Double?
    ): AiAnalysis {

        if (change == null) {

            return AiAnalysis(
                signal = "VERİ YOK",
                confidence = 0,
                risk = "Bekleniyor"
            )
        }

        return when {

            change >= 3.0 -> {

                AiAnalysis(
                    signal = "GÜÇLÜ AL",
                    confidence = 95,
                    risk = "Düşük"
                )
            }

            change >= 1.0 -> {

                val confidence =
                    (
                            75.0 +
                                    ((change - 1.0) / 2.0 * 14.0)
                            )
                        .toInt()
                        .coerceIn(75, 89)

                AiAnalysis(
                    signal = "AL",
                    confidence = confidence,
                    risk = "Orta"
                )
            }

            change >= 0.25 -> {

                val confidence =
                    (
                            60.0 +
                                    ((change - 0.25) / 0.75 * 14.0)
                            )
                        .toInt()
                        .coerceIn(60, 74)

                AiAnalysis(
                    signal = "ZAYIF AL",
                    confidence = confidence,
                    risk = "Orta"
                )
            }

            change > -0.25 -> {

                AiAnalysis(
                    signal = "BEKLE",
                    confidence = 55,
                    risk = "Orta"
                )
            }

            change > -1.0 -> {

                val confidence =
                    (
                            60.0 +
                                    ((-change - 0.25) / 0.75 * 14.0)
                            )
                        .toInt()
                        .coerceIn(60, 74)

                AiAnalysis(
                    signal = "ZAYIF SAT",
                    confidence = confidence,
                    risk = "Orta"
                )
            }

            change > -3.0 -> {

                val confidence =
                    (
                            75.0 +
                                    ((-change - 1.0) / 2.0 * 14.0)
                            )
                        .toInt()
                        .coerceIn(75, 89)

                AiAnalysis(
                    signal = "SAT",
                    confidence = confidence,
                    risk = "Yüksek"
                )
            }

            else -> {

                AiAnalysis(
                    signal = "GÜÇLÜ SAT",
                    confidence = 95,
                    risk = "Yüksek"
                )
            }
        }
    }


    // =========================================================
    // ACTIVITY
    // =========================================================

    override fun onCreate(
        savedInstanceState: Bundle?
    ) {

        super.onCreate(savedInstanceState)


        // =====================================================
        // UI
        // =====================================================

        setContent {

            MaterialTheme {

                var searchText by remember {
                    mutableStateOf("")
                }

                var sortOption by remember {
                    mutableStateOf("Varsayılan")
                }

                // =================================================
                // AŞAĞI ÇEKEREK MANUEL YENİLEME
                // =================================================
                val pullToRefreshState =
                    rememberPullToRefreshState()


                // =================================================
                // SERVER'DAN GELEN TÜM HİSSELERİ KULLAN
                // =================================================
                // Android artık BistSymbols listesini ekranı
                // 438 hisseye sınırlamak için kullanmıyor.
                // Server kaç hisse gönderirse tamamı gösterilir.

                val allStocks =
                    remember(stocks) {
                        stocks
                            .filter {
                                it.sembol.isNotBlank()
                            }
                            .distinctBy {
                                normalizeSymbol(it.sembol)
                            }
                    }


                // =================================================
                // ARAMA + SIRALAMA
                // =================================================

                val filteredStocks =
                    remember(
                        searchText,
                        sortOption,
                        allStocks
                    ) {

                        var result =
                            allStocks.filter {

                                it.sembol.contains(
                                    searchText,
                                    ignoreCase = true
                                )
                            }


                        result =
                            when (sortOption) {

                                "Artan" -> {

                                    result.sortedByDescending {
                                        it.degisimYuzde
                                    }
                                }

                                "Düşen" -> {

                                    result.sortedBy {
                                        it.degisimYuzde
                                    }
                                }

                                "Alfabetik" -> {

                                    result.sortedBy {
                                        it.sembol
                                    }
                                }

                                else -> {

                                    result
                                }
                            }


                        result
                    }


                // =================================================
                // ANA EKRAN
                // =================================================

                Scaffold(
                    modifier =
                        Modifier.fillMaxSize()
                ) { innerPadding ->

                    Column(

                        modifier =
                            Modifier
                                .fillMaxSize()
                                .padding(
                                    innerPadding
                                )
                                .padding(16.dp)
                    ) {

                        Text(

                            text =
                                "Yalçın Pro Borsa",

                            fontSize =
                                28.sp,

                            fontWeight =
                                FontWeight.Bold
                        )


                        Spacer(
                            modifier =
                                Modifier.height(4.dp)
                        )


                        Text(

                            text =
                                "Canlı BIST Piyasası",

                            fontSize =
                                14.sp,

                            color =
                                MaterialTheme
                                    .colorScheme
                                    .onSurfaceVariant
                        )


                        Spacer(
                            modifier =
                                Modifier.height(8.dp)
                        )


                        // =================================================
                        // DURUM
                        // =================================================

                        Text(

                            text =
                                if (isRefreshing) {

                                    "● Fiyatlar güncelleniyor..."

                                } else {

                                    "● Son güncelleme: $lastUpdateTime"
                                },

                            fontSize =
                                13.sp,

                            color =
                                if (isRefreshing) {

                                    Color(0xFFFF9800)

                                } else {

                                    Color(0xFF16803C)
                                }
                        )


                        Spacer(
                            modifier =
                                Modifier.height(4.dp)
                        )


                        Text(

                            text =
                                "Gösterilen: ${filteredStocks.size} / ${serverSymbolCount} hisse",

                            fontSize =
                                14.sp,

                            fontWeight =
                                FontWeight.Medium
                        )


                        if (
                            loadError.isNotBlank()
                        ) {

                            Spacer(
                                modifier =
                                    Modifier.height(4.dp)
                            )

                            Text(

                                text =
                                    "Hata: $loadError",

                                color =
                                    Color(0xFFD32F2F),

                                fontSize =
                                    12.sp
                            )
                        }


                        Spacer(
                            modifier =
                                Modifier.height(10.dp)
                        )


                        // =================================================
                        // YENİLE + SIRALAMA
                        // =================================================

                        Row(

                            modifier =
                                Modifier.fillMaxWidth(),

                            horizontalArrangement =
                                Arrangement.spacedBy(6.dp)
                        ) {

                            Button(

                                modifier =
                                    Modifier.weight(1f),

                                enabled =
                                    !isRefreshing,

                                onClick = {

                                    lifecycleScope.launch {

                                        refreshStocks()
                                    }
                                }

                            ) {

                                Text(
                                    "↻ YENİLE",
                                    fontSize = 12.sp
                                )
                            }


                            Button(

                                modifier =
                                    Modifier.weight(1f),

                                onClick = {

                                    sortOption =
                                        "Artan"
                                }

                            ) {

                                Text(
                                    "↑ Artan",
                                    fontSize = 12.sp
                                )
                            }


                            Button(

                                modifier =
                                    Modifier.weight(1f),

                                onClick = {

                                    sortOption =
                                        "Düşen"
                                }

                            ) {

                                Text(
                                    "↓ Düşen",
                                    fontSize = 12.sp
                                )
                            }
                        }


                        Spacer(
                            modifier =
                                Modifier.height(6.dp)
                        )


                        Row(

                            modifier =
                                Modifier.fillMaxWidth(),

                            horizontalArrangement =
                                Arrangement.spacedBy(6.dp)
                        ) {

                            Button(

                                modifier =
                                    Modifier.weight(1f),

                                onClick = {

                                    sortOption =
                                        "Alfabetik"
                                }

                            ) {

                                Text(
                                    "A-Z",
                                    fontSize = 12.sp
                                )
                            }


                            Button(

                                modifier =
                                    Modifier.weight(1f),

                                onClick = {

                                    sortOption =
                                        "Varsayılan"
                                }

                            ) {

                                Text(
                                    "Varsayılan",
                                    fontSize = 12.sp
                                )
                            }
                        }


                        Spacer(
                            modifier =
                                Modifier.height(8.dp)
                        )


                        // =================================================
                        // ARAMA
                        // =================================================

                        OutlinedTextField(

                            value =
                                searchText,

                            onValueChange = {

                                searchText =
                                    it
                            },

                            modifier =
                                Modifier.fillMaxWidth(),

                            label = {
                                Text("Hisse ara")
                            },

                            placeholder = {

                                Text(
                                    "THYAO, ASELS, GARAN..."
                                )
                            },

                            singleLine = true
                        )


                        Spacer(
                            modifier =
                                Modifier.height(8.dp)
                        )


                        // =================================================
                        // LİSTE
                        // =================================================

                        PullToRefreshBox(
                            isRefreshing = isRefreshing,
                            onRefresh = {
                                lifecycleScope.launch {
                                    refreshStocks()
                                }
                            },
                            state = pullToRefreshState,
                            modifier = Modifier.fillMaxSize()
                        ) {

                            LazyColumn(

                                modifier =
                                    Modifier.fillMaxSize()
                            ) {

                                items(
                                    items = filteredStocks,
                                    key = { it.sembol }
                                ) { stock ->


                                val analysis =
                                    analyzeStock(
                                        stock.degisimYuzde
                                    )


                                StockCard(

                                    code =
                                        stock.sembol,

                                    fiyat =
                                        stock.fiyat,

                                    oncekiKapanis =
                                        stock.oncekiKapanis,

                                    change =
                                        stock.degisimYuzde,

                                    currency =
                                        stock.paraBirimi,

                                    signal =
                                        analysis.signal,

                                    confidence =
                                        analysis.confidence,

                                    risk =
                                        analysis.risk
                                )
                            }
                        }
                    }
                    }
                }
            }
        }


        // =========================================================
        // İLK YÜKLEME + 30 SANİYELİK OTOMATİK YENİLEME
        // =========================================================

        startAutoRefresh()
    }


    // =========================================================
    // OTOMATİK CANLI YENİLEME
    // =========================================================

    private fun startAutoRefresh() {

        refreshJob?.cancel()

        refreshJob =
            lifecycleScope.launch {

                while (isActive) {

                    refreshStocks()

                    // Bir sonraki yenileme için 30 saniye bekle.
                    delay(30_000)
                }
            }
    }


    // =========================================================
    // CANLI FİYATLARI SERVER'DAN AL
    // =========================================================

    private suspend fun refreshStocks() {

        if (isRefreshing) {
            return
        }


        isRefreshing = true

        loadError = ""


        try {

            println(
                "================================================="
            )

            println(
                "YALCIN PRO - ANDROID CANLI YENILEME"
            )


            // =====================================================
            // SERVER'A BOŞ SYMBOL GÖNDERİYORUZ.
            // SERVER KENDİ AKTİF 614 HİSSESİNİ DÖNDÜRÜYOR.
            // =====================================================

            val response =
                RetrofitClient
                    .getBistStocksFromServer("")


            if (!response.success) {

                throw Exception(
                    "Server success=false"
                )
            }


            // =====================================================
            // TEMİZLE
            // =====================================================

            val newStocks =
                response.data

                    .filter {
                        it.sembol.isNotBlank()
                    }

                    .map {

                        it.copy(
                            sembol =
                                it.sembol
                                    .trim()
                                    .uppercase(
                                        Locale.ROOT
                                    )
                        )
                    }

                    .distinctBy {
                        normalizeSymbol(
                            it.sembol
                        )
                    }


            if (newStocks.isEmpty()) {

                throw Exception(
                    "Server fiyat verisi göndermedi."
                )
            }


            // =====================================================
            // EN ÖNEMLİ KISIM:
            // LİSTEYİ TEK SEFERDE DEĞİŞTİR.
            //
            // Böylece Compose kesin olarak yeniden çizilir.
            // =====================================================

            stocks =
                newStocks.toList()


            serverSymbolCount =
                newStocks.size


            // =====================================================
            // SON GÜNCELLEME ZAMANI
            // =====================================================

            val formatter =
                SimpleDateFormat(
                    "HH:mm:ss",
                    Locale.getDefault()
                )


            lastUpdateTime =
                formatter.format(
                    Date()
                )


            // =====================================================
            // KONTROL LOG'LARI
            // =====================================================

            val kontrolSembolleri =
                listOf(
                    "THYAO",
                    "VERUS",
                    "USHOL",
                    "YUNSA",
                    "AKBNK"
                )


            for (
            symbol in kontrolSembolleri
            ) {

                val stock =
                    newStocks.firstOrNull {

                        normalizeSymbol(
                            it.sembol
                        ) ==
                                normalizeSymbol(
                                    symbol
                                )
                    }


                if (stock != null) {

                    println(

                        "YALCIN PRO - CANLI FIYAT: " +
                                "${stock.sembol} = " +
                                "${stock.fiyat} | DEG = " +
                                "${stock.degisimYuzde}"
                    )
                }
            }


            println(
                "YALCIN PRO - SERVER'DAN GELEN: " +
                        "${newStocks.size} HISSE"
            )

            println(
                "YALCIN PRO - EKRANA AKTARILDI: " +
                        "${stocks.size} HISSE"
            )

            println(
                "YALCIN PRO - GUNCELLEME: " +
                        lastUpdateTime
            )

            println(
                "================================================="
            )


        } catch (e: Exception) {

            loadError =
                e.message
                    ?: "Bilinmeyen hata"


            println(
                "YALCIN PRO - CANLI YENILEME HATASI: $e"
            )

        } finally {

            isRefreshing =
                false
        }
    }


    override fun onDestroy() {

        refreshJob?.cancel()

        super.onDestroy()
    }
}


// =============================================================
// HİSSE KARTI
// =============================================================

@Composable
fun StockCard(

    code: String,

    fiyat: Double?,

    oncekiKapanis: Double?,

    change: Double?,

    currency: String,

    signal: String,

    confidence: Int,

    risk: String
) {

    val context =
        LocalContext.current


    // =========================================================
    // DEĞİŞİM RENGİ
    // =========================================================

    val changeColor =
        when {

            change == null -> {

                MaterialTheme
                    .colorScheme
                    .onSurfaceVariant
            }

            change > 0 -> {

                Color(
                    0xFF16803C
                )
            }

            change < 0 -> {

                Color(
                    0xFFD32F2F
                )
            }

            else -> {

                MaterialTheme
                    .colorScheme
                    .onSurfaceVariant
            }
        }


    // =========================================================
    // DEĞİŞİM ARKA PLANI
    // =========================================================

    val changeBackground =
        when {

            change == null -> {

                MaterialTheme
                    .colorScheme
                    .surfaceVariant
            }

            change > 0 -> {

                Color(
                    0xFFE8F5E9
                )
            }

            change < 0 -> {

                Color(
                    0xFFFFEBEE
                )
            }

            else -> {

                MaterialTheme
                    .colorScheme
                    .surfaceVariant
            }
        }


    // =========================================================
    // FİYAT
    // =========================================================

    val priceText =
        if (
            fiyat != null &&
            fiyat > 0.0
        ) {

            String.format(

                Locale.US,

                "%.2f %s",

                fiyat,

                currency
            )

        } else {

            "Veri bekleniyor"
        }


    // =========================================================
    // DEĞİŞİM
    // =========================================================

    val changeText =
        if (
            change != null
        ) {

            String.format(

                Locale.US,

                "%+.2f%%",

                change
            )

        } else {

            "--"
        }


    // =========================================================
    // KART
    // =========================================================

    Card(

        modifier =
            Modifier
                .fillMaxWidth()
                .padding(
                    vertical = 6.dp
                )
                .clickable {

                    val intent =
                        Intent(
                            context,
                            StockDetailActivity::class.java
                        )


                    intent.putExtra(
                        "stockName",
                        code
                    )


                    intent.putExtra(
                        "fiyat",
                        fiyat
                            ?: Double.NaN
                    )


                    intent.putExtra(
                        "oncekiKapanis",
                        oncekiKapanis
                            ?: Double.NaN
                    )


                    intent.putExtra(
                        "degisimYuzde",
                        change
                            ?: Double.NaN
                    )


                    intent.putExtra(
                        "paraBirimi",
                        currency
                    )


                    intent.putExtra(
                        "signal",
                        signal
                    )


                    intent.putExtra(
                        "confidence",
                        confidence
                    )


                    intent.putExtra(
                        "risk",
                        risk
                    )


                    context.startActivity(
                        intent
                    )
                },

        shape =
            RoundedCornerShape(
                18.dp
            ),

        elevation =
            CardDefaults
                .cardElevation(
                    defaultElevation = 3.dp
                )

    ) {

        Column(

            modifier =
                Modifier.padding(
                    16.dp
                )
        ) {

            Row(

                modifier =
                    Modifier.fillMaxWidth(),

                verticalAlignment =
                    Alignment.CenterVertically,

                horizontalArrangement =
                    Arrangement.SpaceBetween
            ) {

                Column(

                    modifier =
                        Modifier.weight(1f)
                ) {

                    Text(

                        text =
                            code,

                        fontWeight =
                            FontWeight.Bold,

                        fontSize =
                            22.sp
                    )


                    Spacer(
                        modifier =
                            Modifier.height(3.dp)
                    )


                    Text(

                        text =
                            "BIST",

                        fontSize =
                            12.sp,

                        color =
                            MaterialTheme
                                .colorScheme
                                .onSurfaceVariant
                    )
                }


                Column(

                    horizontalAlignment =
                        Alignment.End
                ) {

                    Text(

                        text =
                            priceText,

                        fontSize =
                            18.sp,

                        fontWeight =
                            FontWeight.Bold
                    )


                    Spacer(
                        modifier =
                            Modifier.height(4.dp)
                    )


                    Text(

                        text =
                            changeText,

                        modifier =
                            Modifier
                                .background(

                                    color =
                                        changeBackground,

                                    shape =
                                        RoundedCornerShape(
                                            8.dp
                                        )
                                )
                                .padding(

                                    horizontal =
                                        8.dp,

                                    vertical =
                                        4.dp
                                ),

                        color =
                            changeColor,

                        fontWeight =
                            FontWeight.Bold,

                        fontSize =
                            14.sp
                    )
                }
            }


            Spacer(
                modifier =
                    Modifier.height(14.dp)
            )


            Row(

                modifier =
                    Modifier.fillMaxWidth(),

                horizontalArrangement =
                    Arrangement.spacedBy(
                        8.dp
                    )
            ) {

                InfoBox(

                    title =
                        "SİNYAL",

                    value =
                        signal,

                    modifier =
                        Modifier.weight(1f)
                )


                InfoBox(

                    title =
                        "GÜVEN",

                    value =
                        "$confidence /100",

                    modifier =
                        Modifier.weight(1f)
                )


                InfoBox(

                    title =
                        "RİSK",

                    value =
                        risk,

                    modifier =
                        Modifier.weight(1f)
                )
            }
        }
    }
}


// =============================================================
// BİLGİ KUTUSU
// =============================================================

@Composable
fun InfoBox(

    title: String,

    value: String,

    modifier: Modifier =
        Modifier
) {

    Column(

        modifier =
            modifier
                .background(

                    color =
                        MaterialTheme
                            .colorScheme
                            .surfaceVariant,

                    shape =
                        RoundedCornerShape(
                            10.dp
                        )
                )
                .padding(
                    8.dp
                )
    ) {

        Text(

            text =
                title,

            fontSize =
                10.sp,

            fontWeight =
                FontWeight.Bold,

            color =
                MaterialTheme
                    .colorScheme
                    .onSurfaceVariant
        )


        Spacer(
            modifier =
                Modifier.height(2.dp)
        )


        Text(

            text =
                value,

            fontSize =
                12.sp,

            fontWeight =
                FontWeight.Bold
        )
    }
}
```
