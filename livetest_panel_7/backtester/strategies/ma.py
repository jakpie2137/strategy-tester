import pandas as pd
import talib as ta
import threading
from backtester.strategies.base import BaseStrategy

TA_LOCK = threading.Lock()

class MAStrategy(BaseStrategy):
    def __init__(self, fast=10, slow=30, amount=1):
        super().__init__()
        self.fast = fast
        self.slow = slow
        self.amount = amount

    @staticmethod
    def get_strategy_name():
        return "ma"

    @staticmethod
    def get_indicator_names():
        return ["MA_FAST", "MA_SLOW"]

    def compute_indicators(self, df):
        df = df.copy()
        with TA_LOCK:
            df['MA_FAST'] = ta.SMA(df['close'], timeperiod=self.fast)
            df['MA_SLOW'] = ta.SMA(df['close'], timeperiod=self.slow)
        return df

    def open_position_signal(self, df, current_position):
        if len(df) < max(self.fast, self.slow) + 1:
            return None
        df = self.compute_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        indicators = {
            "MA_FAST": last['MA_FAST'],
            "MA_SLOW": last['MA_SLOW'],
        }
        if current_position is not None:
            return None
        if prev['MA_FAST'] < prev['MA_SLOW'] and last['MA_FAST'] > last['MA_SLOW']:
            return {"signal_type": 'open_long', "amount": self.amount, "indicators": indicators}
        elif prev['MA_FAST'] > prev['MA_SLOW'] and last['MA_FAST'] < last['MA_SLOW']:
            return {"signal_type": 'open_short', "amount": self.amount, "indicators": indicators}
        else:
            return {"signal_type": None, "amount": self.amount, "indicators": indicators}

    def close_position_signal(self, df, current_position):
        if len(df) < max(self.fast, self.slow) + 1:
            return None
        df = self.compute_indicators(df)
        last = df.iloc[-1]
        indicators = {
            "MA_FAST": last['MA_FAST'],
            "MA_SLOW": last['MA_SLOW'],
        }
        if current_position is None:
            return None
        if current_position['side'] == 'long' and last['MA_FAST'] < last['MA_SLOW']:
            return {"signal_type": 'close_long', "amount": current_position['amount'], "indicators": indicators}
        elif current_position['side'] == 'short' and last['MA_FAST'] > last['MA_SLOW']:
            return {"signal_type": 'close_short', "amount": current_position['amount'], "indicators": indicators}
        else:
            return {"signal_type": None, "amount": current_position['amount'], "indicators": indicators}
