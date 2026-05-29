def scan():
    print("🔍 OKX Tickers verisi çekiliyor...")
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    if not tickers: 
        print("❌ OKX'ten hiçbir ticker verisi alınamadı!")
        return
    print(f"📊 Toplam {len(tickers)} adet swap çifti bulundu.")

    detected_signals = []
    
    for t in tickers:
        symbol = t['instId']
        # Sadece USDT çiftlerini ve SWAP (Vadeli) olanları alıyoruz
        if "-USDT-" not in symbol: continue
        
        try:
            last_p = float(t['last'])
            open_24h = float(t['open24h'])
            if open_24h == 0: continue
            chg = (last_p / open_24h - 1) * 100
            
            # 1. KRİTİK FİLTRE: %8 barajı
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
                
                # 2. KRİTİK FİLTRE: RSI Limiti
                if rsi > RSI_LIMIT:
                    
                    c_4h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
                    if not c_4h: continue
                    df_4h = pd.DataFrame(c_4h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
                    
                    ph_4h, _ = find_custom_sr(df_4h)
                    res_4h = min([x for x in ph_4h if x > last_p]) if any(x > last_p for x in ph_4h) else (ph_4h[-1] if ph_4h else 0)
                    
                    ph_1h, _ = find_custom_sr(df_1h)
                    res_1h = min([x for x in ph_1h if x > last_p]) if any(x > last_p for x in ph_1h) else (ph_1h[-1] if ph_1h else 0)

                    # ALIM ODAKLI NET HACİM VE DİĞER DETAYLAR
                    bv, sv, ratio = get_smart_volume(symbol, df_1h)
                    div = check_volume_divergence(df_1h)
                    
                    candle_text, urgent_score = check_candle_trigger_15m(symbol)
                    funding_info = get_funding_rate(symbol)

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
                    
                    # Ekran görüntüsü alınamazsa bile sinyal iptal OLMASIN, listeye eklensin
                    has_photo = take_tradingview_screenshot(tv_clean, img_name)
                    if not has_photo:
                        img_name = "NO_IMAGE"
                    
                    text_summary = f"Fiyat: {last_p} | 4H Dir: {res_4h} | 1H Dir: {res_1h} | 15m Durum: {candle_text} | Funding: {funding_info}"
                    
                    # DİKKAT: append artık has_photo kontrolünün dışında! Grafik olmasa bile sinyal eklenecek.
                    detected_signals.append({
                        "symbol": symbol,
                        "img_path": img_name,
                        "text_data": text_summary,
                        "rsi": round(rsi, 1),
                        "chg": round(chg, 1),
                        "ratio": ratio,
                        "div": div,
                        "note": note,
                        "candle": candle_text,
                        "urgent": urgent_score,
                        "funding": funding_info,
                        "tv_link": f"https://www.tradingview.com/chart/?symbol=OKX:{tv_clean}.P"
                    })
                    time.sleep(0.2)
                    
        except Exception as e:
            continue

    print(f"📢 Tarama bitti. Telegram'a gönderilecek toplam coin sayısı: {len(detected_signals)}")
    # Döngü bitti, sinyaller listesi doluysa aşağıda Telegram'a gönderilecek...
