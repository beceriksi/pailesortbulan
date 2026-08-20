import csv
from datetime import datetime, timezone
import os
import time
from google import genai
from google.genai import types
import pandas as pd
from PIL import Image
from playwright.sync_api import sync_playwright
import requests

# Yeni Destek/Direnç Analiz Modülü Import Edildi
from sr_detector import SRDetector

# =========================================================
# GLOBAL AYARLAR VE ENV / SECRETS
# =========================================================
TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

RSI_LIMIT = 70
CHANGE_24H_LIMIT = 8
PIVOT_LEN = 4
LOG_FILE = 'signals_log.csv'
MAX_PENDING_HOURS = 12

# HTTP Bağlantı Yeniden Kullanımı İçin Session
session = requests.Session()

# Destek/Direnç Dedektör Nesnesi
sr_detector = SRDetector(
    pivot_len=8,
    max_pivots=12,
    breakout_buf_mult=0.15,
    tol_mult=0.15,
    near_mult=0.50,
    line_mode='Çok Dokunulan',
    show_horizontal=True,
    lookback_bars=300,
)


# =========================================================
# TELEGRAM BİLDİRİM FONKSİYONLARI
# =========================================================
def send_telegram_text(msg):
  if not (TOKEN and CHAT_ID and msg):
    return

  url = f'https://api.telegram.org/bot{TOKEN}/sendMessage'
  if len(msg) > 4000:
    parts = [msg[i : i + 4000] for i in range(0, len(msg), 4000)]
    for part in parts:
      try:
        session.post(
            url,
            json={
                'chat_id': CHAT_ID,
                'text': part,
                'parse_mode': 'Markdown',
                'disable_web_page_preview': True,
            },
            timeout=10,
        )
        time.sleep(0.5)
      except Exception as e:
        print(f'Parçalı Telegram metin hatası: {e}')
    return

  try:
    res = session.post(
        url,
        json={
            'chat_id': CHAT_ID,
            'text': msg,
            'parse_mode': 'Markdown',
            'disable_web_page_preview': True,
        },
        timeout=10,
    )
    if res.status_code != 200:
      session.post(
          url,
          json={
              'chat_id': CHAT_ID,
              'text': msg,
              'disable_web_page_preview': True,
          },
          timeout=10,
      )
  except Exception as e:
    print(f'Telegram metin hatası: {e}')


def send_telegram_photo(photo_path, caption):
  if not (TOKEN and CHAT_ID and os.path.exists(photo_path)):
    return

  url = f'https://api.telegram.org/bot{TOKEN}/sendPhoto'
  if len(caption) > 1000:
    try:
      with open(photo_path, 'rb') as photo:
        session.post(
            url,
            data={
                'chat_id': CHAT_ID,
                'caption': '📸 GEMINI ALFA ANALİZ GRAFİĞİ',
            },
            files={'photo': photo},
            timeout=15,
        )
      time.sleep(1)
      send_telegram_text(caption)
    except Exception as e:
      print(f'Fotoğraf/Metin split hatası: {e}')
      send_telegram_text(caption)
    return

  try:
    with open(photo_path, 'rb') as photo:
      res = session.post(
          url,
          data={
              'chat_id': CHAT_ID,
              'caption': caption,
              'parse_mode': 'Markdown',
          },
          files={'photo': photo},
          timeout=15,
      )
    if res.status_code != 200:
      send_telegram_text(caption)
  except Exception as e:
    print(f'Telegram fotoğraf hatası: {e}')
    send_telegram_text(caption)


# =========================================================
# OKX DATA SAĞLAYICI
# =========================================================
def get_data(endpoint, params=None):
  if params is None:
    params = {}
  base = 'https://www.okx.com'
  try:
    res = session.get(base + endpoint, params=params, timeout=10).json()
    return res.get('data', [])
  except Exception as e:
    print(f'OKX API İsteği Hatası [{endpoint}]: {e}')
    return []


