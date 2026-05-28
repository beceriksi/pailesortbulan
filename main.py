import os
import time
import requests
import pandas as pd
from PIL import Image
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

# ENV / SECRETS (GitHub veya Bilgisayarınızdan Alınır)
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# STRATEJİ AYARLARI
RSI_LIMIT = 70
CHANGE_24H_LIMIT = 8

def send_telegram_text(msg):
    if TOKEN and CHAT_ID and msg:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        try:
            requests.post(url, json={
                "chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown", "disable_web_page_preview": True
            }, timeout=10)
        except Exception as e: 
            print(f"Telegram metin hatası: {e}")

def send_telegram_photo(photo_path, caption):
    if TOKEN and CHAT_ID and os.path.exists(photo_path):
        url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        try:
            with open(photo_path, 'rb') as photo:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"}, files={"photo": photo}, timeout=15)
        except Exception as e: 
            print(f"Telegram fotoğraf hatası: {e}")

def get_data(endpoint, params={}):
    base = "https://www.okx.com"
    try:
        res = requests.get(base + endpoint, params=params, timeout=10).json()
        return res.get('data', [])
    except: 
        return []

def find_custom_sr(df, pivot_len=2):
    if len(df) < (pivot_len * 2 + 1):
        return [], []
        
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
    buy_v, sell_v = 0, 0
    
    if res and len(res) > 0 and len(res[0]) > 2 and float(res[0][1]) > 0:
        buy_v = float(res[0][1])
        sell_v = float(res[0][2])
    else:
        if not df_1h.empty:
            last = df_1h.iloc[-1]
            c, o, h, l, v = float(last['c']), float(last['o']), float(last['h']), float(last['l']), float(last['v'])
            body = abs(c - o)
            total_range = (h - l) if (h - l) > 0 else 0.0001
            if c > o: 
                buy_v = v * (0.5 + (body / total_range) * 0.5)
                sell_v = v - buy_v
            else: 
                sell_v = v * (0.5 + (body / total_range) * 0.5)
                buy_v = v - sell_v
                
    return buy_v, sell_v, round(buy_v / sell_v, 2) if sell_v > 0 else 1.0

def check_volume_divergence(df):
    if len(df) < 5:
        return ""
    p = df['c'].astype(float).iloc[-5:].values
    v = df['v'].astype(float).iloc[-5:].values
    if p[-1] > p[0] and v[-1] < v[-2]: 
        return "⚠️ *AYI UYUMSUZLUĞU (Düşüş Beklentisi)*"
    return ""

def take_tradingview_screenshot(tv_symbol, output_path="chart.png"):
    url = f"https://s.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=OKX:{tv_symbol}.P&interval=60&theme=dark&style=1"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url)
            page.wait_for_timeout(3500)
            page.screenshot(path=output_path)
            browser.close()
        return True
    except Exception as e:
        print(f"Grafik görüntüsü alınamadı ({tv_symbol}): {e}")
        return False

