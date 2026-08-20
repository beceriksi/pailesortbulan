import pandas as pd
import numpy as np

class SRDetector:
    def __init__(self, pivot_len=8, max_pivots=12, breakout_buf_mult=0.15, 
                 tol_mult=0.15, near_mult=0.50, line_mode="Çok Dokunulan", 
                 show_horizontal=True, lookback_bars=300):
        self.pivot_len = pivot_len
        self.max_pivots = max_pivots
        self.breakout_buf_mult = breakout_buf_mult
        self.tol_mult = tol_mult
        self.near_mult = near_mult
        self.line_mode = line_mode
        self.show_horizontal = show_horizontal
        self.lookback_bars = lookback_bars

    def calculate_atr(self, df, period=14):
        high_low = df['h'] - df['l']
        high_close = np.abs(df['h'] - df['c'].shift())
        low_close = np.abs(df['l'] - df['c'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = np.max(ranges, axis=1)
        return true_range.rolling(period).mean()

    def find_pivots(self, df):
        ph_prices, ph_bars = [], []
        pl_prices, pl_bars = [], []
        n = len(df)
        
        for i in range(self.pivot_len, n - self.pivot_len):
            window_high = df['h'].iloc[i - self.pivot_len : i + self.pivot_len + 1]
            if df['h'].iloc[i] == window_high.max():
                ph_prices.append(df['h'].iloc[i])
                ph_bars.append(i)
                
            window_low = df['l'].iloc[i - self.pivot_len : i + self.pivot_len + 1]
            if df['l'].iloc[i] == window_low.min():
                pl_prices.append(df['l'].iloc[i])
                pl_bars.append(i)
                
        return (ph_prices[-self.max_pivots:], ph_bars[-self.max_pivots:], 
                pl_prices[-self.max_pivots:], pl_bars[-self.max_pivots:])

    def is_line_valid(self, df, x1, y1, x2, y2, side, tol):
        slope = (y2 - y1) / (x2 - x1)
        intercept = y1 - slope * x1
        for b in range(x1 + 1, x2):
            line_val = intercept + slope * b
            if side == 1:  # Direnç
                if df['h'].iloc[b] > line_val + tol:
                    return False
            else:  # Destek
                if df['l'].iloc[b] < line_val - tol:
                    return False
        return True

    def count_touches(self, prices, bars, x1, x2, slope, intercept, tol):
        touches = 0
        for j in range(len(prices)):
            xj = bars[j]
            yj = prices[j]
            if x1 <= xj <= x2:
                line_val = intercept + slope * xj
                if abs(yj - line_val) <= tol:
                    touches += 1
        return touches

    def find_best_line(self, df, prices, bars, side, tol):
        n = len(prices)
        if n < 2:
            return None, None
            
        bx1, by1, bx2, by2 = None, None, None, None
        found = False
        best_touches = -1
        
        x2 = bars[-1]
        y2 = prices[-1]
        
        for i in range(n - 1):
            x1 = bars[i]
            y1 = prices[i]
            if self.is_line_valid(df, x1, y1, x2, y2, side, tol):
                if self.line_mode == "En Uzun Geçerli":
                    if not found:
                        bx1, by1, bx2, by2 = x1, y1, x2, y2
                        found = True
                        break
                else:  # Çok Dokunulan
                    slope = (y2 - y1) / (x2 - x1)
                    intercept = y1 - slope * x1
                    touches = self.count_touches(prices, bars, x1, x2, slope, intercept, tol)
                    if touches > best_touches:
                        best_touches = touches
                        bx1, by1, bx2, by2 = x1, y1, x2, y2
                        found = True
                        
        if not found and n >= 2:
            bx1, by1, bx2, by2 = bars[-2], prices[-2], x2, y2
            
        if bx1 is not None and bx2 is not None and bx1 != bx2:
            slope = (by2 - by1) / (bx2 - bx1)
            intercept = by1 - slope * bx1
            return slope, intercept
        return None, None

    def analyze(self, df):
        if df is None or len(df) < 20:
            return []

        df = df.copy()
        df['h'] = df['h'].astype(float)
        df['l'] = df['l'].astype(float)
        df['c'] = df['c'].astype(float)
        
        df['atr'] = self.calculate_atr(df)
        
        current_bar = len(df) - 1
        current_close = df['c'].iloc[-1]
        prev_close = df['c'].iloc[-2] if len(df) > 1 else current_close
        current_atr = df['atr'].iloc[-1]
        
        if pd.isna(current_atr) or current_atr == 0:
            return []

        tol = self.tol_mult * current_atr
        ph_prices, ph_bars, pl_prices, pl_bars = self.find_pivots(df)
        
        res_slope, res_intercept = self.find_best_line(df, ph_prices, ph_bars, 1, tol)
        sup_slope, sup_intercept = self.find_best_line(df, pl_prices, pl_bars, -1, tol)
        
        notes = []

        # Trend Direnç Analizi
        if res_slope is not None:
            res_val_now = res_intercept + res_slope * current_bar
            res_val_prev = res_intercept + res_slope * (current_bar - 1)
            
            if prev_close <= res_val_prev and current_close > (res_val_now + self.breakout_buf_mult * current_atr):
                notes.append(f"🔼 **Trend Direnci Kırıldı!** (Seviye: `{res_val_now:.4f}`)")
            elif current_close < res_val_now and (res_val_now - current_close) <= (self.near_mult * current_atr):
                notes.append(f"⚠️ **Trend Direncine Yakın!** (Seviye: `{res_val_now:.4f}`)")

        # Trend Destek Analizi
        if sup_slope is not None:
            sup_val_now = sup_intercept + sup_slope * current_bar
            sup_val_prev = sup_intercept + sup_slope * (current_bar - 1)
            
            if prev_close >= sup_val_prev and current_close < (sup_val_now - self.breakout_buf_mult * current_atr):
                notes.append(f"🔽 **Trend Desteği Kırıldı!** (Seviye: `{sup_val_now:.4f}`)")
            elif current_close > sup_val_now and (current_close - sup_val_now) <= (self.near_mult * current_atr):
                notes.append(f"⚠️ **Trend Desteğine Yakın!** (Seviye: `{sup_val_now:.4f}`)")

        # Yatay Destek / Direnç Analizi
        if self.show_horizontal:
            lookback = min(self.lookback_bars, len(df))
            window_df = df.iloc[-lookback:]
            hh = window_df['h'].max()
            ll = window_df['l'].min()
            
            prev_window_df = df.iloc[-lookback - 1 : -1] if len(df) > lookback else window_df
            hh_prev = prev_window_df['h'].max()
            ll_prev = prev_window_df['l'].min()

            if prev_close <= hh_prev and current_close > hh:
                notes.append(f"🚀 **Yatay Zirve/Direnç Kırıldı!** (Seviye: `{hh:.4f}`)")
            if prev_close >= ll_prev and current_close < ll:
                notes.append(f"💥 **Yatay Dip/Destek Kırıldı!** (Seviye: `{ll:.4f}`)")

        return notes
