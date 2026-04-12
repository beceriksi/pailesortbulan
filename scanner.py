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
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            p_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            p_lows.append(lows[i])
    return p_highs, p_lows

def get_htf_analysis(symbol):
    candles = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
    if not candles: return None, None, False
    df_htf = pd.DataFrame(candles, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1]
    p_highs, p_lows = find_custom_sr(df_htf)
    curr_c = float(df_htf['c'].iloc[-1])
    
    # 4H Seviyeleri
    res_4h = min([h for h in p_highs if h > curr_c]) if any(h > curr_c for h in p_highs) else (p_highs[-1] if p_highs else 0)
    sup_4h = max([l for l in p_lows if l < curr_c]) if any(l < curr_c for l in p_lows) else (p_lows[-1] if p_lows else 0)
    
    # 4H Kırılım Kontrolü (Sadece Uyarı İçin)
    is_htf_break = False
    if p_highs and curr_c > p_highs[-1]:
        is_htf_break = True
        
    return res_4h, sup_4h, is_htf_break

def scan():
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
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
            
            # RSI
            delta = df['c'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean(); loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = 100 - (100 / (1 + gain / loss)).iloc[-1]
            
            if rsi > RSI_LIMIT:
                res_4h, sup_4h, htf_break = get_htf_analysis(symbol)
                
                # 1H Pivotlar
                p_highs, p_lows = find_custom_sr(df)
                if not p_highs: continue
                last_res_1h = p_highs[-1]

                # PARA AKIŞI
                ratio_res = get_data("/api/v5/rubik/stat/taker-volume", {"instId": symbol, "period": "1H"})
                buy_v = 0; sell_v = 0; ratio = 1.0
                if ratio_res:
                    buy_v = float(ratio_res[0][1]); sell_v = float(ratio_res[0][2])
                    ratio = round(buy_v / sell_v, 2) if sell_v > 0 else 1.0

                # DİRENÇ DURUMU VE NOT BELİRLEME
                alert_header = ""
                if htf_break:
                    alert_header = "🔥 *DİRENÇ KIRILDI! RİSK ÇOK YÜKSEK*"
                    note = "4H ana direnci üzerinde kapanış geldi, trend çok güçlü!"
                elif ratio < 0.85:
                    alert_header = "📉 *SATICILAR GELDİ / SHORT VAKTİ*"
                    note = "Direnç korunuyor, hacimli satışlar başladı."
                elif ratio > 1.35:
                    alert_header = "🚫 *SAKIN EKLEME YAPMA!*"
                    note = "Alıcılar çok agresif, direnç zorlanıyor."
                else:
                    alert_header = "🛡️ *DİRENÇ ALTI SEYİR*"
                    note = "Fiyat direnç altında oyalanıyor."

                tv_link = f"https://www.tradingview.com/chart/?symbol=OKX:{symbol.replace('-USDT-SWAP', 'USDTPERP')}"

                msg = (f"{alert_header}\n\n"
                       f"🚨 *SİNYAL: {symbol}*\n"
                       f"━━━━━━━━━━━━━━━\n"
                       f"📊 RSI: `{round(rsi, 1)}` | 📈 24s: `%{round(change, 1)}` \n\n"
                       f"🛒 *1H PARA AKIŞI (USDT):*\n"
                       f"🟢 Alış: `${round(buy_v/1000, 1)}K` \n"
                       f"🔴 Satış: `${round(sell_v/1000, 1)}K` \n"
                       f"⚖️ Oran: `{ratio}`\n\n"
                       f"📍 *1H DİRENÇ:* `{last_res_1h}`\n"
                       f"🏛️ *4H DİRENÇ:* `{res_4h}`\n\n"
                       f"📝 *NOT:* {note}\n"
                       f"━━━━━━━━━━━━━━━\n"
                       f"🔗 [Grafiği Aç]({tv_link})")
                
                send_telegram(msg)

if __name__ == "__main__":
    scan()
