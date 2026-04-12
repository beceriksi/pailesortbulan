import requests
import pandas as pd
import numpy as np
import os

# GitHub Secrets
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ANA AYARLAR
RSI_LIMIT = 70
CHANGE_24H_LIMIT = 8

def send_telegram(msg):
    if TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True})
        except: pass

def get_data(endpoint, params={}):
    base = "https://www.okx.com"
    try:
        res = requests.get(base + endpoint, params=params, timeout=10).json()
        return res.get('data', [])
    except: return []

def find_custom_sr(df, pivot_len=2):
    highs = df['h'].astype(float).values
    lows = df['l'].astype(float).values
    p_highs, p_lows = [], []
    for i in range(pivot_len, len(highs) - pivot_len):
        if all(highs[i] > highs[i-j] for j in range(1, pivot_len + 1)) and all(highs[i] > highs[i+j] for j in range(1, pivot_len + 1)):
            p_highs.append(highs[i])
        if all(lows[i] < lows[i-j] for j in range(1, pivot_len + 1)) and all(lows[i] < lows[i+j] for j in range(1, pivot_len + 1)):
            p_lows.append(lows[i])
    return p_highs, p_lows

def scan():
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    tickers = sorted(tickers, key=lambda x: float(x['vol24h']), reverse=True)
    
    for t in tickers:
        symbol = t['instId']
        if "-USDT-" not in symbol: continue
        
        last_price = float(t['last'])
        change = (last_price / float(t['open24h']) - 1) * 100
        
        if change > CHANGE_24H_LIMIT:
            candles_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "100"})
            if not candles_1h: continue
            
            df = pd.DataFrame(candles_1h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
            df['c'] = df['c'].astype(float)
            df['h'] = df['h'].astype(float)

            # RSI Hesaplama
            delta = df['c'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = 100 - (100 / (1 + gain / loss)).iloc[-1]
            
            if rsi > RSI_LIMIT:
                if last_price >= df['h'].max(): continue # ATH Koruması

                p_highs, p_lows = find_custom_sr(df)
                if not p_highs: continue
                last_res_1h = p_highs[-1]

                # Yeni Kırılım Filtresi
                if df['c'].iloc[-1] > last_res_1h: continue 

                # Alıcı/Satıcı Oranı ve Yorum Mantığı
                ratio_res = get_data("/api/v5/rubik/stat/taker-volume", {"instId": symbol, "period": "1H"})
                ratio = 1.0
                alert_status = ""
                
                if ratio_res:
                    b_vol, s_vol = float(ratio_res[0][1]), float(ratio_res[0][2])
                    ratio = round(b_vol / s_vol, 2) if s_vol > 0 else 1.0
                    
                    if ratio > 1.30:
                        alert_status = "🚫 *SAKIN EKLEME YAPMA!* (Alıcılar %30+ Baskın)\n"
                    elif ratio < 0.70:
                        alert_status = "📉 *SATICILAR BASKIN:* Trend dönüşü beklenebilir.\n"
                    else:
                        alert_status = "⚖️ *DENGELİ AKIŞ:* Risk iştahı stabil.\n"

                funding_res = get_data("/api/v5/public/funding-rate", {"instId": symbol})
                funding = funding_res[0]['fundingRate'] if funding_res else "0"
                tv_link = f"https://www.tradingview.com/chart/?symbol=OKX:{symbol.replace('-USDT-SWAP', 'USDTPERP')}"

                msg = (f"{alert_status}\n"
                       f"🚨 *SİNYAL: {symbol}*\n"
                       f"━━━━━━━━━━━━━━━\n"
                       f"📊 RSI: `{round(rsi, 1)}` | 📈 24s: `%{round(change, 1)}` \n"
                       f"🏦 Funding: `{funding}` | 🛒 Oran: `{ratio}`\n\n"
                       f"📍 *1H DİRENÇ (Pivot 2-2):* `{last_res_1h}`\n"
                       f"✅ *1H DESTEK (Pivot 2-2):* `{p_lows[-1] if p_lows else 'Yok'}`\n\n"
                       f"🔗 [Grafiği Aç]({tv_link})")
                
                send_telegram(msg)

if __name__ == "__main__":
    scan()