# =========================================================
# TEKNİK ANALİZ MODÜLLERİ VE FİLTRELER
# =========================================================
def get_btc_trend():
  try:
    c_1h = get_data(
        '/api/v5/market/candles',
        {'instId': 'BTC-USDT-SWAP', 'bar': '1H', 'limit': '60'},
    )
    if not c_1h or len(c_1h) < 55:
      return 'BILINMIYOR', 0
    df = (
        pd.DataFrame(
            c_1h,
            columns=[
                'ts',
                'o',
                'h',
                'l',
                'c',
                'v',
                'vc',
                'vq',
                'conf',
            ],
        )
        .iloc[::-1]
        .reset_index(drop=True)
    )
    df['c'] = df['c'].astype(float)
    ema20 = df['c'].ewm(span=20).mean().iloc[-1]
    ema50 = df['c'].ewm(span=50).mean().iloc[-1]
    last_price = df['c'].iloc[-1]
    chg_3h = (
        (last_price / df['c'].iloc[-4] - 1) * 100 if len(df) >= 4 else 0
    )

    if last_price > ema20 > ema50 or chg_3h > 1.5:
      return 'GUCLU_YUKARI', 2
    elif last_price > ema20:
      return 'HAFIF_YUKARI', 1
    elif last_price < ema20 < ema50:
      return 'ASAGI', -1
    else:
      return 'NOTR', 0
  except Exception as e:
    print(f'BTC trend hatası: {e}')
    return 'BILINMIYOR', 0


def check_rsi_price_divergence(df):
  try:
    if df is None or len(df) < 30:
      return 'Veri Yetersiz', False
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
      if (
          h[i] > h[i - 1]
          and h[i] > h[i - 2]
          and h[i] > h[i + 1]
          and h[i] > h[i + 2]
      ):
        highs_idx.append(i)

    if len(highs_idx) < 2:
      return 'Uyumsuzluk Yok', False

    i1, i2 = highs_idx[-2], highs_idx[-1]
    price1, price2 = h[i1], h[i2]
    rsi1, rsi2 = rsi_window.iloc[i1], rsi_window.iloc[i2]

    if price2 > price1 and rsi2 < rsi1:
      return '🔻 RSI AYI UYUMSUZLUĞU', True
    return 'Uyumsuzluk Yok', False
  except Exception as e:
    print(f'Divergence hesap hatası: {e}')
    return 'Hesaplama Hatası', False


def find_custom_sr(df, pivot_len=PIVOT_LEN):
  if len(df) < (pivot_len * 2 + 1):
    return [], []
  highs = df['h'].astype(float).values
  lows = df['l'].astype(float).values
  p_highs, p_lows = [], []
  for i in range(pivot_len, len(highs) - pivot_len):
    if all(
        highs[i] > highs[i - j] for j in range(1, pivot_len + 1)
    ) and all(highs[i] > highs[i + j] for j in range(1, pivot_len + 1)):
      p_highs.append(highs[i])
    if all(lows[i] < lows[i - j] for j in range(1, pivot_len + 1)) and all(
        lows[i] < lows[i + j] for j in range(1, pivot_len + 1)
    ):
      p_lows.append(lows[i])
  return p_highs, p_lows


def check_volume_divergence(df):
  if len(df) < 5:
    return ''
  p = df['c'].astype(float).iloc[-5:].values
  v = df['v'].astype(float).iloc[-5:].values
  if p[-1] > p[0] and v[-1] < v[-2]:
    return '⚠️ Hacimsiz Yükseliş (Zayıf Trend)'
  return ''


def check_candle_trigger_15m(symbol):
  try:
    c_15m = get_data(
        '/api/v5/market/candles',
        {'instId': symbol, 'bar': '15m', 'limit': '6'},
    )
    if not c_15m or len(c_15m) < 4:
      return 'Veri Alınamadı', 0
    df_15m = (
        pd.DataFrame(
            c_15m,
            columns=[
                'ts',
                'o',
                'h',
                'l',
                'c',
                'v',
                'vc',
                'vq',
                'conf',
            ],
        )
        .iloc[::-1]
        .reset_index(drop=True)
    )
    df_15m[['o', 'h', 'l', 'c']] = df_15m[['o', 'h', 'l', 'c']].astype(float)
    last3 = df_15m.iloc[-3:].reset_index(drop=True)
    o, h, l, c = (
        last3.iloc[-1]['o'],
        last3.iloc[-1]['h'],
        last3.iloc[-1]['l'],
        last3.iloc[-1]['c'],
    )
    body = abs(c - o)
    upper_wick = (h - max(o, c)) if h > max(o, c) else 0
    last_candle_bearish = (c < o) or (upper_wick > body * 0.8)
    lower_high = last3.iloc[-1]['h'] < last3.iloc[-2]['h']
    closes_weakening = last3.iloc[-1]['c'] < last3.iloc[-2]['c']
    confirmations = sum([last_candle_bearish, lower_high, closes_weakening])

    if confirmations >= 2:
      return (
          f'🚨 ACİL DURUM: REAKSİYON TEYİTLİ ({confirmations}/3)',
          confirmations,
      )
    elif confirmations == 1:
      return '⚠️ Tek Teyit (Zayıf Reaksiyon)', 1
    else:
      return '⏳ Grafik Güçlü (Dönüş Yok)', 0
  except Exception as e:
    print(f'15m tetik hatası: {e}')
    return 'Durum Okunamadı', 0


