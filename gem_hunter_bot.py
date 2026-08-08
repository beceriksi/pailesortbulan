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
PIVOT_LEN = 2                 # destek/direnç: önce/sonrasındaki 2 muma göre tepe/dip
LOG_FILE = "signals_log.csv"
MEMORY_FILE = "market_memory.json"  # Botun hafıza dosyası
MAX_PENDING_HOURS = 12

# --- Risk yönetimi (R:R hesabı) ---
STOP_BUFFER_PCT = 0.008       # stop, dirençten %0.8 yukarıya konur
MIN_RR = 1.5                  # bu R:R'nin altındaki sinyaller elenir
DEFAULT_TARGET_RR = 2.5       # destek bulunamazsa kullanılacak varsayılan R:R

# --- Günlük rapor ---
DAILY_REPORT_HOUR_UTC = 6
REPORT_STATE_FILE = "daily_report_state.json"

# --- Alım/Satım oran eşiği ---
RATIO_NOTABLE = 1.15          # bu oranın altı "Dengeli" sayılır

# --- Açık pozisyon (short) alıcı baskısı uyarı eşiği ---
BUY_PRESSURE_ALERT_RATIO = 1.3    # bu oranın üzerinde alım fazlası -> uyarı
BUY_PRESSURE_INCREASE_MULT = 1.1  # önceki orana göre %10+ artış -> "artıyor" uyarısı


