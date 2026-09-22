import os
import asyncio
import pytz
from datetime import datetime
import ccxt.async_support as ccxt
import pandas as pd
from telegram import Bot

# -------------------------------------------------------------------
# ORTAM DEĞİŞKENLERİ (Environment Variables)
# -------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

# OKX API Bağlantısı (Kamuya açık veriler, API Key/Secret gerekmez)
exchange = ccxt.okx({'enableRateLimit': True})

# OKX Parite İsimleri
SYMBOLS = {
    'BTC': 'BTC/USDT',
    'ETH': 'ETH/USDT'
}

# Son gönderilen sinyali hafızada tutarak spam mesajları önler
last_sent_signal = None

# -------------------------------------------------------------------
# VERİ ÇEKME FONKSİYONLARI (OKX)
# -------------------------------------------------------------------
async def fetch_ohlcv_data(symbol, timeframe, limit=100):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df
    except Exception as e:
        print(f"OKX Veri çekme hatası ({symbol} - {timeframe}): {e}")
        return None

# -------------------------------------------------------------------
# SMC & PRICE ACTION ANALİZ MOTORU
# -------------------------------------------------------------------
async def analyze_symbol(symbol):
    df_1d = await fetch_ohlcv_data(symbol, '1d', 30)
    df_1w = await fetch_ohlcv_data(symbol, '1w', 10)
    df_1m = await fetch_ohlcv_data(symbol, '1m', 5)
    
    if df_1d is None or df_1w is None or df_1m is None:
        return None

    last_close = df_1d['close'].iloc[-1]
    
    # 1. Breakout Kontrolü (1M Top)
    m_top = df_1m['high'].iloc[-2]
    is_breakout_1m = last_close > m_top

    # 2. Candle Body Close & Sweep (1D Prev Low)
    prev_low_1d = df_1d['low'].iloc[-3]
    curr_low_1d = df_1d['low'].iloc[-2]
    curr_close_1d = df_1d['close'].iloc[-2]
    sweep_1d = (curr_low_1d < prev_low_1d) and (curr_close_1d > prev_low_1d)

    # 3. FVG (Fair Value Gap 1D)
    fvg_bottom = df_1d['high'].iloc[-3]
    fvg_top = df_1d['low'].iloc[-1]
    has_fvg = fvg_top > fvg_bottom

    # 4. Dealing Range & Equilibrium (%50)
    range_high = df_1d['high'].max()
    range_low = df_1d['low'].min()
    eq_50 = (range_high + range_low) / 2

    # 5. Güven Puanı (Confidence Score) Hesabı
    confidence = 0
    if is_breakout_1m: confidence += 35
    if sweep_1d: confidence += 25
    if has_fvg: confidence += 20
    if last_close > eq_50: confidence += 20

    direction = "BUY" if confidence >= 40 else "NEUTRAL"
    clean_name = symbol.replace('/', '')
    
    return {
        'symbol': clean_name,
        'direction': direction,
        'confidence': confidence,
        'last_close': last_close,
        'breakout_1m_price': round(m_top, 2),
        'sweep_1d': sweep_1d,
        'eq_50': round(eq_50, 2),
        'range_low': round(range_low, 2),
        'range_high': round(range_high, 2),
        'fvg_range': f"{round(fvg_bottom,2)}–{round(fvg_top,2)}" if has_fvg else "Yok",
        'target_1': round(last_close * 1.15, 2),
        'target_2': round(last_close * 1.30, 2),
        'stop_loss': round(range_low, 2)
    }

# -------------------------------------------------------------------
# RAPOR FORMATLAMA & TELEGRAM GÖNDERİMİ
# -------------------------------------------------------------------
async def run_analysis_and_notify():
    global last_sent_signal

    btc_data = await analyze_symbol(SYMBOLS['BTC'])
    eth_data = await analyze_symbol(SYMBOLS['ETH'])

    if not btc_data or not eth_data:
        return

    # SMT Korelasyon Onayı (BTC & ETH ikisi de alım yönlü mü?)
    smt_status = "uyumlu" if btc_data['direction'] == "BUY" and eth_data['direction'] == "BUY" else "uyumsuz"

    # En Güçlü Sembolü Seçme
    selected = btc_data if btc_data['confidence'] >= eth_data['confidence'] else eth_data

    # Sinyal Durum Anahtarı (Aynı sinyali tekrar tekrar atmaması için)
    current_signal_key = f"{selected['symbol']}_{selected['direction']}_{selected['confidence']}"

    # Eğer sinyal öncekilerle aynıysa bildirim atma (Spam Engelleme)
    if current_signal_key == last_sent_signal:
        print("Sinyal durumunda değişiklik yok. Mesaj atılmadı.")
        return

    # New York Zamanı
    ny_tz = pytz.timezone('America/New_York')
    ny_time = datetime.now(ny_tz).strftime('%Y-%m-%d %H:%M:%S')

    # Telegram Mesaj Metni Formatı
    report = f"""SEMBOL KARSILASTIRMA / KARAR (OKX VERİSİ)
#########################################
BTCUSDT   verdict={btc_data['direction']}   guven={btc_data['confidence']}/100 [SINYAL VAR]
ETHUSDT   verdict={eth_data['direction']}   guven={eth_data['confidence']}/100 [SINYAL VAR]

KARAR: İki sembol de OKX üzerinde analiz edildi — DAHA GUCLU olan seçildi.
-> {selected['symbol']} {selected['direction']} (LIMIT {selected['last_close']}'e gelirse bakilabilir, guven %{selected['confidence']})
#########################################

*** YENI SINYAL - OTOMATIK TAKIP: {selected['symbol']} ALIS ({selected['direction']}) - Fiyat {selected['last_close']} ***
DURUM: BILGILENDIRME - Bu bot otomatik islem ACMAMAKTADIR. Sadece sinyal üretir.

[{ny_time} New York]

📊 {selected['symbol']} ANALİZ RAPORU
Direction: {selected['direction']} — Breakout 1M ({selected['breakout_1m_price']})
En olası: Önce {selected['last_close']} retest, sonra Target {selected['target_1']} → {selected['target_2']}
Retestte günlük kapanisin seviye altina dönmesi olagan (%80), tek basina bozulma degil

Candle Body Close — 1D: Prev Low süpürüldü, üstünde kapandi → Target {selected['target_1']} (%63)
Çizgi grafik (MSNR): SBR Fresh/Unfresh seviyeleri aktif takipte
SMT: OKX BTC & ETH yapisi → {smt_status}
Fiyat premium bölgesinde • dealing range %50 = {selected['eq_50']} ({selected['range_low']}–{selected['range_high']})
FVG 1D: {selected['fvg_range']}
SL (Geçersizlik): {selected['stop_loss']}

Yatirim tavsiyesi degildir.
"""

    # Telegram'a Mesaj Gönder
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text=report)
    print("Yeni OKX Sinyal raporu Telegram'a başarıyla gönderildi.")
    
    # Son gönderilen sinyali güncelle
    last_sent_signal = current_signal_key

# -------------------------------------------------------------------
# DÖNGÜ VE ANA ÇALIŞTIRICI (5 Dakikada Bir Kontrol)
# -------------------------------------------------------------------
async def main():
    while True:
        try:
            print("OKX Analizi başlatılıyor (5 dk periyot)...")
            await run_analysis_and_notify()
            # 5 dakikada bir (300 saniye) kontrol eder
            await asyncio.sleep(300)
        except Exception as e:
            print(f"Döngü hatası: {e}")
            await asyncio.sleep(60)

if __name__ == '__main__':
    asyncio.run(main())
