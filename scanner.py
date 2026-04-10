import requests
import pandas as pd
import numpy as np
import os

# GitHub Secrets
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# STRATEJİ AYARLARI
RSI_LIMIT = 70
CHANGE_24H_LIMIT = 8
WHALE_WALL_RATIO = 2.5

def send_telegram(msg):
    if TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
        except: pass

def get_data(endpoint, params={}):
    base = "https://www.okx.com"
    try:
        res = requests.get(base + endpoint, params=params, timeout=10).json()
        return res.get('data', [])
    except: return []

def find_htf_resistance(symbol):
    """
    4 Saatlik 200 mumu tarayarak Fractal High (Zirve) bölgelerini bulur.
    Fiyatın geçmişte takıldığı 'ana kaleleri' tespit eder.
    """
    candles = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "200"})
    if not candles: return []
    
    df = pd.DataFrame(candles, columns=['ts','o','h','l','c','v','vc','vq','conf'])
    highs = df['h'].astype(float).tolist()
    
    potential_res = []
    # Fractal High: Bir mum sağındaki ve solundaki 2 mumdan yüksekse zirvedir
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            potential_res.append(highs[i])
    
    if not potential_res: return []
    
    # Mevcut fiyata en yakın üstteki 2 ana direnci döndür
    current_price = float(df['c'].iloc[0])
    upper_res = [r for r in potential_res if r > current_price]
    return sorted(list(set(upper_res)))[:2]

def get_buy_sell_ratio(symbol, df_1h=None):
    """
    Rubik API'den veri çeker, yoksa canlı mumdan sentetik oran üretir.
    """
    res = get_data("/api/v5/rubik/stat/taker-volume", {"instId": symbol, "period": "1H"})
    buy_vol, sell_vol = 0, 0
    if res and len(res) > 0:
        buy_vol, sell_vol = float(res[0][1]), float(res[0][2])

    if buy_vol == 0 and df_1h is not None:
        last = df_1h.iloc[-1]
        c, o, h, l, v = float(last['c']), float(last['o']), float(last['h']), float(last['l']), float(last['v'])
        body = abs(c - o)
        range_total = (h - l) if (h - l) > 0 else 0.0001
        if c > o:
            buy_vol = v * (0.5 + (body / range_total) * 0.5)
            sell_vol = v - buy_vol
        else:
            sell_vol = v * (0.5 + (body / range_total) * 0.5)
            buy_vol = v - sell_vol

    ratio = round(buy_vol / sell_vol, 2) if sell_vol > 0 else 1.0
    return ratio, buy_vol, sell_vol

def check_15m_micro_flow(symbol):
    m15 = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "15m", "limit": "5"})
    if not m15: return ""
    vols = [float(c[5]) for c in m15][::-1]
    closes = [float(c[4]) for c in m15][::-1]
    opens = [float(c[1]) for c in m15][::-1]
    
    notes = []
    if closes[-1] > opens[-1] and vols[-1] > vols[-2]:
        notes.append("⚠️ *TEHLİKE:* 15m barda alıcılar çok güçlü giriyor!")
    if vols[-1] < vols[-2] < vols[-3]:
        notes.append("📉 *15m Hacim Kuruması:* Satıcı iştahı azalıyor.")
    return "\n".join(notes)

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    return 100 - (100 / (1 + gain / loss))

def scan():
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    tickers = sorted(tickers, key=lambda x: float(x['vol24h']), reverse=True)
    
    signals = []
    for t in tickers:
        symbol = t['instId']
        if "-USDT-" not in symbol: continue
        
        change = (float(t['last']) / float(t['open24h']) - 1) * 100
        if change > CHANGE_24H_LIMIT:
            candles_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "100"})
            if not candles_1h: continue
            
            df_1h = pd.DataFrame(candles_1h, columns=['ts','o','h','l','c','v','vc','vq','conf'])
            df_1h = df_1h.iloc[::-1].reset_index(drop=True)
            
            # Analizler
            ratio, b_vol, s_vol = get_buy_sell_ratio(symbol, df_1h)
            htf_res = find_htf_resistance(symbol)
            micro_alert = check_15m_micro_flow(symbol)
            rsi = calculate_rsi(df_1h['c'].astype(float)).iloc[-1]
            
            if rsi > RSI_LIMIT:
                alert_status = ""
                if ratio > 1.35:
                    alert_status = "🚫 *SAKIN EKLEME YAPMA!* (Alıcılar %35+ Baskın)\n"
                elif ratio < 0.75:
                    alert_status = "📉 *SATICILAR BASKIN:* Dönüş sinyali güçlü.\n"

                header = f"🔥 *MİKRO DURUM:*\n{micro_alert}\n\n" if micro_alert else ""
                res_text = " | ".join([f"{round(r, 4)}" for r in htf_res]) if htf_res else "Bulunamadı"

                msg = (f"{alert_status}{header}🚨 *SİNYAL: {symbol}*\n\n"
                       f"📊 RSI: {round(rsi, 1)} | 📈 24s: %{round(change, 1)}\n"
                       f"🏔️ *4H ANA DİRENÇLER:* {res_text}\n"
                       f"🛒 *Alıcı/Satıcı Oranı:* {ratio}\n"
                       f"💰 Alış Hacmi: {round(b_vol/1000, 1)}K | Satış: {round(s_vol/1000, 1)}K\n\n"
                       f"🧲 *LİKİDİTE HEDEFLERİ (1H):*\n{round(float(df_1h['l'].min()), 5)}\n")
                signals.append(msg)
                
    if signals:
        send_telegram("\n---\n".join(signals))

if __name__ == "__main__":
    scan()
