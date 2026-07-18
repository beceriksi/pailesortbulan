import pandas as pd
from datetime import datetime

# =========================================================
# GÜNCELLENEN VE YENİ EKLENEN FONKSİYONLAR
# =========================================================

def get_oi_trend(symbol):
    """
    OKX Kamu API'sinden kesin ve kararlı anlık Open Interest verisini çeker.
    """
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
        print(f"OI yeni fonksiyon hatası ({symbol}): {e}")
        return "OI Verisi Yok (Hata)", 0


def get_net_buying_pressure(symbol):
    """
    Genel hacmi filtreleyerek sadece market emirleriyle (Taker) yapılan 
    net alım/satım baskısını (CVD mantığı) hesaplar.
    """
    try:
        if not symbol.endswith("-SWAP"):
            inst_id = f"{symbol.split('-')[0]}-USDT-SWAP"
        else:
            inst_id = symbol

        res = get_data("/api/v5/rubik/stat/taker-volume", {"instId": inst_id, "period": "1H"})
        
        if res and len(res) > 0:
            # OKX Yanıtı: [[ts, buyVol, sellVol]]
            buy_vol = float(res[0][1])   # Agresif alıcılar
            sell_vol = float(res[0][2])  # Agresif satıcılar
            total_vol = buy_vol + sell_vol

            if total_vol == 0:
                return "Hacim Yok / Durağan", 0

            net_pressure_pct = ((buy_vol - sell_vol) / total_vol) * 100
            
            if net_pressure_pct > 15:
                return f"🔥 SAF ALIM BASKISI: +%{net_pressure_pct:.1f} (Agresif Alıcılar Dominant)", net_pressure_pct
            elif net_pressure_pct < -15:
                return f"🔻 SAF SATIŞ BASKISI: %{net_pressure_pct:.1f} (Gizli Dağıtım / Mal Boşaltma)", net_pressure_pct
            else:
                return f"Dengeli Konsolidasyon: %{net_pressure_pct:.1f}", net_pressure_pct
        
        return "Saf Alım Verisi Alınamadı", 0
    except Exception as e:
        print(f"Saf alım baskısı hesaplama hatası ({symbol}): {e}")
        return "Saf Alım Verisi Yok", 0


def update_open_signals():
    """
    Açık olan sinyallerin durumunu günceller. Yazım hatası (p ct_move) düzeltildi.
    """
    print("⏳ Mevcut açık sinyaller kontrol ediliyor ve güncelleniyor...")
    try:
        df = pd.read_csv(LOG_FILE)
    except FileNotFoundError:
        return

    open_rows = df[df["resolved"] == "FALSE"]
    if open_rows.empty:
        return

    updated = False
    notify_msgs = []

    for idx, row in open_rows.iterrows():
        try:
            symbol = row["symbol"]
            status, pct_move = check_signal_status(row) # Durumu ve yüzde değişimi kontrol eden fonksiyonun

            if status == "INVALIDATE_STRUCTURE":
                df.at[idx, "outcome"] = "BASARISIZ (direnc kirildi)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(
                    f"🔁 *SİNYAL GEÇERSİZ (YAPI KIRILDI):* {symbol} 1H direnç seviyesinin "
                    f"üzerinde kapandı (%{pct_move:.1f}). Trend kırıldı, short tezi geçersiz."
                )
            elif status == "INVALIDATE_MOMENTUM":
                df.at[idx, "outcome"] = "BASARISIZ (momentum geri geldi)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(
                    f"📉 *SİNYAL GEÇERSİZ (MOMENTUM DÖNDÜ):* {symbol} için aşırı alım bölgesi "
                    f"tükenmedi, RSI yükselmeye devam ediyor. Pozisyon riskli."
                )
            elif status == "SUCCESS":
                df.at[idx, "outcome"] = "BAŞARILI (hedef yakalandı)"
                df.at[idx, "outcome_pct"] = round(pct_move, 2)
                df.at[idx, "resolved"] = "TRUE"
                updated = True
                notify_msgs.append(
                    f"💰 *SİNYAL BAŞARILI:* {symbol} beklendiği gibi dirençten çöktü! Hareket: %{pct_move:.1f}"
                )
            elif datetime.utcnow() - datetime.fromisoformat(row["timestamp"]) > pd.Timedelta(hours=MAX_PENDING_HOURS):
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
# ANA YÜRÜTÜCÜ (MAINLOOP)
# =========================================================

def main():
    print(f"🚀 Gem Hunter Botu Başlatıldı: {datetime.utcnow().isoformat()} UTC")
    
    # 1. Aşama: Önce açıkta bekleyen pozisyonları ve sinyal takibini güncelle
    update_open_signals()
    
    # 2. Aşama: Piyasadaki fırsatları tara
    print("🔍 Potansiyel sinyal adayları için piyasa taranıyor...")
    candidates = get_market_candidates() # 1H direnç + 15m tetik mekanizmandan dönen liste
    
    if not candidates:
        print("📭 Bu tarama döngüsünde kriterlere uyan aday bulunamadı.")
        return

    print(f"📊 Toplam {len(candidates)} aday üzerinde derin veri analitiği başlatılıyor...")
    
    for coin in candidates:
        symbol = coin["symbol"]
        
        # Kesinleşen yeni kararlı API'lerden verileri çekiyoruz
        oi_label, oi_raw = get_oi_trend(symbol)
        pressure_label, pressure_raw = get_net_buying_pressure(symbol)
        
        # Ekran logu
        print(f"-> {symbol} | {oi_label} | {pressure_label}")
        
        # Gemini'ye gönderilecek multimodal veri paketini hazırlıyoruz
        # Grafik resimlerini çektiğin fonksiyonun (örn: fetch_charts) olduğunu varsayıyoruz
        charts = fetch_charts(symbol) 
        
        # Gemini için zenginleştirilmiş metin promptu
        text_data = (
            f"Kripto Çifti: {symbol}\n"
            f"1H Direnç Seviyesi: {coin['resistance_1h']}\n"
            f"Anlık Fiyat (15m): {coin['current_price']}\n"
            f"15m RSI Değeri: {coin['rsi_15m']:.2f}\n"
            f"Açık Pozisyon Trendi (OI): {oi_label}\n"
            f"Net Alım/Satım Baskısı (1H CVD): {pressure_label}\n\n"
            f"GÖREV: Yukarıdaki teknik metrikleri ve ekte verilen 1H/15m grafik yapılarını "
            f"birlikte incele. Fiyatın dirençten dönme (Short) ihtimalini rasyonel bir şekilde "
            f"puanla. Eğer 'ALFA' bir fırsat görüyorsan onay ver."
        )
        
        # Gemini Multimodal Analizi Tetikleniyor
        decision = analyze_charts_with_gemini(charts, text_data)
        
        if decision.get("verdict") == "APPROVED":
            # Onaylanan sinyali Telegram'a gönder ve CSV'ye logla
            log_and_notify_approved_signal(coin, decision, oi_label, pressure_label)
            
    print("🏁 Tarama döngüsü başarıyla tamamlandı. GitHub Actions kapatılıyor.")

if __name__ == "__main__":
    main()
