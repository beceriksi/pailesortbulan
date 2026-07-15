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

# ENV / SECRETS
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# STRATEJİ AYARLARI
RSI_LIMIT = 70
CHANGE_24H_LIMIT = 8
PIVOT_LEN = 4                # 2 -> 4: çok kısa pivot gürültülü direnç üretiyordu
LOG_FILE = "signals_log.csv" # Backtest / performans takibi için
MAX_PENDING_HOURS = 12       # Bu süreyi dolduran ama hiçbir yapısal karar üretmeyen
                              # sinyaller "ZAMAN AŞIMI" olarak kapatılır (log şişmesin diye).
                              # İptal/başarı kararı artık saate değil yapıya bakıyor (aşağıda).


# =========================================================
# TELEGRAM (değişmedi)
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


def get_data(endpoint, params={}):
    base = "https://www.okx.com"
    try:
        res = requests.get(base + endpoint, params=params, timeout=10).json()
        return res.get('data', [])
    except:
        return []


# =========================================================
# YENİ #1: BTC TREND FİLTRESİ
# Altcoin short'u BTC güçlü yükselirken açmak "terste kalma"
# vakalarının en büyük kaynağıydı. Genel piyasa yönünü ölçüp
# BTC yukarı trendse sinyalleri ya eliyoruz ya da bariz
# uyarıyla işaretliyoruz.
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

        # Son 3 saatteki değişim de eklenir: EMA henüz dönmemiş olsa da
        # ani BTC pump'ı varsa yakalar.
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


# =========================================================
# YENİ #2: RSI/FİYAT DİVERGENCE (klasik tepe yakalama sinyali)
# Fiyat yeni zirve yaparken RSI önceki zirveden daha düşükse
# bu, momentumun tükendiğinin en güvenilir işaretlerinden biri.
# =========================================================
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

        # basit pivot-yüksek bulucu (son 20 barda)
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