def get_funding_rate(symbol):
  res = get_data('/api/v5/public/funding-rate', {'instId': symbol})
  if res and len(res) > 0:
    rate = float(res[0].get('fundingRate', 0))
    rate_pct = rate * 100
    if rate_pct < -0.05:
      return f'%{rate_pct:.3f}', '⚠️ AŞIRI NEGATİF FL (Short Riski)'
    elif rate_pct > 0.08:
      return (
          f'%{rate_pct:.3f}',
          '🔥 AŞIRI POZİTİF FL (Kaldıraçlı Long Yoğun)',
      )
    return f'%{rate_pct:.3f}', ''
  return 'Veri Alınamadı', ''


def get_oi_trend(symbol):
  try:
    inst_id = symbol if '-SWAP' in symbol else f"{symbol.split('-')[0]}-USDT-SWAP"
    res = get_data('/api/v5/public/open-interest', {'instId': inst_id})
    if not res or len(res) == 0:
      return 'OI Verisi Alınamadı', 0
    oi_now = float(res[0].get('oi', 0))
    comment = 'Short Pozitif Birikim' if oi_now > 100000 else 'Dengeli'
    return f'{oi_now:.0f} Kontrat ({comment})', oi_now
  except Exception as e:
    print(f'OI hatası ({symbol}): {e}')
    return 'OI Verisi Çekilemedi', 0


def get_net_buying_pressure(symbol):
  try:
    inst_id = symbol if '-SWAP' in symbol else f"{symbol.split('-')[0]}-USDT-SWAP"
    res = get_data(
        '/api/v5/rubik/stat/taker-volume',
        {'instId': inst_id, 'period': '1H'},
    )
    if res and len(res) > 0 and isinstance(res[0], list) and len(res[0]) >= 3:
      buy_vol = float(res[0][1])
      sell_vol = float(res[0][2])
      total_vol = buy_vol + sell_vol
      if total_vol == 0:
        return 'Hacim Yok', 0
      net_pressure_pct = ((buy_vol - sell_vol) / total_vol) * 100
      if net_pressure_pct > 15:
        return f'+%{net_pressure_pct:.1f} (Alıcı Baskın)', net_pressure_pct
      elif net_pressure_pct < -15:
        return f'%{net_pressure_pct:.1f} (Satıcı Baskın)', net_pressure_pct
      else:
        return f'%{net_pressure_pct:.1f} (Dengeli)', net_pressure_pct
    return 'Veri Yok', 0
  except Exception as e:
    print(f'Saf alım baskısı hatası ({symbol}): {e}')
    return 'Veri Yok', 0


# =========================================================
# SİNYAL TAKİP VE GÜNCELLEME SİSTEMİ
# =========================================================
def check_signal_status(symbol, entry_price, entry_rsi, res_1h):
  try:
    c_1h = get_data(
        '/api/v5/market/candles',
        {'instId': symbol, 'bar': '1H', 'limit': '30'},
    )
    if not c_1h or len(c_1h) < 20:
      return 'PENDING', 0
    df = (
        pd.DataFrame(
            c_1h,
            columns=[
                'ts',
                'o',
                'h',
                'l',
                'c',
                'v',
                'vc',
                'vq',
                'conf',
            ],
        )
        .iloc[::-1]
        .reset_index(drop=True)
    )
    df['c'] = df['c'].astype(float)
    last_closed = df.iloc[-2]
    current_close = float(last_closed['c'])
    pct_move = (current_close / entry_price - 1) * 100

    delta = df['c'].diff()
    g = (delta.where(delta > 0, 0)).rolling(14).mean()
    l = (-delta.where(delta < 0, 0)).rolling(14).mean()
    last_g, last_l = g.iloc[-2], l.iloc[-2]

    # RSI Sıfıra bölünme koruması
    if last_l == 0:
      current_rsi = 100.0
    else:
      current_rsi = 100 - (100 / (1 + (last_g / last_l)))

    if res_1h and res_1h > 0 and current_close > res_1h * 1.003:
      return 'INVALIDATE_STRUCTURE', pct_move
    if current_rsi > entry_rsi + 5 and current_close > entry_price:
      return 'INVALIDATE_MOMENTUM', pct_move
    if pct_move <= -3:
      return 'SUCCESS', pct_move
    return 'PENDING', pct_move
  except Exception as e:
    print(f'Sinyal durum kontrolü hatası ({symbol}): {e}')
    return 'PENDING', 0


