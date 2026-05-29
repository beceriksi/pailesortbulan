def scan():
    print("🔍 OKX Tickers verisi çekiliyor...")
    tickers = get_data("/api/v5/market/tickers", {"instType": "SWAP"})
    if not tickers: 
        print("❌ OKX'ten hiçbir ticker verisi alınamadı!")
        return
    print(f"📊 Toplam {len(tickers)} adet swap çifti bulundu.")

    underlyings = get_data("/api/v5/public/underlying", {"instType": "SWAP"})
    valid_cryptos = []
    if underlyings and isinstance(underlyings, list):
        for item in underlyings:
            if isinstance(item, dict) and 'underlying' in item:
                valid_cryptos.append(item['underlying'])
            elif isinstance(item, list) and len(item) > 0:
                valid_cryptos.append(item[0])
            elif isinstance(item, str):
                valid_cryptos.append(item)
    print(f"✅ Filtrelenmiş geçerli coin listesi derinliği: {len(valid_cryptos)}")

    detected_signals = []
    
    for t in tickers:
        symbol = t['instId']
        if "-USDT-" not in symbol: continue
        
        base_coin = symbol.split('-')[0]
        if valid_cryptos and base_coin not in valid_cryptos:
            continue
        
        try:
            last_p = float(t['last'])
            open_24h = float(t['open24h'])
            if open_24h == 0: continue
            chg = (last_p / open_24h - 1) * 100
            
            # TEST LOGU: %8 barajına takılanları görmek için burayı izleyebilirsin
            if chg > CHANGE_24H_LIMIT:
                print(f"🚀 {symbol} %8 barajını geçti (%{chg:.2f}). 1H Mumları isteniyor...")
                
                c_1h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "1H", "limit": "100"})
                if not c_1h: 
                    print(f"⚠️ {symbol} için 1H mum verisi boş döndü!")
                    continue
                
                df_1h = pd.DataFrame(c_1h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
                df_1h['c'] = df_1h['c'].astype(float)
                
                if len(df_1h) < 15: 
                    print(f"⚠️ {symbol} veri uzunluğu yetersiz ({len(df_1h)} mum). Eleyor.")
                    continue
                    
                delta = df_1h['c'].diff()
                g = (delta.where(delta > 0, 0)).rolling(14).mean()
                l = (-delta.where(delta < 0, 0)).rolling(14).mean()
                
                last_g, last_l = g.iloc[-1], l.iloc[-1]
                rsi = 100 - (100 / (1 + last_g / last_l)) if last_l != 0 else (100 if last_g > 0 else 50)
                
                print(f"📈 {symbol} -> Anlık RSI Değeri: {rsi:.2f}")
                
                if rsi > RSI_LIMIT:
                    print(f"🔥 FİLTRE GEÇİLDİ: {symbol} hem %8 üstü hem RSI > {RSI_LIMIT}! Detaylar toplanıyor...")
                    
                    c_4h = get_data("/api/v5/market/candles", {"instId": symbol, "bar": "4H", "limit": "100"})
                    if not c_4h: 
                        print(f"⚠️ {symbol} 4H mumu alınamadı, eleniyor.")
                        continue
                    df_4h = pd.DataFrame(c_4h, columns=['ts','o','h','l','c','v','vc','vq','conf']).iloc[::-1].reset_index(drop=True)
                    
                    ph_4h, _ = find_custom_sr(df_4h)
                    res_4h = min([x for x in ph_4h if x > last_p]) if any(x > last_p for x in ph_4h) else (ph_4h[-1] if ph_4h else 0)
                    
                    ph_1h, _ = find_custom_sr(df_1h)
                    res_1h = min([x for x in ph_1h if x > last_p]) if any(x > last_p for x in ph_1h) else (ph_1h[-1] if ph_1h else 0)

                    # ALIM ODAKLI HACİM KONTROLÜ
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
                    
                    has_photo = take_tradingview_screenshot(tv_clean, img_name)
                    if not has_photo:
                        print(f"📸 {symbol} grafiği çekilemedi, düz metin olarak listeye ekleniyor.")
                        img_name = "NO_IMAGE"
                    
                    text_summary = f"Fiyat: {last_p} | 4H Dir: {res_4h} | 1H Dir: {res_1h} | 15m Durum: {candle_text} | Funding: {funding_info}"
                    
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
                    print(f"✅ {symbol} BAŞARIYLA LİSTEYE EKLENDİ.")
                    time.sleep(0.5)
                    
        except Exception as e:
            print(f"❌ {symbol} taranırken döngü içi hata: {e}")
            continue

    print(f"📢 Tarama bitti. Telegram'a gönderilecek toplam coin sayısı: {len(detected_signals)}")
    # ... (Telegram gönderim ve Gemini blokları aynen devam ediyor)