# =========================================================
# TELEGRAM BİLDİRİM FONKSİYONLARI
# =========================================================
def _split_message_smart(msg, limit=4000):
    lines = msg.split("\n")
    parts = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                parts.append(current)
            if len(line) > limit:
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
    try:
        res = requests.post(url, json={
            "chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }, timeout=10)
        if res.status_code != 200:
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
    _send_single_telegram_message(url, msg)


def send_telegram_photo(photo_path, caption):
    if not (TOKEN and CHAT_ID and photo_path and os.path.exists(photo_path)):
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
# HAFIZA (MEMORY) YÖNETİMİ
# =========================================================
def load_memory():
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Hafıza okuma hatası: {e}")
            return {}
    return {}


def save_memory(memory_data):
    try:
        with open(MEMORY_FILE, "w") as f:
            json.dump(memory_data, f, indent=2)
    except Exception as e:
        print(f"Hafıza kaydetme hatası: {e}")


def update_and_compare_memory(symbol, current_ratio_val, current_direction, current_rsi, current_price):
    memory = load_memory()
    prev_data = memory.get(symbol)

    analysis_note = "🆕 İlk Kez Taranıyor"

    if prev_data:
        prev_dir = prev_data.get("direction", "balanced")
        prev_ratio = prev_data.get("ratio_value", 1.0)
        prev_rsi = prev_data.get("rsi", 0)

        if current_direction == "buy" and (prev_dir != "buy" or current_ratio_val > prev_ratio):
            analysis_note = f"⚠️ *ALIM BASKISI ARTTI!* (Eski: {prev_ratio:.1f}x -> Yeni: {current_ratio_val:.1f}x)"
        elif current_direction == "sell" and (prev_dir != "sell" or current_ratio_val > prev_ratio):
            analysis_note = f"🔥 *SATIM BASKISI ARTTI* (Short Uygun - Eski: {prev_ratio:.1f}x -> Yeni: {current_ratio_val:.1f}x)"
        elif current_direction == "balanced" and prev_dir != "balanced":
            analysis_note = f"⚖️ *Hacim Dengelendi* (Önceki baskı azaldı)"
        else:
            analysis_note = f"➡️ *Akış Stabil* ({current_direction.upper()} {current_ratio_val:.1f}x)"

        if current_rsi > prev_rsi + 3:
            analysis_note += " | 📈 RSI Tırmanıyor"

    memory[symbol] = {
        "direction": current_direction,
        "ratio_value": current_ratio_val,
        "rsi": current_rsi,
        "price": current_price,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    save_memory(memory)

    return analysis_note


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


def to_df_1h(raw):
    df = pd.DataFrame(raw, columns=['ts', 'o', 'h', 'l', 'c', 'v', 'vc', 'vq', 'conf']).iloc[::-1].reset_index(drop=True)
    df[['o', 'h', 'l', 'c', 'v']] = df[['o', 'h', 'l', 'c', 'v']].astype(float)
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

        df = to_df_1h(c_1h)
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
    p_highs, _ = find_custom_sr(df, pivot_len)
    above = [h for h in p_highs if h > current_price]
    if not above:
        return None
    return min(above)


def find_target_support(df, current_price, pivot_len=PIVOT_LEN):
    _, p_lows = find_custom_sr(df, pivot_len)
    below = [l for l in p_lows if l < current_price]
    if not below:
        return None
    return max(below)


def calc_trade_levels(current_price, resistance):
    entry = current_price
    stop = resistance * (1 + STOP_BUFFER_PCT)
    risk = stop - entry
    if risk <= 0:
        return None
    return {"entry": entry, "stop": stop, "risk": risk}


def finalize_target(levels, support):
    risk = levels["risk"]
    entry = levels["entry"]
    if support is not None and support < entry:
        target = support
        rr = (entry - target) / risk
    else:
        rr = DEFAULT_TARGET_RR
        target = entry - risk * DEFAULT_TARGET_RR

    if rr < MIN_RR:
        return None

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

        df_15m = to_df_1h(c_15m)
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
            return f"🚨 Teyitli ({confirmations}/3)", confirmations
        elif confirmations == 1:
            return "⚠️ Zayıf Teyit (1/3)", 1
        else:
            return "⏳ Teyit Yok", 0
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


def get_buy_sell_ratio(symbol):
    try:
        trades = get_data("/api/v5/market/trades", {"instId": symbol, "limit": "100"})
        if not trades:
            return "Veri Yok", 1.0, "balanced"

        buy_vol = sum(float(t['sz']) for t in trades if t.get('side') == 'buy')
        sell_vol = sum(float(t['sz']) for t in trades if t.get('side') == 'sell')

        if buy_vol == 0 and sell_vol == 0:
            return "Hacim Yok", 1.0, "balanced"

        if buy_vol >= sell_vol:
            ratio = buy_vol / sell_vol if sell_vol > 0 else float('inf')
            direction = "buy"
        else:
            ratio = sell_vol / buy_vol if buy_vol > 0 else float('inf')
            direction = "sell"

        if ratio < RATIO_NOTABLE:
            return "Dengeli (~1x)", 1.0, "balanced"

        ratio_display = "10x+" if ratio > 10 else f"{ratio:.1f}x"
        if direction == "buy":
            return f"{ratio_display} Alım Fazla", ratio, "buy"
        else:
            return f"{ratio_display} Satım Fazla", ratio, "sell"
    except Exception as e:
        print(f"Alım/satım oranı hatası ({symbol}): {e}")
        return "Veri Yok", 1.0, "balanced"


# =========================================================
# SİNYAL TAKİP VE GÜNCELLEME SİSTEMİ
# =========================================================
def check_signal_status(symbol, entry_price, stop, target):
    try:
        c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "10"})
        if not c_1h or len(c_1h) < 3:
            return "PENDING", 0

        df = to_df_1h(c_1h)
        if len(df) < 1:
            return "PENDING", 0

        last_closed = df.iloc[-1]
        current_close = float(last_closed['c'])
        current_high = float(last_closed['h'])
        current_low = float(last_closed['l'])

        # Short pozisyonunda kâr/zarar hesabı (fiyat düştükçe kâr artar)
        pct_move = (1 - current_close / entry_price) * 100

        if current_high >= stop:
            return "STOPPED", pct_move
        if current_low <= target:
            return "SUCCESS", pct_move

        return "PENDING", pct_move
    except Exception as e:
        print(f"Sinyal durum kontrolü hatası ({symbol}): {e}")
        return "PENDING", 0


def _is_resolved(val):
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

            status, pct_move = check_signal_status(symbol, entry_price, stop, target)

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
        # Açık pozisyon hacim takibi için başlangıç değerleri
        "last_ratio_value": 1.0,
        "last_ratio_direction": "balanced",
    }
    file_exists = os.path.exists(LOG_FILE)
    df_row = pd.DataFrame([row])
    if file_exists:
        df_row.to_csv(LOG_FILE, mode="a", header=False, index=False)
    else:
        df_row.to_csv(LOG_FILE, mode="w", header=True, index=False)