def update_open_signals():
  print('⏳ Mevcut açık sinyaller kontrol ediliyor...')
  if not os.path.exists(LOG_FILE):
    return
  try:
    df = pd.read_csv(LOG_FILE)
  except Exception as e:
    print(f'Log okuma hatası: {e}')
    return
  if df.empty:
    return

  now = datetime.now(timezone.utc)
  updated = False
  notify_msgs = []

  for idx, row in df.iterrows():
    if str(row.get('resolved')).upper() == 'TRUE':
      continue
    try:
      symbol = row['symbol']
      entry_price = float(row['entry_price'])
      entry_rsi = float(row['rsi'])
      res_1h = float(row['res_1h']) if pd.notna(row['res_1h']) else 0
      status, pct_move = check_signal_status(
          symbol, entry_price, entry_rsi, res_1h
      )

      ts = pd.to_datetime(row['timestamp_utc'])
      if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')

      hours_passed = (now - ts).total_seconds() / 3600

      if status == 'INVALIDATE_STRUCTURE':
        df.at[idx, 'outcome'] = 'BASARISIZ (direnc kirildi)'
        df.at[idx, 'outcome_pct'] = round(pct_move, 2)
        df.at[idx, 'resolved'] = 'TRUE'
        updated = True
        notify_msgs.append(
            f'🔁 *SİNYAL GEÇERSİZ (YAPI KIRILDI):* {symbol} 1H direnç seviyesinin'
            f' üzerinde kapandı (%{pct_move:.1f}). Short tezi geçersiz.'
        )
      elif status == 'INVALIDATE_MOMENTUM':
        df.at[idx, 'outcome'] = 'BASARISIZ (momentum geri geldi)'
        df.at[idx, 'outcome_pct'] = round(pct_move, 2)
        df.at[idx, 'resolved'] = 'TRUE'
        updated = True
        notify_msgs.append(
            f'📉 *SİNYAL GEÇERSİZ (MOMENTUM DÖNDÜ):* {symbol} için aşırı alım'
            f' bölgesi tükenmedi, RSI yükseliyor (%{pct_move:.1f}).'
        )
      elif status == 'SUCCESS':
        df.at[idx, 'outcome'] = 'BAŞARILI (hedef yakalandı)'
        df.at[idx, 'outcome_pct'] = round(pct_move, 2)
        df.at[idx, 'resolved'] = 'TRUE'
        updated = True
        notify_msgs.append(
            f'💰 *SİNYAL BAŞARILI:* {symbol} beklendiği gibi dirençten çöktü!'
            f' Hareket: %{pct_move:.1f}'
        )
      elif hours_passed > MAX_PENDING_HOURS:
        df.at[idx, 'outcome'] = 'ZAMAN ASIMI'
        df.at[idx, 'outcome_pct'] = round(pct_move, 2)
        df.at[idx, 'resolved'] = 'TRUE'
        updated = True
    except Exception as e:
      print(f'Satır işleme hatası (Satır {idx}): {e}')

  if updated:
    df.to_csv(LOG_FILE, index=False)
    for msg in notify_msgs:
      send_telegram_text(msg)


def log_new_signal(symbol, entry_price, rsi, res_1h):
  file_exists = os.path.exists(LOG_FILE)
  now_str = datetime.now(timezone.utc).isoformat()
  fieldnames = [
      'timestamp_utc',
      'symbol',
      'entry_price',
      'rsi',
      'res_1h',
      'outcome',
      'outcome_pct',
      'resolved',
  ]
  try:
    with open(LOG_FILE, mode='a', newline='', encoding='utf-8') as f:
      writer = csv.DictWriter(f, fieldnames=fieldnames)
      if not file_exists:
        writer.writeheader()
      writer.writerow({
          'timestamp_utc': now_str,
          'symbol': symbol,
          'entry_price': entry_price,
          'rsi': round(rsi, 2),
          'res_1h': res_1h,
          'outcome': 'PENDING',
          'outcome_pct': 0.0,
          'resolved': 'FALSE',
      })
  except Exception as e:
    print(f'Log yazma hatası ({symbol}): {e}')


