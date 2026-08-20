import pandas as pd
import numpy as np

class SRDetector:
    def __init__(self, pivot_len=8, max_pivots=12, breakout_buf_mult=0.15, near_mult=0.50):
        self.pivot_len = pivot_len
        self.max_pivots = max_pivots
        self.breakout_buf_mult = breakout_buf_mult
        self.near_mult = near_mult

    def calculate_atr(self, df, period=14):
        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        return true_range.rolling(period).mean()

    def find_pivots(self, df):
        ph_prices, ph_bars = [], []
        pl_prices, pl_bars = [], []
        
        n = len(df)
        for i in range(self.pivot_len, n - self.pivot_len):
            window_high = df['high'].iloc[i - self.pivot_len : i + self.pivot_len + 1]
            if df['high'].iloc[i] == window_high.max():
                ph_prices.append(df['high'].iloc[i])
                ph_bars.append(i)
                
            window_low = df['low'].iloc[i - self.pivot_len : i + self.pivot_len + 1]
            if df['low'].iloc[i] == window_low.min():
                pl_prices.append(df['low'].iloc[i])
                pl_bars.append(i)
                
        return (ph_prices[-self.max_pivots:], ph_bars[-self.max_pivots:], 
                pl_prices[-self.max_pivots:], pl_bars[-self.max_pivots:])

    def get_best_line(self, prices, bars, current_bar):
        if len(prices) < 2:
            return None, None
        
        # Son iki pivot üzerinden trend çizgisi denklemi (y = slope * x + intercept)
        x1, y1 = bars[-2], prices[-2]
        x2, y2 = bars[-1], prices[-1]
        
        slope = (y2 - y1) / (x2 - x1)
        intercept = y1 - slope * x1
        
        return slope, intercept

    def analyze(self, df):
        if len(df) < 50:
            return []

        df = df.copy()
        df['atr'] = self.calculate_atr(df)
        
        current_bar = len(df) - 1
        current_close = df['close'].iloc[-1]
        prev_close = df['close'].iloc[-2]
        current_atr = df['atr'].iloc[-1]
        
        ph_prices, ph_bars, pl_prices, pl_bars = self.find_pivots(df)
        
        res_slope, res_intercept = self.get_best_line(ph_prices, ph_bars, current_bar)
        sup_slope, sup_intercept = self.get_best_line(pl_prices, pl_bars, current_bar)
        
        notes = []

        # --- DİRENÇ ANALİZİ ---
        if res_slope is not None:
            res_val_now = res_intercept + res_slope * current_bar
            res_val_prev = res_intercept + res_slope * (current_bar - 1)
            
            # Kırılım
            if prev_close <= res_val_prev and current_close > (res_val_now + self.breakout_buf_mult * current_atr):
                notes.append(f"🔼 **Direnç Kırıldı!**\nFiyat direnç çizgisini yukarı kırdı.\n• Güncel Fiyat: `{current_close:.4f}`\n• Kırılan Direnç: `{res_val_now:.4f}`")
            # Yakınlık
            elif current_close < res_val_now and (res_val_now - current_close) <= (self.near_mult * current_atr):
                diff = res_val_now - current_close
                notes.append(f"⚠️ **Dirence Yakın!**\nFiyat trend direncine yaklaştı.\n• Güncel Fiyat: `{current_close:.4f}`\n• Direnç Seviyesi: `{res_val_now:.4f}` (Mesafe: `{diff:.4f}`)")

        # --- DESTEK ANALİZİ ---
        if sup_slope is not None:
            sup_val_now = sup_intercept + sup_slope * current_bar
            sup_val_prev = sup_intercept + sup_slope * (current_bar - 1)
            
            # Kırılım
            if prev_close >= sup_val_prev and current_close < (sup_val_now - self.breakout_buf_mult * current_atr):
                notes.append(f"🔽 **Destek Kırıldı!**\nFiyat destek çizgisini aşağı kırdı.\n• Güncel Fiyat: `{current_close:.4f}`\n• Kırılan Destek: `{sup_val_now:.4f}`")
            # Yakınlık
            elif current_close > sup_val_now and (current_close - sup_val_now) <= (self.near_mult * current_atr):
                diff = current_close - sup_val_now
                notes.append(f"⚠️ **Desteğe Yakın!**\nFiyat trend desteğine yaklaştı.\n• Güncel Fiyat: `{current_close:.4f}`\n• Destek Seviyesi: `{sup_val_now:.4f}` (Mesafe: `{diff:.4f}`)")

        return notes
