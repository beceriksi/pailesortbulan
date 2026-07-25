import os
import json
import time
import requests
import pandas as pd
from datetime import datetime, timezone
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
OI_STATE_FILE = "oi_state.json"
MAX_PENDING_HOURS = 12

# --- Günlük rapor ayarları (yeni) ---
DAILY_REPORT_HOUR_UTC = 6          # rapor her gün bu UTC saatinde (ilk 15dk içinde) gönderilir
REPORT_STATE_FILE = "daily_report_state.json"

# --- Risk yönetimi ayarları (yeni) ---
STOP_BUFFER_PCT = 0.008     # stop, dirençten %0.8 yukarıya konur
MIN_RR = 1.5                # bu R:R'nin altındaki sinyaller elenir
DEFAULT_TARGET_RR = 2.5     # destek bulunamazsa kullanılacak varsayılan R:R
TOP_N_SIGNALS = 2           # detaylı olarak gönderilecek en iyi sinyal sayısı

# --- Likidite ve squeeze riski (yeni) ---
MIN_24H_VOLUME_USDT = 3_000_000   # bu 24s işlem hacminin altındaki coinler elenir (slippage riski)
SQUEEZE_OI_INCREASE_PCT = 3       # negatif funding + bu oranın üzerinde OI artışı = short squeeze riski
BUYER_DOMINANT_PCT = 15           # net alım baskısı bu değerin üzerindeyse short tezi çelişir -> elenir

