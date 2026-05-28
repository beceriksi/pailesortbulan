import os
import time
import requests
import pandas as pd
from PIL import Image
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright

# ENV / SECRETS
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
        except Exception as e: print(f"Telegram metin hatası: {e}")

def send_telegram_photo(photo_path, caption):
    if TOKEN and CHAT_ID and os.path.exists(photo_path):
        url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        try:
            with open(photo_path, 'rb') as photo:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "Markdown"}, files={"photo": photo}, timeout=15)
        except Exception as e: print(f"Telegram fotoğraf hatası: {e}")

def get_data(endpoint, params={}):
    base = "https://www.okx.com"
    try:
        res = requests.get(base + endpoint, params=params, timeout=10).json()
        return res.get('data', [])
    except: return []

def take_tradingview_screenshot(tv_symbol, output_path="chart.png"):
    """Tarayıcıyı gizli modda açar, TradingView grafiğinin yüklenmesini bekler ve ekran görüntüsü alır."""
    # TradingView ücretsiz grafik widget'ı reklam barındırmayan temiz bir görünüm sunar
    url = f"https://s.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=OKX:{tv_symbol}.P&interval=60&theme=dark&style=1"
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url)
            # Grafik elementlerinin ve mumların tamamen çizilmesi için 3.5 saniye bekleme
            page.wait_for_timeout(3500)
            page.screenshot(path=output_path)
            browser.close()
        return True
    except Exception as e:
        print(f"Grafik görüntüsü alınamadı ({tv_symbol}): {e}")
        return False

def analyze_charts_with_gemini(signals_data):
    """
    signals_data: List of dict -> [{'symbol': 'BTC-USDT-SWAP', 'img_path': '...', 'text_data': '...'}, ...]
    """
    if not GEMINI_API_KEY or not signals_data:
        return None

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        
        # Gemini'ye göndereceğimiz çoklu içerik (Multimodal) listesini hazırlıyoruz
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
        
        # Her bir sinyalin görselini ve metnini analiz havuzuna ekliyoruz
        for item in signals_data:
            if os.path.exists(item['img_path']):
                img = Image.open(item['img_path'])
                contents.append(img)
                contents.append(f"Coin: {item['symbol']} Teknik Özeti:\n{item['text_data']}\n\n")
        
        # Görsel işleme yeteneği en yüksek ve güncel olan modelimizi çağırıyoruz
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
                c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "50"})
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
                    # TradingView için temiz isim (Örn: BTCUSDT)
                    tv_clean = symbol.replace('-SWAP', '').replace('-', '')
                    img_name = f"{symbol}_1h.png"
                    
                    print(f"📸 {symbol} için grafik fotoğrafı çekiliyor...")
                    # Grafiği çekip diske kaydediyoruz
                    if take_tradingview_screenshot(tv_clean, img_name):
                        text_summary = f"Fiyat: {last_p}, 24s Değişim: %{round(chg,1)}, RSI(14): {round(rsi,1)}"
                        
                        detected_signals.append({
                            "symbol": symbol,
                            "img_path": img_name,
                            "text_data": text_summary,
                            "tv_link": f"https://www.tradingview.com/chart/?symbol=OKX:{tv_clean}.P"
                        })
                    time.sleep(0.5) # API ve tarayıcıyı yormamak için
                    
        except Exception as e:
            print(f"{symbol} taranırken hata: {e}")
            continue

    # Yapay Zeka Karar Aşaması
    if detected_signals:
        # Önce Telegram'a ham listeyi duyuruyoruz
        header = "🚨 *TEKNİK ALARM VEREN COİNLER* 🚨\n━━━━━━━━━━━━━━━\n"
        list_msg = header + "\n".join([f"• [{s['symbol']}]({s['tv_link']}) | {s['text_data']}" for s in detected_signals])
        send_telegram_text(list_msg)
        
        # Gemini'ye resimleri gönderip şampiyonu seçtiriyoruz
        print("🤖 Gemini tüm grafikleri inceliyor ve en iyi işlemi seçiyor...")
        ai_report = analyze_charts_with_gemini(detected_signals)
        
        if ai_report:
            # Yapay zekanın seçtiği coini metinden ayıklamaya çalışıp o resmi rapora ekleyelim
            selected_photo = None
            for s in detected_signals:
                if s['symbol'].split('-')[0] in ai_report: # Örn: "BTC" raporda geçiyor mu?
                    selected_photo = s['img_path']
                    break
            
            # Eğer eşleşen coin resmi bulunduysa o resmi Gemini raporu açıklamasıyla gönderiyoruz
            if selected_photo:
                send_telegram_photo(selected_photo, ai_report)
            else:
                send_telegram_text(ai_report)
                
        # İşimiz bitince oluşturulan geçici resim dosyalarını temizliyoruz
        for s in detected_signals:
            if os.path.exists(s['img_path']):
                os.remove(s['img_path'])

if __name__ == "__main__":
    scan()