# =========================================================
# AÇIK POZİSYON HACİM (ALICI BASKISI) İZLEME
# =========================================================
def monitor_open_positions_flow():
    """
    Açık (henüz resolve olmamış) short sinyallerinin anlık alım/satım oranını
    kontrol eder. Bu kontrol, tarama filtrelerinden (RSI, %24s değişim vb.)
    bağımsızdır -- yani coin artık "aday" listesinden düşmüş olsa bile
    pozisyon açık kaldığı sürece izlenmeye devam eder.

    State, signals_log.csv dosyasındaki last_ratio_value / last_ratio_direction
    kolonlarında tutulur -- ayrı bir dosyaya gerek yoktur.
    """
    print("🔎 Açık pozisyonlarda alım/satım akışı kontrol ediliyor...")
    if not os.path.exists(LOG_FILE):
        return

    try:
        df = pd.read_csv(LOG_FILE)
    except Exception as e:
        print(f"Log okuma hatası (monitor): {e}")
        return

    if df.empty:
        return

    # Eski satırlarda bu kolonlar olmayabilir -- yoksa varsayılanla oluştur
    if "last_ratio_value" not in df.columns:
        df["last_ratio_value"] = 1.0
    if "last_ratio_direction" not in df.columns:
        df["last_ratio_direction"] = "balanced"

    updated = False

    for idx, row in df.iterrows():
        if _is_resolved(row.get("resolved")):
            continue

        symbol = row["symbol"]
        try:
            ratio_label, ratio_value, direction = get_buy_sell_ratio(symbol)
        except Exception as e:
            print(f"Akış kontrol hatası ({symbol}): {e}")
            continue

        prev_ratio = row.get("last_ratio_value", 1.0)
        prev_ratio = float(prev_ratio) if pd.notna(prev_ratio) else 1.0
        prev_direction = row.get("last_ratio_direction", "balanced")
        if pd.isna(prev_direction):
            prev_direction = "balanced"

        if direction == "buy" and ratio_value >= BUY_PRESSURE_ALERT_RATIO:
            if prev_direction != "buy":
                send_telegram_text(
                    f"⚠️ *ALICI BASKISI BAŞLADI:* `{symbol}` (açık SHORT pozisyonun)\n"
                    f"Oran: {ratio_value:.1f}x alım fazlası. Pozisyonu gözden geçir."
                )
            elif ratio_value > prev_ratio * BUY_PRESSURE_INCREASE_MULT:
                send_telegram_text(
                    f"🚨 *ALICI BASKISI ARTIYOR:* `{symbol}` (açık SHORT pozisyonun)\n"
                    f"Önceki: {prev_ratio:.1f}x -> Şimdi: {ratio_value:.1f}x. Riskli, dikkatli ol!"
                )

        df.at[idx, "last_ratio_value"] = ratio_value
        df.at[idx, "last_ratio_direction"] = direction
        updated = True

    if updated:
        df.to_csv(LOG_FILE, index=False)


# --- Günlük rapor ---
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

    yesterday_signals = df[df["ts"].dt.date == yesterday]
    ongoing = df[~df["is_resolved"]]
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
        lines.append(f"\n📈 Dün açılan toplam: {total_yesterday} | Başarı oranı: %{win_rate:.0f}")

    return "\n".join(lines)


def maybe_send_daily_report():
    now = datetime.now(timezone.utc)
    if now.hour != DAILY_REPORT_HOUR_UTC:
        return

    state = _load_report_state()
    today_str = now.date().isoformat()
    if state.get("last_report_date") == today_str:
        return

    report = build_daily_report()
    send_telegram_text(report)

    state["last_report_date"] = today_str
    _save_report_state(state)