# =========================================================
# YENİ #3: 15m TETİK - artık tek muma değil son 3 muma bakıyor
# Tek kırmızı/fitilli mum pump ortasında da rastgele oluşabiliyordu.
# Şimdi "art arda düşen tepe (lower-high)" + kapanış zayıflığı
# birlikte aranıyor -> yanlış pozitiflerin en büyük kaynağıydı.
# =========================================================
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

        # lower-high teyidi: son mumun tepe noktası bir öncekinden düşük mü
        lower_high = last3.iloc[-1]['h'] < last3.iloc[-2]['h']
        # kapanış zayıflığı: son 2 mum kapanışı gerileme eğiliminde mi
        closes_weakening = last3.iloc[-1]['c'] < last3.iloc[-2]['c']

        confirmations = sum([last_candle_bearish, lower_high, closes_weakening])

        if confirmations >= 2:
            return f"🚨 *ACİL DURUM: REAKSİYON TEYİTLİ ({confirmations}/3)*", confirmations
        elif confirmations == 1:
            return "⚠️ 15m'de zayıf reaksiyon belirtisi (tek teyit, henüz güvenilir değil)", 0
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
# YENİ #4: OPEN INTEREST
# Funding tek başına yeterli değil. Yüksek pozitif funding +
# yükselen OI = agresif yeni long girişleri = pump'ın devam
# etme ihtimali yüksek -> tam "pump ortasında sinyal" riski.
# =========================================================
def get_oi_trend(symbol):
    try:
        res = get_data("/api/v5/rubik/stat/contracts/open-interest-volume", {"instId": symbol, "period": "1H"})
        if not res or len(res) < 4:
            return "OI verisi yok", 0

        oi_now = float(res[0][1])
        oi_prev = float(res[3][1])
        if oi_prev == 0:
            return "OI verisi yok", 0

        oi_chg = (oi_now / oi_prev - 1) * 100
        if oi_chg > 5:
            return f"OI son 3H: +%{oi_chg:.1f} (yeni pozisyon girişi yoğun)", 1
        elif oi_chg < -5:
            return f"OI son 3H: %{oi_chg:.1f} (pozisyon kapanışı / tükeniş)", -1
        return f"OI son 3H: %{oi_chg:.1f}", 0
    except Exception as e:
        print(f"OI hatası: {e}")
        return "OI verisi yok", 0


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
        Sen üst düzey bir kripto para teknik analisti ve kurumsal bir short (açığa satış) trader'ısın.
        Genel piyasa (BTC) trend durumu şu anda: {btc_trend_label}. Bu bilgiyi seçimlerinde MUTLAKA dikkate al -
        BTC güçlü yukarı trendteyse short önerini buna göre temkinli ver ya da riskini açıkça belirt.

        Sana şu anda tarayıcı botumdan yakalanan ve aşırı şişmiş (RSI > 70 ve %8 üstü yükselmiş) coinlerin
        1 Saatlik grafiklerini ve anlık teknik verilerini gönderiyorum. Verilerde 15m reaksiyon mumu teyit sayısı,
        RSI divergence durumu, funding oranı ve open interest trendi de bulunuyor.

        1. BÖLÜM: TÜM LİSTEYE BAKIŞ (SANA GÖNDERİLEN SIRA İLE)
        Her coin için 1-2 cümlelik net teknik yorum yaz. 15m teyit sayısı düşükse veya OI hâlâ artıyorsa
        bunun "erken / riskli" olabileceğini belirt.
        Format: • **[COIN ADI]**: [yorum]

        2. BÖLÜM: 👑 GEMINI ALFA SEÇİMİ
        Listeden kısa vadeli SHORT için EN GÜVENLİ (15m çoklu teyit + divergence + OI tükeniş sinyali olan) 1 coini seç:
        🎯 **İşlem Yapılacak Coin:** [COIN ADI]
        💡 **Neden Bu Grafik?:** [detaylı gerekçe]
        🛑 **Risk & Stop Yönetimi:** [iptal seviyesi]

        Eğer listede hiçbir coin yeterince güvenilir teyide sahip değilse (örn. tüm 15m teyitler 1/3 veya altı),
        bunu açıkça belirt ve "net alfa önerisi yok, izlemede kal" de. Şablon dışına çıkma.
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
# YENİ #5: LOGLAMA + BACKTEST ALTYAPISI
# Her sinyal CSV'ye yazılır. Bir sonraki taramalarda
# INVALIDATE_HOURS saat dolan açık sinyaller kontrol edilip
# sonuç (hedef/stop/nötr) işaretlenir. Bu olmadan hangi filtre
# gerçekten işe yarıyor bilinemez.
# =========================================================
def log_signal(s):
    file_exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "timestamp_utc", "symbol", "entry_price", "rsi", "chg24h",
                "res_4h", "res_1h", "candle_confirmations", "divergence",
                "funding", "oi_trend", "vol_ratio", "btc_trend",
                "outcome", "outcome_pct", "resolved"
            ])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(), s['symbol'], s['entry_price'],
            s['rsi'], s['chg'], s['res_4h'], s['res_1h'], s['urgent'],
            s['has_divergence'], s['funding_raw'], s['oi_trend_raw'], s['ratio'],
            s['btc_trend_label'], "", "", "FALSE"
        ])


