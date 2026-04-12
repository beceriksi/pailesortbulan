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
    if TOKEN and CHAT_ID and msg:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": CHAT_ID, 
                "text": msg, 
                "parse_mode": "Markdown", 
                "disable_web_page_preview": True
            })
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

def get_smart_volume(symbol, df_1h):
    res = get_data("/api/v5/rubik/stat/taker-volume", {"instId": symbol, "period": "1H"})
    buy_v = 0; sell_v = 0
    if res and len(res) > 0 and float(res[0][1]) > 0:
        buy_v = float(res[0][1]); sell_v = float(res[0][2])
    else:
        last = df_1h.iloc[-1]
        c, o, h, l, v = float(last['c']), float(last['o']), float(last['h']), float(last['l']), float(last['v'])
        body = abs(c - o); total_range = (h - l) if (h - l) > 0 else 0.0001
        if c > o: buy_v = v * (0.5 + (body / total_range) * 0.5); sell_v = v - buy_v
        else: sell_v = v * (0.5 + (body / total_range) * 0.5); buy_v = v - sell_v
    ratio = round(buy_v / sell_v, 2) if sell_v > 0 else 1.0
    return buy_v, sell_v, ratio

def check_volume_divergence(df):
    prices = df['c'].astype(float).iloc[-5:].values
    volumes = df['v'].astype(float).iloc[-5:].values
    if prices[-1] > prices[0] and volumes[-1] < volumes[-2]:
        return "⚠️ *HACİM UYUMSUZLUĞU:* 🐻 (Short Destekli)"
    return ""

def scan():
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    all_signals = []
    
    for t in tickers:
        symbol = t['instId']
        if "-USDT-" not in symbol: continue
        last_price = float(t['last'])
        change = (last_price / float(t['open24h']) - 1) * 100
        
        if change > CHANGE_24H_LIMIT:
            candles_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "100"})
            if not candles_1h: continue
            df_1h = pd.DataFrame(candles_1h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
            df_1h['c'] = df_1h['c'].astype(float)
            
            # RSI
            delta = df_1h['c'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean(); loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi_val = 100 - (100 / (1 + gain / loss)).iloc[-1]
            
            if rsi_val > RSI_LIMIT:
                # 4H Analiz
                candles_4h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
                df_4h = pd.DataFrame(candles_4h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1]
                p_highs_4h, p_lows_4h = find_custom_sr(df_4h)
                res_4h = min([h for h in p_highs_4h if h > last_price]) if any(h > last_price for h in p_highs_4h) else (p_highs_4h[-1] if p_highs_4h else 0)
                
                # 1H Analiz
                p_highs_1h, p_lows_1h = find_custom_sr(df_1h)
                res_1h = min([h for h in p_highs_1h if h > last_price]) if any(h > last_price for h in p_highs_1h) else (p_highs_1h[-1] if p_highs_1h else 0)
                sup_1h = max([l for l in p_lows_1h if l < last_price]) if any(l < last_price for l in p_lows_1h) else (p_lows_1h[-1] if p_lows_1h else 0)

                buy_v, sell_v, ratio = get_smart_volume(symbol, df_1h)
                vol_div = check_volume_divergence(df_1h)

                # DİRENÇ VE SATIŞ YORUMU
                status_note = ""
                if last_price > res_4h and res_4h != 0:
                    status_note = "🔥 *DİRENÇ ÜSTÜ:* 4H Ana direnç kırıldı, riskli!"
                elif (res_1h - last_price) / last_price < 0.005: # %0.5 yakınlık
                    status_note = "🚨 *DİRENÇTE:* Dirençten satış yemeye çalışıyor."
                elif ratio < 0.85:
                    status_note = "📉 *SATIŞ BASKISI:* Direnç altı hacimli satışlar var."
                elif ratio > 1.35:
                    status_note = "🚫 *SAKIN EKLEME:* Alıcılar hala çok agresif."
                else:
                    status_note = "🛡️ *DİRENÇ ALTI:* Kararsız bölge, hacim bekle."

                div_msg = f"\n{vol_div}" if vol_div else ""

                signal_msg = (f"*{symbol}* | RSI: `{round(rsi_val, 1)}` | %{round(change, 1)}\n"
                              f"{status_note}\n"
                              f"🛒 Oran: `{ratio}` | 🏛️ 4H: `{res_4h}` | 📍 1H: `{res_1h}`{div_msg}\n"
                              f"━━━━━━━━━━━━━━━")
                
                all_signals.append(signal_msg)

    if all_signals:
        header = "🔍 *BURSA PİYASA ANALİZİ* 🔍\n━━━━━━━━━━━━━━━\n"
        send_telegram(header + "\n".join(all_signals))

if __name__ == "__main__":
    scan()
