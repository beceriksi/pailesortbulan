import requests
import pandas as pd
import numpy as np
import os

# GitHub Secrets
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# ANA STRATEJİ AYARLARI
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
                "disable_web_page_preview": False # Link önizlemesi için False yapıldı
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
        if lows[i] < lows[i-1] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            p_lows.append(lows[i])
    return p_highs, p_lows

def get_smart_volume(symbol, df_1h):
    res = get_data("/api/v5/rubik/stat/taker-volume", {"instId": symbol, "period": "1H"})
    buy_v, sell_v = 0, 0
    if res and len(res) > 0 and float(res[0][1]) > 0:
        buy_v = float(res[0][1]); sell_v = float(res[0][2])
    else:
        last = df_1h.iloc[-1]
        c, o, h, l, v = float(last['c']), float(last['o']), float(last['h']), float(last['l']), float(last['v'])
        body = abs(c - o); total_range = (h - l) if (h - l) > 0 else 0.0001
        if c > o: buy_v = v * (0.5 + (body / total_range) * 0.5); sell_v = v - buy_v
        else: sell_v = v * (0.5 + (body / total_range) * 0.5); buy_v = v - sell_v
    return buy_v, sell_v, round(buy_v / sell_v, 2) if sell_v > 0 else 1.0

def check_volume_divergence(df):
    p = df['c'].astype(float).iloc[-5:].values
    v = df['v'].astype(float).iloc[-5:].values
    if p[-1] > p[0] and v[-1] < v[-2]: return "⚠️ *AYI UYUMSUZLUĞU (Düşüş Beklentisi)*"
    return ""

def scan():
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    all_signals = []
    
    for t in tickers:
        symbol = t['instId']
        if "-USDT-" not in symbol: continue
        last_p = float(t['last'])
        chg = (last_p / float(t['open24h']) - 1) * 100
        
        if chg > CHANGE_24H_LIMIT:
            c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "100"})
            if not c_1h: continue
            df_1h = pd.DataFrame(c_1h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
            df_1h['c'] = df_1h['c'].astype(float)
            
            delta = df_1h['c'].diff(); g = (delta.where(delta > 0, 0)).rolling(14).mean(); l = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = 100 - (100 / (1 + g / l)).iloc[-1]
            
            if rsi > RSI_LIMIT:
                # Seviye Analizleri
                c_4h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
                df_4h = pd.DataFrame(c_4h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1]
                ph_4h, _ = find_custom_sr(df_4h)
                res_4h = min([x for x in ph_4h if x > last_p]) if any(x > last_p for x in ph_4h) else (ph_4h[-1] if ph_4h else 0)
                
                ph_1h, _ = find_custom_sr(df_1h)
                res_1h = min([x for x in ph_1h if x > last_p]) if any(x > last_p for x in ph_1h) else (ph_1h[-1] if ph_1h else 0)

                bv, sv, ratio = get_smart_volume(symbol, df_1h)
                div = check_volume_divergence(df_1h)

                # TradingView Link Oluşturma (Perpetual Swap Formatı)
                # Örnek: BTC-USDT-SWAP -> OKX:BTCUSDT.P
                tv_clean = symbol.replace('-SWAP', '').replace('-', '')
                tv_link = f"https://www.tradingview.com/chart/?symbol=OKX:{tv_clean}.P"

                # STRATEJİK YORUM
                if last_p > res_4h and res_4h != 0: note = "🔥 *KIRILIM:* 4H Direnç üstü kapanış, tehlikeli!"
                elif (res_1h - last_p) / last_p < 0.006: note = "🚨 *DİRENÇTE:* Fiyat dirençten satış yemeye çalışıyor."
                elif ratio < 0.85: note = "📉 *SATIŞ BASKISI:* Direnç altı hacimli satışlar başladı."
                elif ratio > 1.35: note = "🚫 *ZİRVEDE ALICI:* FOMO var, ekleme yapmak riskli."
                else: note = "🛡️ *GÖZLEM:* Direnç altı kararsız yapı."

                div_msg = f"\n{div}" if div else ""

                all_signals.append(f"*{symbol}* | RSI: `{round(rsi, 1)}` | %{round(chg, 1)}\n"
                                   f"{note}\n"
                                   f"⚖️ Oran: `{ratio}` | 🏛️ 4H: `{res_1h}` | 📍 1H: `{res_1h}`{div_msg}\n"
                                   f"🔗 [Grafiği Aç (TradingView)]({tv_link})\n"
                                   f"━━━━━━━━━━━━━━━")
                
    if all_signals:
        header = "🚨 *TEKNİK SATIŞ SİNYALLERİ* 🚨\n━━━━━━━━━━━━━━━\n"
        send_telegram(header + "\n".join(all_signals))

if __name__ == "__main__":
    scan()