# =========================================================
# EKRAN GÖRÜNTÜSÜ VE GEMINI ENTEGRASYONU
# =========================================================
def take_tradingview_screenshot(tv_symbol, output_path="chart.png"):
    url = f"https://s.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=OKX:{tv_symbol}.P&interval=60&theme=dark&style=1"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 720})
                page.goto(url, wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(3000)
                page.screenshot(path=output_path)
            finally:
                browser.close()
        return os.path.exists(output_path)
    except Exception as e:
        print(f"Grafik görüntüsü alınamadı ({tv_symbol}): {e}")
        return False


def analyze_top_signal_with_gemini(sig, btc_trend_label):
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
Alım/Satım Oranı: {sig['ratio_label']}
Hafıza/Akış Durumu: {sig['memory_note']}

Sadece şu şablonla, en fazla 3 madde, kısa cümlelerle cevap ver:
🤖 *Gemini Görüşü:* [ONAYLIYORUM / TEREDDÜTLÜYÜM / KATILMIYORUM]
[1-2 kısa cümle gerekçe - varsa tezi zayıflatan bir nokta belirt]
"""
        contents = [prompt]
        img_path = sig.get("img_path")
        if img_path and os.path.exists(img_path):
            with open(img_path, 'rb') as f:
                contents.append(types.Part.from_bytes(data=f.read(), mime_type='image/png'))

        response = client.models.generate_content(model='gemini-2.5-flash', contents=contents)
        return response.text
    except Exception as e:
        print(f"Gemini analiz hatası: {e}")
        return None


# =========================================================
# OKX PİYASA TARAMA DÖNGÜSÜ
# =========================================================
def get_market_candidates():
    print("🌐 OKX Borsasındaki aktif vadeli işlem pariteleri taranıyor...")
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    if not tickers:
        print("⚠️ Ticker verisi borsa API'sinden çekilemedi.")
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

        c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "40"})
        if not c_1h or len(c_1h) < 20:
            continue

        df = to_df_1h(c_1h)
        last_rsi = calc_rsi_series(df['c']).iloc[-1]
        vol_div = check_volume_divergence(df)
        current_price = float(t['last'])

        if last_rsi < RSI_LIMIT and vol_div == "":
            continue

        resistance = find_valid_resistance(df, current_price)
        if resistance is None:
            continue

        levels = calc_trade_levels(current_price, resistance)
        if levels is None:
            continue

        support = find_target_support(df, current_price)
        levels = finalize_target(levels, support)
        if levels is None:
            continue

        candidates.append({
            "symbol": symbol,
            "chg": chg24h,
            "rsi": last_rsi,
            "resistance": resistance,
            "vol_anomaly": vol_div,
            "df_1h": df,
            "current_price": current_price,
            **levels,
        })

    return candidates


def score_candidate(c):
    score = 0
    score += min(c["rsi"] - RSI_LIMIT, 20)
    score += c["rr"] * 5
    if c["vol_anomaly"]:
        score += 5
    if c.get("has_div"):
        score += 8
    if c.get("confirm_count", 0) >= 2:
        score += 10
    if c.get("ratio_direction") == "sell":
        score += min(c.get("ratio_value", 1.0) * 2, 10)

    # Alım baskısı artan coin'leri skorda geriye iterek riski azalt
    if "ALIM BASKISI ARTTI" in c.get("memory_note", ""):
        score -= 15
    return score


# =========================================================
# ANA YÜRÜTÜCÜ (MAINLOOP)
# =========================================================
def main():
    print(f"🚀 Gem Hunter Botu Başlatıldı: {datetime.now(timezone.utc).isoformat()} UTC")

    # 1. Açık sinyalleri, açık pozisyon hacim akışını ve günlük rapor durumunu kontrol et
    update_open_signals()
    monitor_open_positions_flow()
    maybe_send_daily_report()

    btc_label, _ = get_btc_trend()

    # 2. Aday taramasını çalıştır
    candidates = get_market_candidates()
    if not candidates:
        print("📭 Bu tarama döngüsünde kriterlere uyan aday bulunamadı.")
        return

    open_symbols = get_open_symbols()

    # 3. Tüm aday analiz verilerini hazırla ve mesaj bloğunu kur
    msg_lines = [
        "🔍 *PİYASA TARAMA SONUÇLARI*",
        f"🌐 *BTC:* {btc_label}",
        "───────────────────────\n"
    ]

    for coin in candidates:
        symbol = coin["symbol"]
        div_label, has_div = check_rsi_price_divergence(coin["df_1h"])
        trigger_label, confirm_count = check_candle_trigger_15m(symbol)
        funding_raw, fl_label = get_funding_rate(symbol)
        ratio_label, ratio_value, ratio_direction = get_buy_sell_ratio(symbol)

        # Hafıza mekanizmasını çalıştır ve karşılaştırma yap
        memory_note = update_and_compare_memory(
            symbol, ratio_value, ratio_direction, coin["rsi"], coin["current_price"]
        )

        coin.update({
            "div_label": div_label, "has_div": has_div,
            "trigger_label": trigger_label, "confirm_count": confirm_count,
            "funding": funding_raw, "fl_flag": fl_label,
            "ratio_label": ratio_label, "ratio_value": ratio_value, "ratio_direction": ratio_direction,
            "memory_note": memory_note
        })
        coin["score"] = score_candidate(coin)

        vol_label = coin["vol_anomaly"] if coin["vol_anomaly"] else "Yok"
        pozisyon_notu = " (📌 açık pozisyon var)" if symbol in open_symbols else ""

        msg_lines.append(
            f"🚨 *{symbol}*{pozisyon_notu}\n"
            f"24s: %{coin['chg']:.1f} | RSI: {coin['rsi']:.1f}\n"
            f"Entry: {coin['entry']:.5f} | Stop: {coin['stop']:.5f} | Hedef: {coin['target']:.5f} | R:R: {coin['rr']}\n"
            f"15m Teyit: {trigger_label} | Uyumsuzluk: {div_label}\n"
            f"Hacim: {vol_label} | Funding: {funding_raw} {fl_label}\n"
            f"Alım/Satım: {ratio_label}\n"
            f"🧠 *Hafıza & Akış:* {memory_note}\n"
            f"───────────────────────\n"
        )

    # Genel tarama özeti mesajını Telegram'a ilet
    send_telegram_text("\n".join(msg_lines))

    # 4. En yüksek skorlu aday (Alfa Seçimi) tespiti
    fresh_candidates = [c for c in candidates if c["symbol"] not in open_symbols]
    pool = fresh_candidates if fresh_candidates else candidates
    best = max(pool, key=lambda c: c["score"])

    # 5. Yalnızca seçilen ALFA adayı (eğer yeni bir sinyal ise) log dosyasına kaydet
    if best["symbol"] not in open_symbols:
        log_signal(best)

    # 6. Alfa seçimi için grafik alma ve Gemini doğrulaması
    img_path = f"{best['symbol'].split('-')[0]}_chart.png"
    tv_symbol = best['symbol'].replace("-SWAP", "").replace("-", "")

    try:
        got_chart = take_tradingview_screenshot(tv_symbol, img_path)
        best["img_path"] = img_path if got_chart else None

        gemini_report = analyze_top_signal_with_gemini(best, btc_label)

        if gemini_report:
            caption = f"👑 *ALFA SEÇİMİ: {best['symbol']}*\n\n{gemini_report}"
            if best["img_path"]:
                send_telegram_photo(best["img_path"], caption)
            else:
                send_telegram_text(caption)
        elif not GEMINI_API_KEY:
            pass
        else:
            send_telegram_text(f"⚠️ {best['symbol']} için Gemini analizi alınamadı.")

    finally:
        # Görsel dosyasının kalıntısını temizle
        if img_path and os.path.exists(img_path):
            try:
                os.remove(img_path)
            except Exception:
                pass


if __name__ == "__main__":
    main()
