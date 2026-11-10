import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify

app = Flask(__name__)


# ============================================================
# YALCIN PRO - 614 BIST HISSE
# Yahoo Finance Chart API tabanli, cache'li sunucu
#
# DUZELTILENLER:
# - /all eski cache'i sonsuza kadar dondurmuyor
# - Cache belirlenen sureyi gecince otomatik yenileniyor
# - /refresh zorunlu yeni veri cekiyor
# - Arka plan otomatik yenileme devam ediyor
# - Fiyat ve degisim yuzdesi yeni verilerle guncelleniyor
# ============================================================


# ============================================================
# AYARLAR
# ============================================================

TARGET = 0  # 0 = sembol sayisini sinirlama

SYMBOL_FILE = "yalcin_pro_active_symbols.json"

# ============================================================
# OTOMATIK BIST SEMBOL KAYNAGI
# ============================================================
# KAP kaynakli, topluluk tarafindan güncellenen BIST sembol listesi.
# Sunucu belirli araliklarla bu listeyi kontrol eder.
SYMBOL_SOURCE_URL = (
    "https://raw.githubusercontent.com/ahmeterenodaci/"
    "Istanbul-Stock-Exchange--BIST--including-symbols-and-logos/"
    "main/bist.json"
)

# RESMI KAP KAYNAGI
KAP_COMPANIES_URL = "https://www.kap.org.tr/tr/bist-sirketler"

# KAP cevabi bazen HTML icinde JSON olarak gelir.
# Bu nedenle hem stockCode alanlarini hem de sirket tablosundaki kodlari okuyoruz.
# KAP gecici olarak bozuk/eksik cevap verirse mevcut liste SILINMEZ.
MIN_VALID_ONLINE_SYMBOLS = 400

# Yeni sembol listesini 1 saatte bir kontrol et.
# Boylece yeni hisse / sembol degisikligi gun icinde otomatik yakalanir.
SYMBOL_SOURCE_REFRESH_SECONDS = 60 * 60

last_symbol_source_check = 0


# Yahoo'yu gereksiz yere hizli bombardimana tutmamak icin
# kontrollu paralellik.
WORKERS = 6

# Yahoo istek timeout
REQUEST_TIMEOUT = 15

# Her hisse icin tekrar deneme
RETRY_COUNT = 2

# Cache kac saniyede bir yenilensin?
#
# 60 saniye:
# - Normal otomatik yenileme
# - Yahoo'ya asiri istek gondermez
#
# Yenile butonu ise bunu beklemez ve force=True ile
# aninda yeni veri ister.
REFRESH_SECONDS = 60


# ============================================================
# YAHOO FINANCE
# ============================================================

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}.IS"


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()
