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

def get_htf_analysis(symbol):
    candles = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
    if not candles: return None, None, False
    df_htf = pd.DataFrame(candles, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1]
    p_highs, p_lows = find_custom_sr(df_htf)
    curr_c = float(df_htf['c'].iloc[-1])
    res_4h = min([h for h in p_highs if h > curr_c]) if any(h > curr_c for h in p_highs) else max(df_htf['h'].astype(float))
    sup_4h = max([l for l in p_lows if l < curr_c]) if any(l < curr_c for l in p_lows) else min(df_htf['l'].astype(float))
    is_htf_break = curr_c > p_highs[-1] if p_highs else False
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
                if htf_break: continue # 4H Direnç kırıldıysa Short yok

                p_highs, p_lows = find_custom_sr(df)
                if not p_highs: continue
                last_res_1h = p_highs[-1]
                if df['c'].iloc[-1] > last_res_1h: continue 

                # PARA AKIŞI VERİSİ (USDT BAZLI)
                ratio_res = get_data("/api/v5/rubik/stat/taker-volume", {"instId": symbol, "period": "1H"})
                buy_vol_usdt = 0; sell_vol_usdt = 0; ratio = 1.0
                alert_status = "🛡️ *DİRENÇ ALTI SEYİR*"; note = "Fiyat direnç altında."
                
                if ratio_res:
                    buy_vol_usdt = float(ratio_res[0][1]) # USDT cinsinden alım
                    sell_vol_usdt = float(ratio_res[0][2]) # USDT cinsinden satış
                    ratio = round(buy_vol_usdt / sell_vol_usdt, 2) if sell_vol_usdt > 0 else 1.0
                    
                    if ratio > 1.35:
                        alert_status = "🚫 *SAKIN EKLEME YAPMA!*"; note = "Alıcılar agresif, direnç kırılabilir."
                    elif ratio < 0.85:
                        alert_status = "📉 *SATICILAR GELDİ / SHORT VAKTİ*"; note = "Satış hacmi alışı geçti, direnç korunuyor."

                tv_link = f"https://www.tradingview.com/chart/?symbol=OKX:{symbol.replace('-USDT-SWAP', 'USDTPERP')}"

                # MESAJ TASARIMI (USDT Hacimli)
                msg = (f"{alert_status}\n\n"
                       f"🚨 *SİNYAL: {symbol}*\n"
                       f"━━━━━━━━━━━━━━━\n"
                       f"📊 RSI: `{round(rsi, 1)}` | 📈 24s: `%{round(change, 1)}` \n"
                       f"🛒 *1H PARA AKIŞI (USDT):*\n"
                       f"🟢 Alış: `${round(buy_vol_usdt/1000, 1)}K` \n"
                       f"🔴 Satış: `${round(sell_vol_usdt/1000, 1)}K` \n"
                       f"⚖️ Oran: `{ratio}`\n\n"
                       f"📍 *1H DİRENÇ:* `{last_res_1h}`\n"
                       f"🏛️ *4H DİRENÇ:* `{res_4h}`\n\n"
                       f"📝 *NOT:* {note}\n"
                       f"━━━━━━━━━━━━━━━\n"
                       f"🔗 [Grafiği Aç]({tv_link})")
                
                send_telegram(msg)

if __name__ == "__main__":
    scan()
