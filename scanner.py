import requests
import pandas as pd
import numpy as np
import os

# GitHub Secrets'tan alınacak bilgiler
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def send_telegram(msg):
    if TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
        except Exception as e: print(f"Bağlantı Hatası: {e}")

def get_data(endpoint, params={}):
    base = "https://www.okx.com"
    try:
        res = requests.get(base + endpoint, params=params, timeout=10).json()
        return res.get('data', [])
    except: return []

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def find_structural_supply(df):
    for i in range(len(df) - 3, 15, -1):
        if df['c'].iloc[i+1] < df['l'].iloc[i] * 0.985:
            return df['h'].iloc[i], df['l'].iloc[i]
    return None, None

def scan():
    print("🛡️ Rafine PA Taraması Başlatıldı...")
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    tickers = sorted(tickers, key=lambda x: float(x['vol24h']), reverse=True)[:40]

    for t in tickers:
        symbol = t['instId']
        if "-USDT-" not in symbol: continue
        
        candles_4h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "50"})
        if len(candles_4h) < 20: continue
        df_4h = pd.DataFrame(candles_4h, columns=['ts','o','h','l','c','v','vc','vq','conf'])
        rsi_4h = calculate_rsi(df_4h['c'].astype(float)[::-1]).iloc[-1]

        if rsi_4h < 70: continue

        candles_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "200"})
        df = pd.DataFrame(candles_1h, columns=['ts','o','h','l','c','v','vc','vq','conf'])
        df[['o','h','l','c','v']] = df[['o','h','l','c','v']].astype(float)
        df_1h = df.iloc[::-1].reset_index(drop=True)

        s_top, s_bottom = find_structural_supply(df_1h)
        if not s_top: continue
        
        curr_price = df_1h['c'].iloc[-1]
        in_zone = curr_price >= s_bottom * 0.995 and curr_price <= s_top * 1.02
        
        rsi_series = calculate_rsi(df_1h['c'])
        rsi_now = rsi_series.iloc[-1]
        rsi_prev_max = rsi_series.iloc[-6:-1].max()
        price_prev_max = df_1h['h'].iloc[-6:-1].max()

        is_divergence = (curr_price >= price_prev_max) and (rsi_now < rsi_prev_max)
        upper_wick = df_1h['h'].iloc[-1] - max(df_1h['c'].iloc[-1], df_1h['o'].iloc[-1])
        body = abs(df_1h['c'].iloc[-1] - df_1h['o'].iloc[-1])
        is_rejected = upper_wick > (body * 1.3)

        if in_zone and is_divergence and is_rejected:
            msg = (f"🛡️ *[RAFİNE PA SİNYALİ]* 🛡️\n"
                   f"━━━━━━━━━━━━━━━\n"
                   f"💎 *Parite:* {symbol}\n"
                   f"📦 *Kutu Aralığı:* {round(s_bottom,5)} - {round(s_top,5)}\n"
                   f"📊 *4H RSI:* {round(rsi_4h,1)}\n"
                   f"📉 *1H Uyumsuzluk:* Mevcut ✅\n"
                   f"🕯️ *Mum Reddi:* İğne Var ✅\n"
                   f"💰 *Güncel Fiyat:* {curr_price}\n"
                   f"━━━━━━━━━━━━━━━\n"
                   f"📍 *Not:* Fiyat yapısal bölgeye çarptı.")
            send_telegram(msg)

if __name__ == "__main__":
    scan()
