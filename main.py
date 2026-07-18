import os
import csv
import time
import requests
import pandas as pd
from datetime import datetime, timezone
from PIL import Image
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

# =========================================================
# GLOBAL AYARLAR VE ENV / SECRETS
# =========================================================
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

RSI_LIMIT = 70
CHANGE_24H_LIMIT = 8
PIVOT_LEN = 4                
LOG_FILE = "signals_log.csv" 
MAX_PENDING_HOURS = 12       

# =========================================================
# TELEGRAM BİLDİRİM FONKSİYONLARI
# =========================================================
def send_telegram_text(msg):
    if TOKEN and CHAT_ID and msg:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        if len(msg) > 4000:
            parts = [msg[i:i+4000] for i in range(0, len(msg), 4000)]
            for part in parts:
                try:
                    requests.post(url, json={
                        "chat_id": CHAT_ID, "text": part, "parse_mode": "Markdown", "disable_web_page_preview": True
                    }, timeout=10)
                    time.sleep(0.5)
                except Exception as e:
                    print(f"Parçalı Telegram metin hatası: {e}")
            return
        try:
            res = requests.post(url, json={
                "chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True
            }, timeout=10)
            if res.status_code != 200:
                print(f"⚠️ Telegram Metin Gönderilemedi! Kod: {res.status_code} | Yanıt: {res.text}")
                requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "disable_web_page_preview": True}, timeout=10)
        except Exception as e:
            print(f"Telegram metin hatası: {e}")