# =========================================================
# EKRAN GÖRÜNTÜSÜ VE GEMINI ENTEGRASYONU
# =========================================================
def take_tradingview_screenshot(tv_symbol, output_path='chart.png'):
  url = f'https://s.tradingview.com/widgetembed/?frameElementId=tradingview_chart&symbol=OKX:{tv_symbol}.P&interval=60&theme=dark&style=1'
  try:
    with sync_playwright() as p:
      browser = p.chromium.launch(headless=True)
      page = browser.new_page(viewport={'width': 1280, 'height': 720})
      page.goto(url, wait_until='networkidle')
      page.wait_for_timeout(4000)
      page.screenshot(path=output_path)
      browser.close()
    return True
  except Exception as e:
    print(f'Grafik görüntüsü alınamadı ({tv_symbol}): {e}')
    return False


def analyze_charts_with_gemini(signals_data, btc_trend_label):
  if not GEMINI_API_KEY or not signals_data:
    return None
  try:
    client = genai.Client(api_key=GEMINI_API_KEY)
    contents = []
    prompt = f"""
        Sen profesyonel bir veri analitiği botusun. Hikaye yazma, uzun paragraflar kurma.
        Genel BTC Trendi: {btc_trend_label}. 
        Gelen grafikleri ve metin verilerini incele, SADECE aşağıdaki şablona sadık kalarak, en özet maddeler halinde çıktı üret.
        🤖 **GEMINI RAPORU**
        ──────────────────
        • **Aday Durumları:**
        [İncelediğin her parite için sadece 1 kısa cümlelik teknik veri durum özeti, örn: "SOL-USDT: RSI şişkin, dirençte dönüş arıyor."]
        👑 **ALFA SEÇİMİ (SHORT)**
        ──────────────────
        🎯 **Coin:** [Seçilen Parite]
        💡 **Neden?:** [En önemli 2 teknik sebep, yan yana yazılacak]
        🛑 **Stop:** [Yapısal direnç seviyesi]
        """
    contents.append(prompt)
    for item in signals_data:
      if os.path.exists(item['img_path']):
        with open(item['img_path'], 'rb') as f:
          img_bytes = f.read()
        contents.append(
            types.Part.from_bytes(data=img_bytes, mime_type='image/png')
        )
      contents.append(
          f"Coin: {item['symbol']} Teknik Özeti:\n{item['text_data']}\n\n"
      )

    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=contents,
    )
    return response.text
  except Exception as e:
    print(f'Gemini multimodal analiz hatası: {e}')
    return None


# =========================================================
# HIZLANDIRILMIŞ VE OPTİMİZE EDİLMİŞ MARKET TARAMA
# =========================================================
def get_market_candidates():
  print('🌐 OKX Borsasındaki aktif vadeli işlem pariteleri taranıyor...')
  tickers = get_data('/api/v5/market/tickers', {'instType': 'SWAP'})
  candidates = []
  if not tickers:
    print("⚠️ Ticker verisi borsa API'sinden çekilemedi.")
    return []

  usdt_swaps = [t for t in tickers if t['instId'].endswith('-USDT-SWAP')]
  for t in usdt_swaps:
    symbol = t['instId']
    try:
      chg24h = ((float(t['last']) / float(t['open24h'])) - 1) * 100
    except (ValueError, KeyError, ZeroDivisionError):
      continue

    if chg24h < CHANGE_24H_LIMIT:
      continue

    c_1h = get_data(
        '/api/v5/market/candles',
        {'instId': symbol, 'bar': '1H', 'limit': '35'},
    )
    if not c_1h or len(c_1h) < 20:
      continue

    df = (
        pd.DataFrame(
            c_1h,
            columns=[
                'ts',
                'o',
                'h',
                'l',
                'c',
                'v',
                'vc',
                'vq',
                'conf',
            ],
        )
        .iloc[::-1]
        .reset_index(drop=True)
    )
    df['c'] = df['c'].astype(float)
    df['h'] = df['h'].astype(float)
    df['l'] = df['l'].astype(float)

    delta = df['c'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, 0.0001)
    rsi = 100 - (100 / (1 + rs))
    last_rsi = rsi.iloc[-1]

    vol_div = check_volume_divergence(df)
    has_vol_anomaly = vol_div != ''

    if last_rsi >= RSI_LIMIT or has_vol_anomaly:
      p_highs, p_lows = find_custom_sr(df)
      res_1h = p_highs[-1] if p_highs else (df['h'].max())

      # Trend ve Pivot S/R Notları Hesaplama
      sr_notes = sr_detector.analyze(df)

      candidates.append({
          'symbol': symbol,
          'current_price': float(t['last']),
          'chg': chg24h,
          'rsi': last_rsi,
          'res_1h': res_1h,
          'vol_anomaly': vol_div,
          'sr_notes': sr_notes,
          'df_1h': df,
      })

  # En yüksek RSI'lı MAX 3 coin seçilir
  candidates = sorted(candidates, key=lambda x: x['rsi'], reverse=True)[:3]
  return candidates