def analyze_charts_with_gemini(signals_data):
    if not GEMINI_API_KEY or not signals_data:
        return None

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        contents = []
        
        prompt = """
        Sen üst düzey bir kripto para teknik analisti ve kurumsal bir short (açığa satış) trader'ısın.
        Sana şu anda tarayıcı botumdan yakalanan ve aşırı şişmiş (RSI > 70 ve %8 üstü yükselmiş) coinlerin 1 Saatlik grafiklerini ve anlık teknik verilerini gönderiyorum.
        
        Senden ricam:
        1. Gönderilen tüm grafikleri (mum yapılarını, trendlerin yorulma iğnelerini, direnç bölgelerini) ve metin verilerini birbiriyle kıyasla.
        2. İçlerinden kısa vadeli (scalping/day-trade) SHORT pozisyon açmak için EN GÜVENLİ, yapısı en çok bozulan ve düşüş olasılığı en yüksek olan 1 ADET coini seç.
        3. Seçtiğin coini ve nedenini harika bir üslupla, teknik gerekçeleriyle (mum iğneleri, hacim, direnç ihlali vb.) açıkla. Kısa, vurucu ve net ol.
        
        Analizini tam olarak şu taslakta teslim et:
        👑 **GEMINI ALFA SEÇİMİ**
        🎯 **İşlem Yapılacak Coin:** [COIN ADI]
        
        💡 **Neden Bu Grafik? (Teknik Gerekçe):** (Buraya grafik üzerindeki mum hareketlerine ve verilere dayanarak seçme nedenini maksimum 3 cümle ile açıkla)
        
        🛑 **Risk & Stop Yönetimi:** (İşlemin iptal olması gereken direnç seviyesi veya dikkat edilecek nokta)
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

def scan():
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    if not tickers: return

    detected_signals = []
    
    for t in tickers:
        symbol = t['instId']
        if "-USDT-" not in symbol: continue
        
        try:
            last_p = float(t['last'])
            open_24h = float(t['open24h'])
            if open_24h == 0: continue
            chg = (last_p / open_24h - 1) * 100
            
            if chg > CHANGE_24H_LIMIT:
                c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "100"})
                if not c_1h: continue
                
                df_1h = pd.DataFrame(c_1h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
                df_1h['c'] = df_1h['c'].astype(float)
                
                if len(df_1h) < 15: continue
                delta = df_1h['c'].diff()
                g = (delta.where(delta > 0, 0)).rolling(14).mean()
                l = (-delta.where(delta < 0, 0)).rolling(14).mean()
                
                last_g, last_l = g.iloc[-1], l.iloc[-1]
                rsi = 100 - (100 / (1 + last_g / last_l)) if last_l != 0 else (100 if last_g > 0 else 50)
                
                if rsi > RSI_LIMIT:
                    c_4h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
                    if not c_4h: continue
                    df_4h = pd.DataFrame(c_4h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
                    
                    ph_4h, _ = find_custom_sr(df_4h)
                    res_4h = min([x for x in ph_4h if x > last_p]) if any(x > last_p for x in ph_4h) else (ph_4h[-1] if ph_4h else 0)
                    
                    ph_1h, _ = find_custom_sr(df_1h)
                    res_1h = min([x for x in ph_1h if x > last_p]) if any(x > last_p for x in ph_1h) else (ph_1h[-1] if ph_1h else 0)

                    bv, sv, ratio = get_smart_volume(symbol, df_1h)
                    div = check_volume_divergence(df_1h)

                    # STRATEJİK YORUM MANTIĞI
                    if last_p > res_4h and res_4h != 0: 
                        note = "🔥 *KIRILIM:* 4H Direnç üstü kapanış, tehlikeli!"
                    elif res_1h != 0 and (res_1h - last_p) / last_p < 0.006: 
                        note = "🚨 *DİRENÇTE:* Fiyat dirençten satış yemeye çalışıyor."
                    elif ratio < 0.85: 
                        note = "📉 *SATIŞ BASKISI:* Direnç altı hacimli satışlar başladı."
                    elif ratio > 1.35: 
                        note = "🚫 *ZİRVEDE ALICI:* FOMO var, ekleme yapmak riskli."
                    else: 
                        note = "🛡️ *GÖZLEM:* Direnç altı kararsız yapı."

                    tv_clean = symbol.replace('-SWAP', '').replace('-', '')
                    img_name = f"{symbol}_1h.png"
                    
                    print(f"📸 {symbol} için grafik çekiliyor...")
                    if take_tradingview_screenshot(tv_clean, img_name):
                        text_summary = f"Fiyat: {last_p} | 4H: {res_4h} | 1H: {res_1h}"
                        
                        detected_signals.append({
                            "symbol": symbol,
                            "img_path": img_name,
                            "text_data": text_summary,
                            "rsi": round(rsi, 1),
                            "chg": round(chg, 1),
                            "ratio": ratio,
                            "div": div,
                            "note": note,
                            "tv_link": f"https://www.tradingview.com/chart/?symbol=OKX:{tv_clean}.P"
                        })
                    time.sleep(0.5)
                    
        except Exception as e:
            print(f"{symbol} taranırken hata: {e}")
            continue

    # Telegram ve Yapay Zeka Çıktı Aşaması
    if detected_signals:
        # 1. MESAJ: Tüm listeyi ve stratejik notları (Ayı Uyumsuzluğu, FOMO vb.) Telegram'a atar
        header = "🚨 *TEKNİK ALARM VEREN COİNLER* 🚨\n━━━━━━━━━━━━━━━\n"
        list_elements = []
        for s in detected_signals:
            div_text = f" | {s['div']}" if s['div'] else ""
            item_msg = (f"• *{s['symbol']}* | RSI: `{s['rsi']}` | %{s['chg']}\n"
                        f"  👉 {s['note']}{div_text}\n"
                        f"  ⚖️ Hacim Oranı: `{s['ratio']}`\n"
                        f"  🔗 [Grafiği Aç]({s['tv_link']})\n"
                        f"  ━━━━━━━━━━━━━━━")
            list_elements.append(item_msg)
            
        send_telegram_text(header + "\n".join(list_elements))
        
        # 2. MESAJ: Gemini tüm resimleri inceler ve en mantıklı olanı seçer
        print("🤖 Gemini grafikleri analiz ediyor...")
        ai_report = analyze_charts_with_gemini(detected_signals)
        
        if ai_report:
            selected_photo = None
            for s in detected_signals:
                if s['symbol'].split('-')[0] in ai_report:
                    selected_photo = s['img_path']
                    break
            
            if selected_photo:
                send_telegram_photo(selected_photo, ai_report)
            else:
                send_telegram_text(ai_report)
                
        # Temizlik
        for s in detected_signals:
            if os.path.exists(s['img_path']):
                os.remove(s['img_path'])

if __name__ == "__main__":
    scan()