def send_telegram_photo(photo_path, caption):
    if TOKEN and CHAT_ID and os.path.exists(photo_path):
        url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        if len(caption) > 1000:
            print("📝 Gemini analizi uzun olduğu için fotoğraf ayrı, rapor metni ayrı gönderiliyor...")
            try:
                with open(photo_path, 'rb') as photo:
                    requests.post(url, data={"chat_id": CHAT_ID, "caption": "📸 GEMINI ALFA ANALİZ GRAFİĞİ"}, files={"photo": photo}, timeout=15)
                time.sleep(1)
                send_telegram_text(caption)
            except Exception as e:
                print(f"Uzun analiz fotoğraf/metin split hatası: {e}")
            return
        try:
            res = requests.post(url, data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"}, files={"photo": photo}, timeout=15)
            if res.status_code != 200:
                print(f"⚠️ Telegram Fotoğraflı Gönderim Başarısız! Kod: {res.status_code}. Düz metin deneniyor...")
                send_telegram_text(caption)
        except Exception as e:
            print(f"Telegram fotoğraf hatası: {e}")


# =========================================================
# OKX DATA SAĞLAYICI
# =========================================================
def get_data(endpoint, params={}):
    base = "https://www.okx.com"
    try:
        res = requests.get(base + endpoint, params=params, timeout=10).json()
        return res.get('data', [])
    except:
        return []


# =========================================================
# TEKNİK ANALİZ MODÜLLERİ VE FİLTRELER
# =========================================================
def get_btc_trend():
    try:
        c_1h = get_data("/api/v5/market/candles", {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": "60"})
        if not c_1h or len(c_1h) < 55:
            return "BILINMIYOR", 0

        df = pd.DataFrame(c_1h, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
        df['c'] = df['c'].astype(float)

        ema20 = df['c'].ewm(span=20).mean().iloc[-1]
        ema50 = df['c'].ewm(span=50).mean().iloc[-1]
        last_price = df['c'].iloc[-1]

        chg_3h = (last_price / df['c'].iloc[-4] - 1) * 100 if len(df) >= 4 else 0

        if last_price > ema20 > ema50 or chg_3h > 1.5:
            return "GUCLU_YUKARI", 2
        elif last_price > ema20:
            return "HAFIF_YUKARI", 1
        elif last_price < ema20 < ema50:
            return "ASAGI", -1
        else:
            return "NOTR", 0
    except Exception as e:
        print(f"BTC trend hatası: {e}")
        return "BILINMIYOR", 0


def check_rsi_price_divergence(df):
    try:
        if len(df) < 30:
            return "", False

        close = df['c'].astype(float)
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.0001)
        rsi_series = 100 - (100 / (1 + rs))

        window = df.iloc[-20:].reset_index(drop=True)
        rsi_window = rsi_series.iloc[-20:].reset_index(drop=True)

        highs_idx = []
        h = window['h'].astype(float).values
        for i in range(2, len(h) - 2):
            if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
                highs_idx.append(i)

        if len(highs_idx) < 2:
            return "", False

        i1, i2 = highs_idx[-2], highs_idx[-1]
        price1, price2 = h[i1], h[i2]
        rsi1, rsi2 = rsi_window.iloc[i1], rsi_window.iloc[i2]

        if price2 > price1 and rsi2 < rsi1:
            return "🔻 *RSI DIVERGENCE TEYİDİ* (fiyat yeni zirve, RSI zayıflıyor)", True

        return "", False
    except Exception as e:
        print(f"Divergence hesap hatası: {e}")
        return "", False


def find_custom_sr(df, pivot_len=PIVOT_LEN):
    if len(df) < (pivot_len * 2 + 1):
        return [], []

    highs = df['h'].astype(float).values
    lows = df['l'].astype(float).values
    p_highs, p_lows = [], []

    for i in range(pivot_len, len(highs) - pivot_len):
        if all(highs[i] > highs[i-j] for j in range(1, pivot_len+1)) and \
           all(highs[i] > highs[i+j] for j in range(1, pivot_len+1)):
            p_highs.append(highs[i])
        if all(lows[i] < lows[i-j] for j in range(1, pivot_len+1)) and \
           all(lows[i] < lows[i+j] for j in range(1, pivot_len+1)):
            p_lows.append(lows[i])

    return p_highs, p_lows


def check_volume_divergence(df):
    if len(df) < 5:
        return ""
    p = df['c'].astype(float).iloc[-5:].values
    v = df['v'].astype(float).iloc[-5:].values
    if p[-1] > p[0] and v[-1] < v[-2]:
        return "⚠️ *AYI UYUMSUZLUĞU (Düşüş Beklentisi)*"
    return ""


def check_candle_trigger_15m(symbol):
    try:
        c_15m = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "15m", "limit": "6"})
        if not c_15m or len(c_15m) < 4:
            return "⏳ 15m veri alınamadı", 0

        df_15m = pd.DataFrame(c_15m, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
        df_15m[['o', 'h', 'l', 'c']] = df_15m[['o', 'h', 'l', 'c']].astype(float)

        last3 = df_15m.iloc[-3:].reset_index(drop=True)
        o, h, l, c = last3.iloc[-1]['o'], last3.iloc[-1]['h'], last3.iloc[-1]['l'], last3.iloc[-1]['c']

        body = abs(c - o)
        upper_wick = (h - max(o, c)) if h > max(o, c) else 0
        last_candle_bearish = (c < o) or (upper_wick > body * 0.8)

        lower_high = last3.iloc[-1]['h'] < last3.iloc[-2]['h']
        closes_weakening = last3.iloc[-1]['c'] < last3.iloc[-2]['c']

        confirmations = sum([last_candle_bearish, lower_high, closes_weakening])

        if confirmations >= 2:
            return f"🚨 *ACİL DURUM: REAKSİYON TEYİTLİ ({confirmations}/3)*", confirmations
        elif confirmations == 1:
            return "⚠️ 15m'de zayıf reaksiyon belirtisi (tek teyit, henüz güvenilir değil)", 1
        else:
            return "⏳ 15m Grafik Hala Güçlü Yeşil Gövdeli", 0
    except Exception as e:
        print(f"15m tetik hatası: {e}")
        return "⏳ 15m Durumu Okunamadı", 0


def get_funding_rate(symbol):
    res = get_data("/api/v5/public/funding-rate", {"instId": symbol})
    if res and len(res) > 0:
        rate = float(res[0].get('fundingRate', 0))
        rate_pct = rate * 100
        if rate_pct < -0.05:
            return f"%{rate_pct:.3f}", "⚠️ *AŞIRI NEGATİF FL (short sıkışması riski)*"
        elif rate_pct > 0.08:
            return f"%{rate_pct:.3f}", "🔥 *AŞIRI POZİTİF FL (long tarafı aşırı kaldıraçlı)*"
        return f"%{rate_pct:.3f}", ""
    return "Veri Alınamadı", ""


# =========================================================
# YENİ VE İLERİ DÜZEY DATA UÇ NOKTALARI (OI & CVD MANTIĞI)
# =========================================================
def get_oi_trend(symbol):
    try:
        if not symbol.endswith("-SWAP"):
            inst_id = f"{symbol.split('-')[0]}-USDT-SWAP"
        else:
            inst_id = symbol

        res = get_data("/api/v5/public/open-interest", {"instId": inst_id})
        if not res or len(res) == 0:
            return "OI Verisi Alınamadı", 0

        oi_now = float(res[0].get('oi', 0))
        return f"Anlık OI: {oi_now:.1f} Kontrat", oi_now
    except Exception as e:
        print(f"OI kamu fonksiyonu hatası ({symbol}): {e}")
        return "OI Verisi Yok (Hata)", 0


def get_net_buying_pressure(symbol):
    try:
        if not symbol.endswith("-SWAP"):
            inst_id = f"{symbol.split('-')[0]}-USDT-SWAP"
        else:
            inst_id = symbol

        res = get_data("/api/v5/rubik/stat/taker-volume", {"instId": inst_id, "period": "1H"})
        
        if res and len(res) > 0:
            buy_vol = float(res[0][1])   
            sell_vol = float(res[0][2])  
            total_vol = buy_vol + sell_vol

            if total_vol == 0:
                return "Hacim Yok", 0

            net_pressure_pct = ((buy_vol - sell_vol) / total_vol) * 100
            
            if net_pressure_pct > 15:
                return f"🔥 SAF ALIM BASKISI: +%{net_pressure_pct:.1f} (Alıcılar Dominant)", net_pressure_pct
            elif net_pressure_pct < -15:
                return f"🔻 SAF SATIŞ BASKISI: %{net_pressure_pct:.1f} (Gizli Dağıtım / Mal Boşaltma)", net_pressure_pct
            else:
                return f"Dengeli Konsolidasyon: %{net_pressure_pct:.1f}", net_pressure_pct
        
        return "Saf Alım Verisi Alınamadı", 0
    except Exception as e:
        print(f"Saf alım baskısı hesaplama hatası ({symbol}): {e}")
        return "Saf Alım Verisi Yok", 0


# =========================================================
# SİNYAL TAKİP VE GÜNCELLEME SİSTEMİ
# =========================================================
def check_signal_status(symbol, entry_price, entry_rsi, res_1h):
    try:
        c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "30"})
        if not c_1h or len(c_1h) < 20:
            return "PENDING", 0

        df = pd.DataFrame(c_1h, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
        df['c'] = df['c'].astype(float)

        last_closed = df.iloc[-2]  
        current_close = float(last_closed['c'])
        pct_move = (current_close / entry_price - 1) * 100

        delta = df['c'].diff()
        g = (delta.where(delta > 0, 0)).rolling(14).mean()
        l = (-delta.where(delta < 0, 0)).rolling(14).mean()
        last_g, last_l = g.iloc[-2], l.iloc[-2]
        current_rsi = 100 - (100 / (1 + last_g / last_l)) if last_l != 0 else 50

        if res_1h and res_1h > 0 and current_close > res_1h * 1.003:
            return "INVALIDATE_STRUCTURE", pct_move

        if current_rsi > entry_rsi + 5 and current_close > entry_price:
            return "INVALIDATE_MOMENTUM", pct_move

        if pct_move <= -3:
            return "SUCCESS", pct_move

        return "PENDING", pct_move
    except Exception as e:
        print(f"Sinyal durum kontrolü hatası ({symbol}): {e}")
        return "PENDING", 0


def update_open_signals():
    print("⏳ Mevcut açık sinyaller kontrol ediliyor...")
    if not os.path.exists(LOG_FILE):
        return

    try:
        df = pd.read_csv(LOG_FILE)
    except Exception as e:
        print(f"Log okuma hatası: {e}")
        return

    if df.empty:
        return

    now = datetime.now(timezone.utc)
    updated = False
    notify_msgs = []

    for idx, row in df.iterrows():
        if str(row.get("resolved")) == "TRUE":
            continue
        try:
            symbol = row["symbol"]
            entry_price = float(row["entry_price"])
            entry_rsi = float(row["rsi"])
            res_1h = float(row["res_1h"]) if pd.notna(row["res_1h"]) else 0

            status, pct_move = check_signal_status(symbol, entry_price, entry_rsi, res_1h)

            ts = pd.to_datetime(row["timestamp_utc"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            hours_passed = (now - ts).total_seconds() / 3600

            if status == "INVALIDATE_STRUCTURE":
                df.at[idx, "outcome"] = "BASARISIZ (direnc kirildi)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(f"🔁 *SİNYAL GEÇERSİZ (YAPI KIRILDI):* {symbol} 1H direnç seviyesinin üzerinde kapandı (%{pct_move:.1f}). Short tezi geçersiz.")
            elif status == "INVALIDATE_MOMENTUM":
                df.at[idx, "outcome"] = "BASARISIZ (momentum geri geldi)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(f"📉 *SİNYAL GEÇERSİZ (MOMENTUM DÖNDÜ):* {symbol} için aşırı alım bölgesi tükenmedi, RSI yükseliyor (%{pct_move:.1f}).")
            elif status == "SUCCESS":
                df.at[idx, "outcome"] = "BAŞARILI (hedef yakalandı)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(f"💰 *SİNYAL BAŞARILI:* {symbol} beklendiği gibi dirençten çöktü! Hareket: %{pct_move:.1f}")
            elif hours_passed > MAX_PENDING_HOURS:
                df.at[idx, "outcome"] = "ZAMAN ASIMI"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True

        except Exception as e:
            print(f"Satır işleme hatası (Satır {idx}): {e}")

    if updated:
        df.to_csv(LOG_FILE, index=False)
        for msg in notify_msgs:
            send_telegram_text(msg)


# =========================================================
# EKRAN GÖRÜNTÜSÜ VE GEMINI ENTEGRASYONU
# =========================================================
def take_tradingview_screenshot(tv_symbol, output_path="chart.png"):
    url = f"https://s.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=OKX:{tv_symbol}.P&interval=60&theme=dark&style=1"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url)
            page.wait_for_timeout(4000)
            page.screenshot(path=output_path)
            browser.close()
        return True
    except Exception as e:
        print(f"Grafik görüntüsü alınamadı ({tv_symbol}): {e}")
        return False


def analyze_charts_with_gemini(signals_data, btc_trend_label):
    if not GEMINI_API_KEY or not signals_data:
        return None

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        contents = []

        prompt = f"""
        Sen üst düzey bir teknik analistsin. Genel piyasa (BTC) trend durumu şu anda: {btc_trend_label}.

        Sana tarayıcı botumdan yakalanan adayların grafiklerini ve anlık teknik verilerini (15m reaksiyon,
        RSI divergence, funding oranı, open interest trendi ve Saf Alım Baskısı) gönderiyorum.

        1. BÖLÜM: TÜM LİSTEYE BAKIŞ
        Her coin için 1-2 cümlelik net teknik yorum yaz.
        Format: • **[COIN ADI]**: [yorum]

        2. BÖLÜM: 👑 GEMINI ALFA SEÇİMİ
        Kısa vadeli SHORT için EN GÜVENLİ 1 coini seç:
        🎯 **İşlem Yapılacak Coin:** [COIN ADI]
        💡 **Neden Bu Grafik?:** [detaylı gerekçe]
        🛑 **Risk & Stop Yönetimi:** [iptal seviyesi]
        """
        contents.append(prompt)

        for item in signals_data:
            if os.path.exists(item['img_path']):
                img = Image.open(item['img_path'])
                contents.append(img)
                contents.append(f"Coin: {item['symbol']} Teknik Özeti:\n{item['text_data']}\n\n")

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
        )
        return response.text
    except Exception as e:
        print(f"Gemini analiz hatası: {e}")
        return None


# =========================================================
# ORİJİNAL VE TAM OKX PİYASA TARAMA DÖNGÜSÜ
# =========================================================
def get_market_candidates():
    print("🌐 OKX Borsasındaki aktif vadeli işlem pariteleri taranıyor...")
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    candidates = []

    if not tickers:
        print("⚠️ Ticker verisi borsa API'sinden çekilemedi.")
        return []

    usdt_swaps = [t for t in tickers if t['instId'].endswith("-USDT-SWAP")]

    for t in usdt_swaps:
        symbol = t['instId']
        try:
            chg24h = ((float(t['last']) / float(t['open24h'])) - 1) * 100
        except:
            continue

        if chg24h < CHANGE_24H_LIMIT:
            continue

        c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "35"})
        if not c_1h or len(c_1h) < 20:
            continue

        df = pd.DataFrame(c_1h, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
        df['c'] = df['c'].astype(float)
        
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, 0.0001)
        rsi = 100 - (100 / (1 + rs))
        last_rsi = rsi.iloc[-1]

        vol_div = check_volume_divergence(df)
        has_vol_anomaly = vol_div != ""

        if last_rsi >= RSI_LIMIT or has_vol_anomaly:
            p_highs, p_lows = find_custom_sr(df)
            res_1h = p_highs[-1] if p_highs else (df['h'].astype(float).max())

            candidates.append({
                "symbol": symbol,
                "current_price": float(t['last']),
                "chg": chg24h,
                "rsi": last_rsi,
                "res_1h": res_1h,
                "vol_anomaly": vol_div
            })
            
    return candidates


# =========================================================
# ANA YÜRÜTÜCÜ (MAINLOOP)
# =========================================================
def main():
    print(f"🚀 Gem Hunter Botu Başlatıldı: {datetime.now(timezone.utc).isoformat()} UTC")
    
    # 1. Aşama: Açık pozisyon takip kayıtlarını güncelle
    update_open_signals()
    
    # 2. Aşama: Genel Market Analizi
    btc_label, btc_score = get_btc_trend()
    print(f"ℹ️ Mevcut BTC Trendi: {btc_label}")
    
    candidates = get_market_candidates()
    if not candidates:
        print("📭 Bu tarama döngüsünde kriterlere uyan aday bulunamadı.")
        return

    print(f"📊 Toplam {len(candidates)} teknik aday bulundu, detaylar çekiliyor...")
    signals_data = []
    
    for coin in candidates:
        symbol = coin["symbol"]
        oi_label, _ = get_oi_trend(symbol)
        pressure_label, _ = get_net_buying_pressure(symbol)
        