# =========================================================
# ANA YÜRÜTÜCÜ (MAINLOOP)
# =========================================================
def main():
  print(
      '🚀 Gem Hunter Botu Başlatıldı:'
      f' {datetime.now(timezone.utc).isoformat()} UTC'
  )
  update_open_signals()
  btc_label, btc_score = get_btc_trend()
  candidates = get_market_candidates()
  if not candidates:
    print('📭 Bu tarama döngüsünde kriterlere uyan aday bulunamadı.')
    return

  signals_data = []
  raw_signals_msg = (
      f'🔍 *PİYASA TARAMA SONUÇLARI*\n🌐 *BTC:* {btc_label}\n'
      '───────────────────────\n\n'
  )

  for coin in candidates:
    symbol = coin['symbol']
    tv_symbol = symbol.replace('-USDT-SWAP', 'USDT')

    oi_label, _ = get_oi_trend(symbol)
    pressure_label, _ = get_net_buying_pressure(symbol)
    div_label, has_div = check_rsi_price_divergence(coin['df_1h'])
    urgent_label, confirm_count = check_candle_trigger_15m(symbol)
    funding_raw, fl_label = get_funding_rate(symbol)

    # Ekran görüntüsü alma işlemi
    img_path = f"{symbol.replace('-', '_')}.png"
    shot_success = take_tradingview_screenshot(tv_symbol, img_path)

    # Telegram metin özeti
    coin_text = f'📍 *{symbol}*\n'
    coin_text += (
        f"• Fiyat: `{coin['current_price']}` | 24s Değişim:"
        f" `%{coin['chg']:.1f}`\n"
    )
    coin_text += (
        f"• 1H RSI: `{coin['rsi']:.1f}` | 1H Direnç: `{coin['res_1h']}`\n"
    )
    coin_text += f'• OI Durumu: {oi_label}\n'
    coin_text += f'• Saf Alım Baskısı: {pressure_label}\n'
    coin_text += f'• Fonlama Oranı: {funding_raw} {fl_label}\n'
    if coin['vol_anomaly']:
      coin_text += f"• {coin['vol_anomaly']}\n"
    if has_div:
      coin_text += f'• {div_label}\n'
    coin_text += f'• 15M Dönüş Teyidi: {urgent_label}\n'

    # S/R ve Trend Notlarını Telegram Mesajına Ekleme
    if coin.get('sr_notes'):
      for note in coin['sr_notes']:
        coin_text += f'• {note}\n'

    coin_text += '\n'
    raw_signals_msg += coin_text

    if shot_success:
      signals_data.append({
          'symbol': symbol,
          'img_path': img_path,
          'text_data': coin_text,
      })

    # Sinyali CSV'ye kaydet
    log_new_signal(
        symbol, coin['current_price'], coin['rsi'], coin['res_1h']
    )

  # 1. Ham Tarama Mesajını Telegram'a Gönder
  send_telegram_text(raw_signals_msg)

  # 2. Gemini Yapay Zeka Analizi ve Grafikli Raporlama
  if signals_data and GEMINI_API_KEY:
    print('🤖 Gemini AI grafikleri ve teknik verileri inceliyor...')
    report = analyze_charts_with_gemini(signals_data, btc_label)
    if report:
      first_img = signals_data[0]['img_path']
      send_telegram_photo(first_img, report)
    else:
      print('Gemini rapor üretemedi.')

  # Geçici ekran görüntülerini temizle
  for item in signals_data:
    if os.path.exists(item['img_path']):
      try:
        os.remove(item['img_path'])
      except Exception:
        pass


if __name__ == '__main__':
  main()