def check_signal_status(symbol, entry_price, entry_rsi, res_1h):
    """Sinyalin hâlâ geçerli mi, yapısal olarak bozuldu mu (iptal), yoksa
    beklendiği gibi çöktü mü (başarı) buna bakar. Sabit yüzde yerine YAPI
    kullanılıyor çünkü %2'lik bir dalgalanma gürültü olabilir ama direncin
    üzerinde KAPANMAK ya da momentumun gerçekten geri gelmesi gürültü değil,
    tezin bozulduğunun somut kanıtıdır."""
    try:
        c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "30"})
        if not c_1h or len(c_1h) < 20:
            return "PENDING", 0, None

        df = pd.DataFrame(c_1h, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
        df['c'] = df['c'].astype(float)

        last_closed = df.iloc[-2]  # son kapanmış 1H mum (anlık mum henüz tamamlanmadı)
        current_close = float(last_closed['c'])
        pct_move = (current_close / entry_price - 1) * 100

        # güncel RSI
        delta = df['c'].diff()
        g = (delta.where(delta > 0, 0)).rolling(14).mean()
        l = (-delta.where(delta < 0, 0)).rolling(14).mean()
        last_g, last_l = g.iloc[-2], l.iloc[-2]
        current_rsi = 100 - (100 / (1 + last_g / last_l)) if last_l != 0 else 50

        # --- 1) YAPISAL KIRILIM: direnç seviyesinin üzerinde KAPANIŞ ---
        # Sadece fitille dokunmak değil, kapanışın üstte kalması aranıyor.
        if res_1h and res_1h > 0 and current_close > res_1h * 1.003:
            return "INVALIDATE_STRUCTURE", pct_move, current_rsi

        # --- 2) MOMENTUM GERİ DÖNÜŞÜ: RSI sinyal anındakinden belirgin yüksek
        # VE fiyat da hâlâ yukarıdaysa, "aşırı alım tükeniyor" tezi çökmüş demektir ---
        if current_rsi > entry_rsi + 5 and current_close > entry_price:
            return "INVALIDATE_MOMENTUM", pct_move, current_rsi

        # --- 3) BAŞARI: fiyat belirgin şekilde düştü (yapısal destek kırıldı sayılır) ---
        if pct_move <= -3:
            return "SUCCESS", pct_move, current_rsi

        return "PENDING", pct_move, current_rsi
    except Exception as e:
        print(f"Sinyal durum kontrolü hatası ({symbol}): {e}")
        return "PENDING", 0, None


def update_open_signals():
    """Her taramada açık sinyalleri YAPIYA göre değerlendirir (sabit saat/yüzde
    beklemez) — böylece tersine dönüş olur olmaz haber verilir, gecikme azalır.
    Uzun süre karar üretmeyen sinyaller MAX_PENDING_HOURS sonunda zaman aşımıyla kapatılır."""
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
            entry_price = float(row["entry_price"])
            entry_rsi = float(row["rsi"])
            res_1h = float(row["res_1h"]) if pd.notna(row["res_1h"]) else 0

            status, pct_move, current_rsi = check_signal_status(row["symbol"], entry_price, entry_rsi, res_1h)

            ts = pd.to_datetime(row["timestamp_utc"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            hours_passed = (now - ts).total_seconds() / 3600

            if status == "INVALIDATE_STRUCTURE":
                df.at[idx, "outcome"] = "BASARISIZ (direnc kirildi)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(
                    f"🔁 *SİNYAL GEÇERSİZ (YAPI KIRILDI):* {row['symbol']} 1H direnç seviyesinin "
                    f"üzerinde kapandı (%{pct_move:.1f}). Trend gerçekten dönmemiş, short tez geçersiz."
                )
            elif status == "INVALIDATE_MOMENTUM":
                df.at[idx, "outcome"] = "BASARISIZ (momentum geri geldi)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(
                    f"🔁 *SİNYAL GEÇERSİZ (MOMENTUM DÖNDÜ):* {row['symbol']} RSI sinyal anındaki "
                    f"{entry_rsi:.1f}'den {current_rsi:.1f}'e çıktı ve fiyat hâlâ yukarıda. "
                    f"Aşırı alım tükenmemiş, tez geçersiz."
                )
            elif status == "SUCCESS":
                df.at[idx, "outcome"] = "BASARILI (dustu)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
            elif hours_passed >= MAX_PENDING_HOURS:
                # Ne yapısal kırılım ne net düşüş oldu -> belirsiz, zorla kapat
                df.at[idx, "outcome"] = f"ZAMAN ASIMI (net karar yok, %{pct_move:.1f})"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True

        except Exception as e:
            print(f"Sinyal güncelleme hatası ({row.get('symbol')}): {e}")
            continue

    if updated:
        df.to_csv(LOG_FILE, index=False)

    if notify_msgs:
        send_telegram_text("\n\n".join(notify_msgs))


def print_performance_summary():
    """İsteğe bağlı: konsola kısa bir başarı oranı özeti basar."""
    if not os.path.exists(LOG_FILE):
        return
    try:
        df = pd.read_csv(LOG_FILE)
        resolved = df[df["resolved"] == True] if df["resolved"].dtype == bool else df[df["resolved"].astype(str) == "TRUE"]
        if len(resolved) == 0:
            return
        success_rate = (resolved["outcome"].astype(str).str.contains("BASARILI")).mean() * 100
        print(f"📊 Şu ana kadar çözümlenen {len(resolved)} sinyalin başarı oranı: %{success_rate:.1f}")
    except Exception as e:
        print(f"Performans özeti hatası: {e}")


def scan():
    print("🔍 Önce açık (bekleyen) sinyaller güncelleniyor...")
    update_open_signals()
    print_performance_summary()

    print("🧭 BTC trend durumu kontrol ediliyor...")
    btc_trend_label, btc_score = get_btc_trend()
    print(f"   BTC Trend: {btc_trend_label} (skor: {btc_score})")

    print("🔍 OKX Tickers verisi çekiliyor...")
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    if not tickers:
        print("❌ OKX'ten hiçbir ticker verisi alınamadı!")
        return
    print(f"📊 Toplam {len(tickers)} adet swap çifti bulundu.")

    detected_signals = []

    for t in tickers:
        symbol = t['instId']
        if "-USDT-" not in symbol:
            continue

        try:
            last_p = float(t['last'])
            open_24h = float(t['open24h'])
            if open_24h == 0:
                continue
            chg = (last_p / open_24h - 1) * 100

            if chg > CHANGE_24H_LIMIT:
                c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "100"})
                if not c_1h:
                    continue

                df_1h = pd.DataFrame(c_1h, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
                df_1h['c'] = df_1h['c'].astype(float)

                if len(df_1h) < 30:
                    continue

                delta = df_1h['c'].diff()
                g = (delta.where(delta > 0, 0)).rolling(14).mean()
                l = (-delta.where(delta < 0, 0)).rolling(14).mean()

                last_g, last_l = g.iloc[-1], l.iloc[-1]
                rsi = 100 - (100 / (1 + last_g / last_l)) if last_l != 0 else (100 if last_g > 0 else 50)

                if rsi > RSI_LIMIT:
                    print(f"🔥 KRİTİK KOŞUL SAĞLANDI: {symbol} -> RSI: {rsi:.2f} | Değişim: %{chg:.2f}")

                    c_4h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
                    if not c_4h:
                        continue
                    df_4h = pd.DataFrame(c_4h, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)

                    ph_4h, _ = find_custom_sr(df_4h)
                    res_4h = min([x for x in ph_4h if x > last_p]) if any(x > last_p for x in ph_4h) else (ph_4h[-1] if ph_4h else 0)

                    ph_1h, _ = find_custom_sr(df_1h)
                    res_1h = min([x for x in ph_1h if x > last_p]) if any(x > last_p for x in ph_1h) else (ph_1h[-1] if ph_1h else 0)

                    bv, sv, ratio = get_smart_volume(symbol, df_1h)
                    div = check_volume_divergence(df_1h)
                    rsi_div_text, has_divergence = check_rsi_price_divergence(df_1h)

                    candle_text, urgent_score = check_candle_trigger_15m(symbol)
                    funding_raw, funding_warn = get_funding_rate(symbol)
                    oi_text, oi_trend_raw = get_oi_trend(symbol)

                    # --- KARAR NOTU: artık tek kritere değil birleşik skora bakıyor ---
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

                    # BTC güçlü yukarı trendde ve OI hâlâ artıyorsa -> yüksek risk uyarısı
                    risk_flags = []
                    if btc_score >= 2:
                        risk_flags.append("⚠️ BTC güçlü yükseliş trendinde, short riskli")
                    if oi_trend_raw > 0:
                        risk_flags.append("⚠️ OI hâlâ artıyor, pozisyon girişleri devam ediyor")
                    if urgent_score < 2:
                        risk_flags.append("⚠️ 15m teyit yetersiz (henüz erken olabilir)")

                    tv_clean = symbol.replace('-SWAP', '').replace('-', '')
                    img_name = f"{symbol}_1h.png"

                    has_photo = take_tradingview_screenshot(tv_clean, img_name)
                    if not has_photo:
                        img_name = "NO_IMAGE"

                    text_summary = (f"Fiyat: {last_p} | 4H Dir: {res_4h} | 1H Dir: {res_1h} | "
                                     f"15m Teyit: {urgent_score}/3 | Divergence: {has_divergence} | "
                                     f"Funding: {funding_raw} | {oi_text} | BTC Trend: {btc_trend_label}")

                    detected_signals.append({
                        "symbol": symbol,
                        "img_path": img_name,
                        "text_data": text_summary,
                        "entry_price": last_p,
                        "rsi": round(rsi, 1),
                        "chg": round(chg, 1),
                        "ratio": ratio,
                        "div": div,
                        "rsi_div_text": rsi_div_text,
                        "has_divergence": has_divergence,
                        "note": note,
                        "candle": candle_text,
                        "urgent": urgent_score,
                        "funding": f"{funding_raw} {funding_warn}".strip(),
                        "funding_raw": funding_raw,
                        "oi_text": oi_text,
                        "oi_trend_raw": oi_trend_raw,
                        "res_4h": res_4h,
                        "res_1h": res_1h,
                        "risk_flags": risk_flags,
                        "btc_trend_label": btc_trend_label,
                        "tv_link": f"https://www.tradingview.com/chart/?symbol=OKX:{tv_clean}.P"
                    })
                    log_signal(detected_signals[-1])
                    time.sleep(0.2)

        except Exception as e:
            print(f"Döngü içi hata yutuldu ({symbol}): {e}")
            continue

    if detected_signals:
        print(f"📢 Tarama bitti. Telegram'a gönderilecek toplam coin sayısı: {len(detected_signals)}")
        try:
            # önce teyit sayısına, sonra RSI'ye göre sırala -> en güvenilir en üstte
            detected_signals.sort(key=lambda x: (x['urgent'], x['rsi']), reverse=True)
        except Exception as e:
            print(f"Sıralama hatası: {e}")

        header = f"🚨 *TEKNİK ALARM VEREN COİNLER* (BTC Trend: {btc_trend_label}) 🚨\n━━━━━━━━━━━━━━━\n"
        list_elements = []
        for s in detected_signals:
            div_text = f" | {s['div']}" if s['div'] else ""
            rsi_div = f"\n  {s['rsi_div_text']}" if s['rsi_div_text'] else ""
            risk_text = ("\n  " + " / ".join(s['risk_flags'])) if s['risk_flags'] else ""
            item_msg = (f"• *{s['symbol']}* | RSI: `{s['rsi']}` | %{s['chg']}\n"
                        f"  👉 {s['note']}{div_text}{rsi_div}\n"
                        f"  ⏳ {s['candle']}\n"
                        f"  💰 Fonlama: {s['funding']} | {s['oi_text']}\n"
                        f"  ⚖️ Hacim Oranı: `{s['ratio']}`{risk_text}\n"
                        f"  🔗 [Grafiği Aç]({s['tv_link']})\n"
                        f"  ━━━━━━━━━━━━━━━")
            list_elements.append(item_msg)

        send_telegram_text(header + "\n".join(list_elements))

        try:
            print("🤖 Gemini grafikleri analiz ediyor...")
            ai_report = analyze_charts_with_gemini(detected_signals, btc_trend_label)

            if ai_report:
                selected_photo = None
                for s in detected_signals:
                    coin_pure = s['symbol'].split('-')[0].upper()
                    if s['img_path'] != "NO_IMAGE" and os.path.exists(s['img_path']):
                        if coin_pure in ai_report.upper():
                            selected_photo = s['img_path']
                            print(f"📸 Gemini analizi için fotoğraf eşleşti: {s['symbol']}")
                            break

                if selected_photo:
                    send_telegram_photo(selected_photo, ai_report)
                else:
                    print("⚠️ Fotoğraf eşleşmedi, analiz düz metin olarak gönderiliyor...")
                    send_telegram_text(ai_report)
        except Exception as e:
            print(f"Gemini aşaması hatası: {e}")

        for s in detected_signals:
            if s['img_path'] != "NO_IMAGE" and os.path.exists(s['img_path']):
                try:
                    os.remove(s['img_path'])
                except:
                    pass
    else:
        print("⏳ Kriterlere uyan hiçbir coin bulunamadı.")


if __name__ == "__main__":
    scan()
