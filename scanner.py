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
    """Hacim uyumsuzluğunu bulur ve yönü tayin eder."""
    prices = df['c'].astype(float).iloc[-5:].values
    volumes = df['v'].astype(float).iloc[-5:].values
    
    # Ayı Uyumsuzluğu (Fiyat ↑, Hacim ↓) -> SHORT DESTEKLER
    if prices[-1] > prices[0] and volumes[-1] < volumes[-2]:
        return "⚠️ *HACİM UYUMSUZLUĞU:* 🐻 (Short Destekli)"
    
    # Boğa Uyumsuzluğu (Fiyat ↓, Hacim ↑) -> LONG DESTEKLER (Bot genelde RSI>70 baktığı için nadir çıkar)
    if prices[-1] < prices[0] and volumes[-1] > volumes[-2]:
        return "⚠️ *HACİM UYUMSUZLUĞU:* 🐂 (Alım Baskısı)"
        
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
            df = pd.DataFrame(candles_1h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
            df['c'] = df['c'].astype(float)
            
            # RSI
            delta = df['c'].diff(); gain = (delta.where(delta > 0, 0)).rolling(14).mean(); loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi_val = 100 - (100 / (1 + gain / loss)).iloc[-1]
            
            if rsi_val > RSI_LIMIT:
                candles_4h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
                df_htf = pd.DataFrame(candles_4h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1]
                p_highs_4h, _ = find_custom_sr(df_htf)
                curr_c = float(df_htf['c'].iloc[-1])
                res_4h = min([h for h in p_highs_4h if h > curr_c]) if any(h > curr_c for h in p_highs_4h) else (p_highs_4h[-1] if p_highs_4h else 0)

                buy_v, sell_v, ratio = get_smart_volume(symbol, df)
                vol_div = check_volume_divergence(df)

                # Yön Belirleme (Robotik Yorum)
                if ratio < 0.85: direction = "📉 *SHORT FIRSATI*"
                elif ratio > 1.35: direction = "🚫 *ZİRVEDE ALICI (TEHLİKE)*"
                else: direction = "🛡️ *DİRENÇ GÖZLEMİ*"

                div_msg = f"\n{vol_div}" if vol_div else ""

                signal_msg = (f"{direction} | *{symbol}*\n"
                              f"📊 RSI: `{round(rsi_val, 1)}` | 📈 24s: `%{round(change, 1)}` \n"
                              f"⚖️ Oran: `{ratio}` | 🏛️ 4H Direnç: `{res_4h}`{div_msg}\n")
                
                all_signals.append(signal_msg)

    if all_signals:
        header = "🔍 *BURSA TARAMA RAPORU* 🔍\n━━━━━━━━━━━━━━━\n"
        final_report = header + "\n".join(all_signals)
        send_telegram(final_report)

if __name__ == "__main__":
    scan()