# =========================================================
# TELEGRAM BİLDİRİM FONKSİYONLARI
# =========================================================
def _split_message_smart(msg, limit=4000):
    """
    DÜZELTME: Eskiden msg[i:i+4000] ile KÖR bölme yapılıyordu, bu da
    *kalın* / _italik_ gibi Markdown etiketlerini ortadan ikiye bölüp
    Telegram'ın 'can't parse entities' hatasıyla mesajı hiç göndermemesine
    yol açabiliyordu. Şimdi satır sınırlarından bölüyoruz (bir Markdown
    etiketi neredeyse hiçbir zaman satır ortasında bölünmez).
    """
    lines = msg.split("\n")
    parts = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                parts.append(current)
            if len(line) > limit:
                # tek satırın kendisi limiti aşıyorsa (nadir), onu da böl
                for i in range(0, len(line), limit):
                    parts.append(line[i:i + limit])
                current = ""
            else:
                current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _send_single_telegram_message(url, text):
    """Markdown ile dener; entity parse hatası olursa düz metinle tekrar dener."""
    try:
        res = requests.post(url, json={
            "chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=10)
        if res.status_code != 200:
            # Markdown parse hatası (kırık etiket vb.) -> düz metinle tekrar dene
            requests.post(url, json={"chat_id": CHAT_ID, "text": text,
                                      "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        print(f"Telegram metin hatası: {e}")


def send_telegram_text(msg):
    if not (TOKEN and CHAT_ID and msg):
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    if len(msg) > 4000:
        for part in _split_message_smart(msg):
            _send_single_telegram_message(url, part)
            time.sleep(0.5)
        return
    try:
        res = requests.post(url, json={
            "chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=10)
        if res.status_code != 200:
            requests.post(url, json={"chat_id": CHAT_ID, "text": msg,
                                      "disable_web_page_preview": True}, timeout=10)
    except Exception as e:
        print(f"Telegram metin hatası: {e}")


def send_telegram_photo(photo_path, caption):
    if not (TOKEN and CHAT_ID and os.path.exists(photo_path)):
        send_telegram_text(caption)
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    if len(caption) > 1000:
        try:
            with open(photo_path, 'rb') as photo:
                requests.post(url, data={"chat_id": CHAT_ID, "caption": "📸 Analiz Grafiği"},
                              files={"photo": photo}, timeout=15)
            time.sleep(1)
            send_telegram_text(caption)
        except Exception as e:
            print(f"Fotoğraf/Metin split hatası: {e}")
            send_telegram_text(caption)
        return
    try:
        with open(photo_path, 'rb') as photo:
            res = requests.post(url, data={"chat_id": CHAT_ID, "caption": caption,
                                            "parse_mode": "Markdown"},
                                 files={"photo": photo}, timeout=15)
        if res.status_code != 200:
            send_telegram_text(caption)
    except Exception as e:
        print(f"Telegram fotoğraf hatası: {e}")
        send_telegram_text(caption)


# =========================================================
# OKX DATA SAĞLAYICI
# =========================================================
def get_data(endpoint, params={}):
    base = "https://www.okx.com"
    try:
        res = requests.get(base + endpoint, params=params, timeout=10).json()
        return res.get('data', [])
    except Exception:
        return []


def to_df_1h(raw, limit_used):
    """
    DÜZELTME: OKX'in 'conf' alanı mumun kapanıp kapanmadığını gösterir
    ('0' = hâlâ oluşuyor, '1' = kapandı). Eskiden bu hiç kontrol edilmiyordu,
    yani hâlâ değişebilen canlı mum pivot/RSI hesaplarına karışıyordu.
    Burada onaylanmamış son mumu veri setinden atıyoruz; tüm analizler
    sadece KAPANMIŞ mumlar üzerinden yapılıyor.
    """
    df = pd.DataFrame(raw, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
    df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)

    # En sondaki mum hâlâ oluşuyorsa (conf == '0'), at.
    if len(df) > 0 and str(df.iloc[-1]['conf']) == '0':
        df = df.iloc[:-1].reset_index(drop=True)

    return df


# =========================================================
# TEKNİK ANALİZ MODÜLLERİ VE FİLTRELER
# =========================================================
def get_btc_trend():
    try:
        c_1h = get_data("/api/v5/market/candles", {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": "60"})
        if not c_1h or len(c_1h) < 55:
            return "BILINMIYOR", 0

        df = to_df_1h(c_1h, 60)
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


def calc_rsi_series(close):
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, 0.0001)
    return 100 - (100 / (1 + rs))


def check_rsi_price_divergence(df):
    """
    DÜZELTME: Eskiden df ve rsi_series'ten AYRI AYRI son 20 satır alınıyordu.
    rolling(14) yüzünden RSI'nin ilk ~14 değeri NaN olduğundan, gelen mum
    sayısı beklenenden azsa bu NaN'lar pencerenin içine sızıp sessizce
    yanlış (veya kaçırılmış) uyumsuzluk sonucuna yol açabiliyordu.
    Artık fiyat ve RSI aynı tabloda tutuluyor, NaN satırlar tamamen
    atılıyor, böylece pencere her zaman geçerli (dolu) veriden oluşuyor.
    """
    try:
        if df is None or len(df) < 30:
            return "Veri Yetersiz", False

        work = df.copy()
        work['rsi'] = calc_rsi_series(work['c'])
        work = work.dropna(subset=['rsi']).reset_index(drop=True)

        if len(work) < 20:
            return "Veri Yetersiz", False

        window = work.iloc[-20:].reset_index(drop=True)
        h = window['h'].values
        rsi_vals = window['rsi'].values

        highs_idx = [i for i in range(2, len(h) - 2)
                     if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]]

        if len(highs_idx) < 2:
            return "Uyumsuzluk Yok", False

        i1, i2 = highs_idx[-2], highs_idx[-1]
        price1, price2 = h[i1], h[i2]
        rsi1, rsi2 = rsi_vals[i1], rsi_vals[i2]

        if price2 > price1 and rsi2 < rsi1:
            return "🔻 RSI AYI UYUMSUZLUĞU", True
        return "Uyumsuzluk Yok", False
    except Exception as e:
        print(f"Divergence hesap hatası: {e}")
        return "Hesaplama Hatası", False


def find_custom_sr(df, pivot_len=PIVOT_LEN):
    """Onaylanmış pivot high/low FİYAT listelerini döndürür (index değil)."""
    if len(df) < (pivot_len * 2 + 1):
        return [], []

    highs = df['h'].values
    lows = df['l'].values
    p_highs, p_lows = [], []

    for i in range(pivot_len, len(highs) - pivot_len):
        if all(highs[i] > highs[i - j] for j in range(1, pivot_len + 1)) and \
           all(highs[i] > highs[i + j] for j in range(1, pivot_len + 1)):
            p_highs.append(highs[i])
        if all(lows[i] < lows[i - j] for j in range(1, pivot_len + 1)) and \
           all(lows[i] < lows[i + j] for j in range(1, pivot_len + 1)):
            p_lows.append(lows[i])

    return p_highs, p_lows


def find_valid_resistance(df, current_price, pivot_len=PIVOT_LEN):
    """
    DÜZELTME: Eskiden 'res_1h' fiyatın altında bile olabiliyordu (kırılmış direnç).
    Artık SADECE fiyatın ÜZERİNDE kalan, henüz kırılmamış en yakın pivotu döner.
    Böyle bir seviye yoksa None döner -> sinyal reddedilir.
    """
    p_highs, _ = find_custom_sr(df, pivot_len)
    above = [h for h in p_highs if h > current_price]
    if not above:
        return None
    return min(above)  # fiyata en yakın olan üst direnç


def find_target_support(df, current_price, pivot_len=PIVOT_LEN):
    """Fiyatın ALTINDA kalan en yakın pivot low'u hedef (TP) adayı olarak döner."""
    _, p_lows = find_custom_sr(df, pivot_len)
    below = [l for l in p_lows if l < current_price]
    if not below:
        return None
    return max(below)  # fiyata en yakın olan alt destek


def calc_trade_levels(current_price, resistance):
    """
    Entry / Stop / Target / R:R hesaplar.
    Stop: dirençten STOP_BUFFER_PCT kadar yukarı (yapı kırılırsa çık).
    Target: varsa en yakın destek, yoksa DEFAULT_TARGET_RR ile hesaplanan seviye.
    """
    entry = current_price
    stop = resistance * (1 + STOP_BUFFER_PCT)
    risk = stop - entry

    if risk <= 0:
        return None  # entry zaten stop seviyesinin üstünde -> geçersiz

    return {
        "entry": entry,
        "stop": stop,
        "risk": risk,
    }


def finalize_target(levels, support):
    """
    NOT: rr < MIN_RR kontrolü burada da yapılıyor (çağıran yerde zaten var,
    ama fonksiyon başka bir yerden çağrılırsa da güvenlik altında olsun diye
    ikinci bir savunma katmanı). rr yetersizse levels None döner.
    """
    risk = levels["risk"]
    entry = levels["entry"]
    if support is not None and support < entry:
        target = support
        rr = (entry - target) / risk
    else:
        rr = DEFAULT_TARGET_RR
        target = entry - risk * DEFAULT_TARGET_RR

    if rr < MIN_RR:
        return None  # destek girişe çok yakın -> kalitesiz R:R, sinyal geçersiz

    levels["target"] = target
    levels["rr"] = round(rr, 2)
    return levels


def check_volume_divergence(df):
    if len(df) < 5:
        return ""
    p = df['c'].iloc[-5:].values
    v = df['v'].iloc[-5:].values
    if p[-1] > p[0] and v[-1] < v[-2]:
        return "⚠️ Hacimsiz Yükseliş"
    return ""


def check_candle_trigger_15m(symbol):
    try:
        c_15m = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "15m", "limit": "6"})
        if not c_15m or len(c_15m) < 4:
            return "Veri Alınamadı", 0

        df_15m = pd.DataFrame(c_15m, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
        df_15m[['o', 'h', 'l', 'c']] = df_15m[['o', 'h', 'l', 'c']].astype(float)

        # Onaylanmamış (hâlâ oluşan) son mumu at - kapanmamış mum yanlış sinyal üretebilir
        if len(df_15m) > 0 and str(df_15m.iloc[-1]['conf']) == '0':
            df_15m = df_15m.iloc[:-1].reset_index(drop=True)

        if len(df_15m) < 4:
            return "Veri Alınamadı", 0

        last3 = df_15m.iloc[-3:].reset_index(drop=True)
        o, h, l, c = last3.iloc[-1][['o', 'h', 'l', 'c']]

        body = abs(c - o)
        upper_wick = (h - max(o, c)) if h > max(o, c) else 0
        last_candle_bearish = (c < o) or (upper_wick > body * 0.8)
        lower_high = last3.iloc[-1]['h'] < last3.iloc[-2]['h']
        closes_weakening = last3.iloc[-1]['c'] < last3.iloc[-2]['c']

        confirmations = sum([last_candle_bearish, lower_high, closes_weakening])
        if confirmations >= 2:
            return f"Teyitli ({confirmations}/3)", confirmations
        elif confirmations == 1:
            return "Zayıf Teyit (1/3)", 1
        else:
            return "Teyit Yok", 0
    except Exception as e:
        print(f"15m tetik hatası: {e}")
        return "Durum Okunamadı", 0


def get_funding_rate(symbol):
    res = get_data("/api/v5/public/funding-rate", {"instId": symbol})
    if res:
        rate_pct = float(res[0].get('fundingRate', 0)) * 100
        if rate_pct < -0.05:
            return f"%{rate_pct:.3f}", "⚠️ Aşırı negatif FL (short sıkışma riski)"
        elif rate_pct > 0.08:
            return f"%{rate_pct:.3f}", "🔥 Aşırı pozitif FL"
        return f"%{rate_pct:.3f}", ""
    return "N/A", ""


# --- OI trendi: gerçek zamanlı karşılaştırma için basit local state ---
def load_oi_state():
    if os.path.exists(OI_STATE_FILE):
        try:
            with open(OI_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_oi_state(state):
    try:
        with open(OI_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"OI state kayıt hatası: {e}")


def get_oi_trend(symbol, oi_state):
    """
    DÜZELTME: Eskiden sabit '>100000' eşiği ile anlamsız bir yorum üretiyordu.
    Artık bir önceki tarama döngüsündeki OI değeriyle KIYASLIYOR (gerçek trend).
    """
    try:
        res = get_data("/api/v5/public/open-interest", {"instId": symbol})
        if not res:
            return "OI Alınamadı", 0

        oi_now = float(res[0].get('oi', 0))
        oi_prev = oi_state.get(symbol)
        oi_state[symbol] = oi_now

        if oi_prev is None or oi_prev == 0:
            return f"{oi_now:.0f} (ilk ölçüm)", oi_now

        change_pct = (oi_now / oi_prev - 1) * 100
        if change_pct > 3:
            return f"{oi_now:.0f} (OI +%{change_pct:.1f} artıyor)", oi_now
        elif change_pct < -3:
            return f"{oi_now:.0f} (OI -%{abs(change_pct):.1f} azalıyor)", oi_now
        else:
            return f"{oi_now:.0f} (stabil)", oi_now
    except Exception as e:
        print(f"OI hatası ({symbol}): {e}")
        return "OI Alınamadı", 0


def get_net_buying_pressure(symbol):
    """
    DÜZELTME: Eskiden /rubik/stat/taker-volume endpoint'i SWAP sembolleri için
    veri döndürmüyordu ('Veri Yok'). Artık son trade'lerden (public/trades)
    doğrudan alış/satış hacmi hesaplanıyor.
    """
    try:
        trades = get_data("/api/v5/market/trades", {"instId": symbol, "limit": "500"})
        if not trades:
            return "Veri Yok", 0

        buy_vol = sum(float(t['sz']) for t in trades if t.get('side') == 'buy')
        sell_vol = sum(float(t['sz']) for t in trades if t.get('side') == 'sell')
        total = buy_vol + sell_vol
        if total == 0:
            return "Hacim Yok", 0

        net_pct = ((buy_vol - sell_vol) / total) * 100
        if net_pct > BUYER_DOMINANT_PCT:
            return f"+%{net_pct:.1f} Alıcı baskın", net_pct
        elif net_pct < -BUYER_DOMINANT_PCT:
            return f"%{net_pct:.1f} Satıcı baskın", net_pct
        return f"%{net_pct:.1f} Dengeli", net_pct
    except Exception as e:
        print(f"Alım baskısı hatası ({symbol}): {e}")
        return "Veri Yok", 0


# =========================================================
# SİNYAL TAKİP VE GÜNCELLEME SİSTEMİ
# =========================================================
def check_signal_status(symbol, entry_price, stop, target, entry_rsi):
    try:
        c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "10"})
        if not c_1h or len(c_1h) < 3:
            return "PENDING", 0

        df = to_df_1h(c_1h, 10)
        if len(df) < 1:
            return "PENDING", 0
        last_closed = df.iloc[-1]  # to_df_1h zaten onaylanmamış mumu attı
        current_close = float(last_closed['c'])
        current_high = float(last_closed['h'])
        current_low = float(last_closed['l'])
        pct_move = (current_close / entry_price - 1) * 100

        # Stop seviyesine mumun İÇİNDE değdiyse (sadece kapanışa değil, high'a bak)
        if current_high >= stop:
            return "STOPPED", pct_move

        # Hedefe mumun düşüğü ulaştıysa
        if current_low <= target:
            return "SUCCESS", pct_move

        return "PENDING", pct_move
    except Exception as e:
        print(f"Sinyal durum kontrolü hatası ({symbol}): {e}")
        return "PENDING", 0


def _is_resolved(val):
    """
    KRİTİK DÜZELTME: signals_log.csv'ye 'TRUE'/'FALSE' metni olarak
    yazılıyor, ama pandas CSV'yi tekrar okurken bu sütunu otomatik olarak
    Python bool'una çeviriyor (True/False). Eski kod hâlâ str(val)=='TRUE'
    diye metinle karşılaştırıyordu -> bu KARŞILAŞTIRMA HİÇBİR ZAMAN True
    OLMUYORDU, yani zaten kapanmış (TP/STOP olmuş) sinyaller her çalıştırmada
    'hâlâ açık' sanılıp durmadan tekrar tekrar bildirim gönderiyordu.
    Bu fonksiyon hem bool hem string hâlini doğru tanır.
    """
    if isinstance(val, bool):
        return val
    return str(val).strip().upper() == "TRUE"


def update_open_signals():
    print("⏳ Açık sinyaller kontrol ediliyor...")
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
        if _is_resolved(row.get("resolved")):
            continue
        try:
            symbol = row["symbol"]
            entry_price = float(row["entry_price"])
            stop = float(row["stop"])
            target = float(row["target"])
            entry_rsi = float(row["rsi"])

            status, pct_move = check_signal_status(symbol, entry_price, stop, target, entry_rsi)

            ts = pd.to_datetime(row["timestamp_utc"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            hours_passed = (now - ts).total_seconds() / 3600

            if status == "STOPPED":
                df.at[idx, "outcome"] = "STOP OLDU"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(f"🛑 *STOP:* {symbol} stop seviyesine ulaştı (%{pct_move:.1f}).")
            elif status == "SUCCESS":
                df.at[idx, "outcome"] = "HEDEF TUTTU"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(f"💰 *HEDEF:* {symbol} hedef seviyesine ulaştı (%{pct_move:.1f}).")
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


def get_open_symbols():
    """
    YENİ: CSV'de 'resolved' = FALSE olan (yani hâlâ sonuçlanmamış) sembolleri
    döndürür. main() bu setle, açık pozisyonu olan coinler için tekrar
    'yeni giriş' sinyali göndermeyi engeller.
    """
    if not os.path.exists(LOG_FILE):
        return set()
    try:
        df = pd.read_csv(LOG_FILE)
    except Exception:
        return set()
    if df.empty:
        return set()
    open_rows = df[~df["resolved"].apply(_is_resolved)]
    return set(open_rows["symbol"].tolist())


def _load_report_state():
    if os.path.exists(REPORT_STATE_FILE):
        try:
            with open(REPORT_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_report_state(state):
    try:
        with open(REPORT_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Rapor state kayıt hatası: {e}")


def build_daily_report():
    """
    YENİ: Dünkü (UTC) verilen sinyalleri, hâlâ devam edenleri, hedefe
    ulaşanları ve stop olanları özetleyen günlük rapor metni üretir.
    """
    if not os.path.exists(LOG_FILE):
        return "📊 *GÜNLÜK RAPOR*\nHenüz kayıtlı sinyal yok."

    try:
        df = pd.read_csv(LOG_FILE)
    except Exception as e:
        return f"📊 *GÜNLÜK RAPOR*\nLog okuma hatası: {e}"

    if df.empty:
        return "📊 *GÜNLÜK RAPOR*\nHenüz kayıtlı sinyal yok."

    now = datetime.now(timezone.utc)
    yesterday = (now - pd.Timedelta(days=1)).date()

    df["ts"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
    df["is_resolved"] = df["resolved"].apply(_is_resolved)

    # Dün açılmış sinyaller (tarihine göre)
    yesterday_signals = df[df["ts"].dt.date == yesterday]
    # Hâlâ devam eden TÜM sinyaller (ne zaman açıldığına bakılmaksızın)
    ongoing = df[~df["is_resolved"]]
    # Dün SONUÇLANMIŞ (TP/STOP/zaman aşımı) sinyaller
    yesterday_resolved = yesterday_signals[yesterday_signals["is_resolved"]]

    tp_rows = yesterday_resolved[yesterday_resolved["outcome"] == "HEDEF TUTTU"]
    stop_rows = yesterday_resolved[yesterday_resolved["outcome"] == "STOP OLDU"]
    timeout_rows = yesterday_resolved[yesterday_resolved["outcome"] == "ZAMAN ASIMI"]

    lines = [f"📊 *GÜNLÜK RAPOR* — {yesterday.strftime('%d.%m.%Y')}\n"]

    lines.append(f"🟢 *Devam Eden ({len(ongoing)}):*")
    if len(ongoing) == 0:
        lines.append("_yok_")
    else:
        for _, r in ongoing.iterrows():
            lines.append(f"• `{r['symbol']}` entry: {r['entry_price']:.5f} R:R: {r.get('rr', '-')}")

    lines.append(f"\n✅ *Hedef Tuttu, Dün ({len(tp_rows)}):*")
    if len(tp_rows) == 0:
        lines.append("_yok_")
    else:
        for _, r in tp_rows.iterrows():
            lines.append(f"• `{r['symbol']}` %{r['outcome_pct']:.1f}")

    lines.append(f"\n🛑 *Stop Oldu, Dün ({len(stop_rows)}):*")
    if len(stop_rows) == 0:
        lines.append("_yok_")
    else:
        for _, r in stop_rows.iterrows():
            lines.append(f"• `{r['symbol']}` %{r['outcome_pct']:.1f}")

    if len(timeout_rows) > 0:
        lines.append(f"\n⏳ *Zaman Aşımı, Dün ({len(timeout_rows)}):*")
        for _, r in timeout_rows.iterrows():
            lines.append(f"• `{r['symbol']}`")

    total_yesterday = len(yesterday_signals)
    win_count = len(tp_rows)
    resolved_count = len(yesterday_resolved)
    if resolved_count > 0:
        win_rate = (win_count / resolved_count) * 100
        lines.append(f"\n📈 Dün açılan toplam: {total_yesterday} | Başarı oranı (sonuçlananlar içinde): %{win_rate:.0f}")

    return "\n".join(lines)


def maybe_send_daily_report():
    """
    YENİ: Günde SADECE BİR KEZ (DAILY_REPORT_HOUR_UTC saatinin ilk 15
    dakikasında) günlük raporu gönderir. daily_report_state.json ile
    aynı gün içinde ikinci kez gönderilmesi engellenir.
    """
    now = datetime.now(timezone.utc)
    if now.hour != DAILY_REPORT_HOUR_UTC:
        return

    state = _load_report_state()
    today_str = now.date().isoformat()
    if state.get("last_report_date") == today_str:
        return  # bugün zaten gönderildi

    report = build_daily_report()
    send_telegram_text(report)

    state["last_report_date"] = today_str
    _save_report_state(state)


def log_signal(sig):
    row = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "symbol": sig["symbol"],
        "entry_price": sig["entry"],
        "stop": sig["stop"],
        "target": sig["target"],
        "rr": sig["rr"],
        "rsi": sig["rsi"],
        "resolved": "FALSE",
        "outcome": "",
        "outcome_pct": "",
    }
    file_exists = os.path.exists(LOG_FILE)
    df_row = pd.DataFrame([row])
    if file_exists:
        df_row.to_csv(LOG_FILE, mode="a", header=False, index=False)
    else:
        df_row.to_csv(LOG_FILE, mode="w", header=True, index=False)


# =========================================================
# EKRAN GÖRÜNTÜSÜ VE GEMINI ENTEGRASYONU (sadece TOP sinyal için)
# =========================================================
def take_tradingview_screenshot(tv_symbol, output_path="chart.png"):
    """
    tv_symbol TradingView/OKX perpetual formatında olmalı (örn. 'BASEDUSDT',
    tiresiz). Çağıran taraf OKX instId'sini (örn. 'BASED-USDT-SWAP') bu
    formata çevirmekten sorumlu - bkz. main() içindeki dönüşüm.
    """
    url = f"https://s.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=OKX:{tv_symbol}.P&interval=60&theme=dark&style=1"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 720})
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(5000)
            page.screenshot(path=output_path)
            browser.close()
        return True
    except Exception as e:
        print(f"Grafik görüntüsü alınamadı ({tv_symbol}): {e}")
        return False


def analyze_top_signal_with_gemini(sig, btc_trend_label):
    """
    DÜZELTME: Eskiden prompt Gemini'ye short tezini önceden kabul ettirip
    'destekleyen 2-3 madde' istiyordu -> bu, onay yanlılığı (confirmation
    bias) yaratıyordu, Gemini hiçbir zaman 'bu short mantıksız' diyemiyordu.
    Şimdi tezi SAVUNMASI değil, BAĞIMSIZ değerlendirmesi isteniyor; açıkça
    katılmama/şüpheci olma seçeneği tanınıyor.
    """
    if not GEMINI_API_KEY:
        return None
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = f"""
Sen bağımsız, tarafsız bir teknik analiz botusun. Sana bir short (satış) tezi
SUNULUYOR ama bu tezi savunmak ZORUNDA DEĞİLSİN. Veriyi ve grafiği kendi
başına değerlendir; tez zayıfsa açıkça karşı çık.

BTC Trendi: {btc_trend_label}
Coin: {sig['symbol']}
Önerilen Short - Entry: {sig['entry']:.5f} | Stop: {sig['stop']:.5f} | Hedef: {sig['target']:.5f} | R:R: {sig['rr']}
RSI: {sig['rsi']:.1f} | Funding: {sig['funding']} {sig['fl_flag']}
OI: {sig['oi_label']} | Alım/Satım: {sig['pressure_label']}

Sadece şu şablonla, en fazla 3 madde, kısa cümlelerle cevap ver:
🤖 *Gemini Görüşü:* [ONAYLIYORUM / TEREDDÜTLÜYÜM / KATILMIYORUM]
[1-2 kısa cümle gerekçe - varsa tezi zayıflatan bir nokta belirt]
"""
        contents = [prompt]
        if os.path.exists(sig.get("img_path", "")):
            with open(sig["img_path"], 'rb') as f:
                contents.append(types.Part.from_bytes(data=f.read(), mime_type='image/png'))

        response = client.models.generate_content(model='gemini-2.5-flash', contents=contents)
        return response.text
    except Exception as e:
        print(f"Gemini analiz hatası: {e}")
        return None



# =========================================================
# PİYASA TARAMA DÖNGÜSÜ
# =========================================================
def get_market_candidates():
    print("🌐 OKX vadeli işlem pariteleri taranıyor...")
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    if not tickers:
        print("⚠️ Ticker verisi alınamadı.")
        return []

    usdt_swaps = [t for t in tickers if t['instId'].endswith("-USDT-SWAP")]
    candidates = []

    for t in usdt_swaps:
        symbol = t['instId']
        try:
            chg24h = ((float(t['last']) / float(t['open24h'])) - 1) * 100
        except Exception:
            continue

        if chg24h < CHANGE_24H_LIMIT:
            continue

        # --- YENİ: likidite filtresi - düşük hacimli coinlerde slippage riski yüksek ---
        try:
            vol_24h_usdt = float(t.get('volCcy24h', 0))
        except Exception:
            vol_24h_usdt = 0
        if vol_24h_usdt < MIN_24H_VOLUME_USDT:
            continue

        c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "45"})
        if not c_1h or len(c_1h) < 20:
            continue

        df = to_df_1h(c_1h, 35)
        last_rsi = calc_rsi_series(df['c']).iloc[-1]
        vol_div = check_volume_divergence(df)
        current_price = float(t['last'])

        if last_rsi < RSI_LIMIT and vol_div == "":
            continue

        # --- DÜZELTME: sadece fiyatın ÜZERİNDE kalan, kırılmamış direnç kabul edilir ---
        resistance = find_valid_resistance(df, current_price)
        if resistance is None:
            continue  # direnç zaten kırılmış -> geçersiz short adayı, atla

        levels = calc_trade_levels(current_price, resistance)
        if levels is None:
            continue

        support = find_target_support(df, current_price)
        levels = finalize_target(levels, support)
        if levels is None:
            continue  # R:R yetersiz (MIN_RR altında) -> kalitesiz sinyal, atla

        candidates.append({
            "symbol": symbol,
            "chg": chg24h,
            "rsi": last_rsi,
            "resistance": resistance,
            "vol_anomaly": vol_div,
            "vol_24h_usdt": vol_24h_usdt,
            "df_1h": df,
            **levels,
        })

    return candidates


def score_candidate(c):
    """Basit, kural tabanlı skorlama -> en iyi sinyali seçmek için."""
    score = 0
    score += min(c["rsi"] - RSI_LIMIT, 20)          # ne kadar aşırı alım o kadar puan (üst sınır 20)
    score += c["rr"] * 5                             # R:R yüksekse puan yüksek
    if c["vol_anomaly"]:
        score += 5
    if c.get("has_div"):
        score += 8
    if c.get("confirm_count", 0) >= 2:
        score += 10
    # Satıcı baskınsa (pressure_pct negatifse) short tezini destekler -> puan ekle
    pressure_pct = c.get("pressure_pct", 0)
    if pressure_pct < 0:
        score += min(abs(pressure_pct) * 0.3, 10)
    return score


# =========================================================
# ANA YÜRÜTÜCÜ (MAINLOOP)
# =========================================================
def main():
    print(f"🚀 Bot Başlatıldı: {datetime.now(timezone.utc).isoformat()} UTC")

    update_open_signals()
    maybe_send_daily_report()
    btc_label, _ = get_btc_trend()
    oi_state = load_oi_state()

    candidates = get_market_candidates()
    if not candidates:
        print("📭 Kriterlere uyan aday bulunamadı.")
        return

    # Ek verileri doldur ve skorla
    for c in candidates:
        symbol = c["symbol"]
        div_label, has_div = check_rsi_price_divergence(c["df_1h"])
        trigger_label, confirm_count = check_candle_trigger_15m(symbol)
        funding_raw, fl_flag = get_funding_rate(symbol)
        oi_label, _ = get_oi_trend(symbol, oi_state)
        pressure_label, pressure_pct = get_net_buying_pressure(symbol)

        # --- YENİ: negatif funding + artan OI = short squeeze riski -> elenir ---
        is_squeeze_risk = ("Short Riski" in fl_flag) and ("artıyor" in oi_label)
        # --- YENİ: alıcı baskınsa short tezi zaten çelişiyor demektir -> elenir ---
        is_buyer_dominant = pressure_pct > BUYER_DOMINANT_PCT

        c.update({
            "div_label": div_label, "has_div": has_div,
            "trigger_label": trigger_label, "confirm_count": confirm_count,
            "funding": funding_raw, "fl_flag": fl_flag,
            "oi_label": oi_label, "pressure_label": pressure_label, "pressure_pct": pressure_pct,
            "squeeze_risk": is_squeeze_risk, "buyer_dominant": is_buyer_dominant,
        })
        c["score"] = score_candidate(c)

    save_oi_state(oi_state)
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # --- YENİ: hâlihazırda açık (sonuçlanmamış) pozisyonu olan coinleri öğren ---
    open_symbols = get_open_symbols()

    # --- SADE ÖZET LİSTESİ: her aday tek satır ---
    lines = [f"🔍 *TARAMA* — BTC: `{btc_label}` — {len(candidates)} aday\n"]
    for c in candidates:
        tags = ""
        if c["has_div"]:
            tags += "🔻"
        if c["confirm_count"] >= 2:
            tags += "🚨"
        if c["squeeze_risk"]:
            tags += "⚠️SQUEEZE"
        if c["buyer_dominant"]:
            tags += "⚠️ALICI BASKIN"
        if c["symbol"] in open_symbols:
            tags += " (📌 açık pozisyon var, yeni giriş DEĞİL)"
        lines.append(
            f"• `{c['symbol']}` RSI:{c['rsi']:.0f} 24s:%{c['chg']:.1f} "
            f"R:R {c['rr']} Al/Sat:%{c['pressure_pct']:.0f} {tags}"
        )
    send_telegram_text("\n".join(lines))

    # --- DETAYLI BLOK: sadece en iyi TOP_N sinyal ---
    # Açık pozisyonu olan, squeeze riski taşıyan veya alıcı baskın (short
    # tezini çürüten) coinler yeni giriş önerisi olarak GÖNDERİLMEZ.
    eligible = [c for c in candidates
                if c["symbol"] not in open_symbols
                and not c["squeeze_risk"]
                and not c["buyer_dominant"]]
    top_signals = eligible[:TOP_N_SIGNALS]

    for sig in top_signals:
        img_path = f"{sig['symbol'].split('-')[0]}_chart.png"
        # DÜZELTME: 'BASED-USDT-SWAP' -> 'BASEDUSDT' (TradingView tiresiz format bekliyor)
        tv_symbol = sig['symbol'].replace("-SWAP", "").replace("-", "")
        got_chart = take_tradingview_screenshot(tv_symbol, img_path)
        sig["img_path"] = img_path

        detail = (
            f"👑 *SİNYAL: {sig['symbol']}*\n"
            f"───────────────────────\n"
            f"🎯 Entry: `{sig['entry']:.5f}`\n"
            f"🛑 Stop: `{sig['stop']:.5f}` (direnç: {sig['resistance']:.5f})\n"
            f"✅ Hedef: `{sig['target']:.5f}`\n"
            f"⚖️ R:R: `{sig['rr']}`\n"
            f"📊 RSI: `{sig['rsi']:.1f}` | 24s: `%{sig['chg']:.1f}` | Hacim: `${sig['vol_24h_usdt']:,.0f}`\n"
            f"📈 Uyumsuzluk: {sig['div_label']}\n"
            f"🕯️ 15m Teyit: {sig['trigger_label']}\n"
            f"💵 Funding: {sig['funding']} {sig['fl_flag']}\n"
            f"🧮 OI: {sig['oi_label']}\n"
            f"⚡ Alım/Satım: {sig['pressure_label']}\n"
        )

        gemini_note = analyze_top_signal_with_gemini(sig, btc_label)
        if gemini_note:
            detail += f"\n{gemini_note}"

        if got_chart:
            send_telegram_photo(img_path, detail)
        else:
            send_telegram_text(detail)

        log_signal(sig)


if __name__ == "__main__":
    main()
