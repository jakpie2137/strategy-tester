# backtester/strategies/rsi.py
# Fully-indented, consolidated indicators, strict MACD-cross primary
# + Added: Stochastic Oscillator (STOCH) and Stochastic RSI (STOCH_RSI) indicators and signals
# (no breaking changes to prior logic)

import logging
from typing import Dict, List, Optional, Tuple
from collections import deque, defaultdict

import numpy as np
import pandas as pd
import ta

from backtester.strategies.base import BaseStrategy
from config import POSITION_SIZE
import config as _cfg


def _to_float(x):
    try:
        return float(x)
    except Exception:
        return np.nan


def _as_bool(x, default=False):
    try:
        return bool(x)
    except Exception:
        return default


# --- Helpers: compute from CLOSE, consistent across indicators ---
def _sma_close(series, window: int):
    s = pd.to_numeric(series, errors='coerce')
    return s.rolling(window=window, min_periods=window).mean()


def _ema_close(series, window: int):
    s = pd.to_numeric(series, errors='coerce')
    try:
        return ta.trend.EMAIndicator(s, window=window).ema_indicator()
    except Exception:
        return s.ewm(span=window, adjust=False).mean()


# =========================
# HIGH-LEVEL STRATEGY CONFIG
# =========================
PRIMARY_SIGNAL: Optional[str] = "RSI"  # "RSI" | "MA" | "MACD" | "BB" | "ATR" | "ATR" | "STOCH" | "STOCH_RSI"
RISK_MODE: str = "ATR"                 # "FIXED" | "ATR"
STORE_ENTRY_ATR: bool = True           # store entry ATR on open (for ATR-mode exits)

TRAILING_STOP_ENABLED: bool = True
CLOSE_AFTER_X_CANDLES: int = 0        # time-based hard close; 0 = disabled    # global switch: enable/disable trailing stop in risk logic


# =========================
# RISK CONFIGS
# =========================
RISK_PARAMS_FIXED = {
    "tp_long":     1.01,
    "sl_long":     0.99,
    "tp_short":    0.99,
    "sl_short":    1.01,
    "trail_long":  0.9975,  # fallback TS if ATR unavailable
    "trail_short": 1.0025,
}

RISK_PARAMS_ATR = {
    # ATR is in USD (price units); GUI ratios derived via ATR/price when needed
    "tp_k_long":   1.5,
    "sl_k_long":   1.5,
    "tp_k_short":  1.5,
    "sl_k_short":  1.5,
    "ts_k_long":   0.5,
    "ts_k_short":  0.5,
}


# =========================
# INDICATOR DISPLAY + PARAMS
# =========================
INDICATOR_CONFIG: Dict[str, dict] = {
    "RSI": {
        "enabled": True,
        "display": "sub1",
        "params": {"window": 14},
        "color": "#7db3ff",
        "is_zero_always_visible": True,
        "primary": {  # used when PRIMARY_SIGNAL == "RSI"
            "enabled": True,
            "oversold": 20.0,
            "overbought": 80.0,
        },
        "confirm": {
            "enabled": True,
            "use_level_50": False,
            "long_max": 50.0,  # allow LONG only if RSI <= long_max
            "short_min": 50.0  # allow SHORT only if RSI >= short_min
        },
    },

    "MA": {
        "enabled": True,
        "display": "main",
        "params": {
            "type": "SMA",       # "SMA" | "EMA"
            "window_fast": 40,
            "window_slow": 400,
        },
        "color": "#ffd166",
        "is_zero_always_visible": False,
        "primary": {            # used when PRIMARY_SIGNAL == "MA"
            "enabled": False,
            # examples: "ma_cross_bullish", "ma_cross_bearish",
            # "price_ma_cross_bullish", "price_ma_cross_bearish"
            "type": "ma_cross_bullish",
            "price_ma": "fast",
            "confirmation_bars": 0,
        },
        "confirm": {
            "enabled": False,
            "long_rules":  ["fast_gt_slow", "price_gt_fast", "price_gt_slow"],
            "short_rules": ["fast_lt_slow", "price_lt_fast", "price_lt_slow"],
            "combine": "any",
        },
    },

    "MACD": {
        "enabled": True,
        "display": "sub2",
        "params": {"fast": 12, "slow": 26, "signal": 9},
        "color": "#ff6b6b",
        "is_zero_always_visible": True,
        "primary": {           # used when PRIMARY_SIGNAL == "MACD"
            "enabled": False,
            "need_cross": True,       # require histogram crossing 0
            "confirm_bars": 0,        # if >0: require rising/dropping BEFORE cross, but still open AT cross
            "min_hist": 0.0,          # minimal |hist| on both sides of cross
            "min_delta": 0.0,         # minimal |(macd-signal) delta change|
            "epsilon": 0.0,           # deadband around 0; if 0 -> uses min_hist
        },
        "confirm": {
            "enabled": False,
            "long_rules":  ["signal_below_zero", "signal_lt_macd"],
            "short_rules": ["signal_above_zero", "signal_gt_macd"],
            "combine": "any",
            "rising_n": 5,  # placeholder (trend bias)
        },
    },

    "BB": {
        "enabled": True,
        "display": "main",
        "params": {"window": 20, "stdev": 2.0},
        "color": {"upper": "#cccccc", "middle": "#aaaaaa", "lower": "#cccccc"},
        "is_zero_always_visible": False,
        "primary": {           # used when PRIMARY_SIGNAL == "BB"
            "enabled": False,
            "mid_offset": 0.35,
            "use_cross": True,
            "inverted": False
        },
        "confirm": {
            "enabled": False,
            "mid_offset": 0.10,
        },
    },

    "ATR": {
        "enabled": True,
        "display": "sub3",
        "params": {"window": 14},
        "color": "#ffd166",
        "is_zero_always_visible": True,
        "primary": {
            "enabled": False,
            "avg_window": 5,
            "multiplier": 3.0,
            "direction": "candle"   # "candle" | "close_change" | "both" | "long_only" | "short_only"
        }
    },

    # ---- NEW: Stochastic Oscillator ----
    "STOCH": {
        "enabled": True,
        "display": "sub1",
        "params": {"k_window": 14, "d_window": 3, "smooth_k": 3},
        "color": {"k": "#9b59b6", "d": "#34495e"},
        "is_zero_always_visible": True,
        "primary": { "enabled": True },  # %K/%D cross → entry
        "confirm": { "enabled": True, "long_max": 20.0, "short_min": 80.0 },  # avg(%K,%D) gating
    },

    # ---- NEW: Stochastic RSI ----
    "STOCH_RSI": {
        "enabled": True,
        "display": "sub1",
        "params": {"rsi_window": 14, "stoch_window": 14, "smooth_k": 3, "d_window": 3},
        "color": {"k": "#00b894", "d": "#2d3436"},
        "is_zero_always_visible": True,
        "primary": { "enabled": True },  # %K/%D cross → entry (on RSI)
        "confirm": { "enabled": True, "long_max": 20.0, "short_min": 80.0 },
    },
}

# exact columns the strategy emits (used by engine/test_worker mapping)
INDICATOR_OUTPUT_COLUMNS: List[str] = [
    "RSI",
    "MA_FAST", "MA_SLOW",
    "MACD", "MACD_SIGNAL", "MACD_HIST",
    "BB_UPPER", "BB_MIDDLE", "BB_LOWER",
    "ATR",
    # NEW:
    "ATR_PCT",
    "STOCH_K","STOCH_D",
    "STOCHRSI_K","STOCHRSI_D",
    "VOL_AVG","BUY_VOL_AVG","VOL_SMA",
    "PCT_CHANGE",
    "FEAR_GREED",
]


class RSIStrategy(BaseStrategy):
    """RSI/MA/MACD/BB strategy with ATR exits; strict MACD histogram zero-cross.
       + Additive STOCH / STOCH_RSI support (no breaking changes).
    """

    # ---------- minimal API for engine/worker ----------
    @staticmethod
    def get_strategy_name(self_or_cls=None) -> str:
        return "rsi"

    def get_display_config(self) -> dict:
        return self._indicator_config

    def get_indicator_names(self) -> List[str]:
        # order of sub-panels for GUI
        cfg = self._indicator_config
        order = []
        for slot in ("sub1", "sub2", "sub3"):
            for name, c in cfg.items():
                if c.get("enabled") and str(c.get("display", "")).lower() == slot:
                    order.append(name)
                    break
        return order

    def get_indicator_output_columns(self) -> List[str]:
        return list(INDICATOR_OUTPUT_COLUMNS)

    # Compatibility with engine variants that expect this name:
    def get_db_indicator_columns(self) -> List[str]:
        return list(INDICATOR_OUTPUT_COLUMNS)

    def extract_indicator_values(self, row) -> Dict[str, float]:
        """Map a DF row to a plain dict for DB writing (only output columns)."""
        out = {}
        for col in INDICATOR_OUTPUT_COLUMNS:
            v = row.get(col) if hasattr(row, "get") else None
            if v is None and col == "BB_MIDDLE" and hasattr(row, "get"):
                v = row.get("BB_MID")
            try:
                out[col] = float(v) if v is not None and np.isfinite(v) else None
            except Exception:
                out[col] = None
        return out

    # ---------- ctor & internal state ----------
    def __init__(self, amount: float = 1.0, indicator_bucket: int = 5, bucket_mode: str = "rolling"):  # "fixed" or "rolling" window
        super().__init__()
        import copy
        self._indicator_config = copy.deepcopy(INDICATOR_CONFIG)
        self.primary_signal = PRIMARY_SIGNAL
        self.risk_mode = RISK_MODE
        self.store_entry_atr = STORE_ENTRY_ATR
        self.trailing_stop_enabled = TRAILING_STOP_ENABLED
        self.close_after_x_candles = int(CLOSE_AFTER_X_CANDLES or 0)
        self.amount = float(amount)
        self.indicator_bucket = max(1, int(indicator_bucket))
        self.bucket_mode = str(bucket_mode).lower().strip()

        self._risk_params_fixed = copy.deepcopy(RISK_PARAMS_FIXED)
        self._risk_params_atr = copy.deepcopy(RISK_PARAMS_ATR)
        # default close-signals config (layer 2)
        self.close_signals_cfg = {
            "enabled": False,
            "required_all": False,
            "BB_close": {
                "enabled": False,
                "primary": {
                    "enabled": False,
                    "mid_offset": 0.0,
                    "inverted": False,
                },
            },
            "RSI_close": {
                "enabled": False,
                "primary": {
                    "enabled": False,
                    "close_long": 70.0,
                    "close_short": 30.0,
                },
            },
            "FEAR_GREED_close": {
                "enabled": False,
                "primary": {
                    "enabled": False,
                    "close_long": 70.0,
                    "close_short": 30.0,
                },
            },
        }


        # apply external overrides (rsi_config.py)
        def _deep_update(dst, src):
            for k, v in (src or {}).items():
                if isinstance(v, dict) and isinstance(dst.get(k), dict):
                    _deep_update(dst[k], v)
                else:
                    dst[k] = v

        try:
            from . import rsi_config as _rsi_cfg

            # podstawowe override'y
            if hasattr(_rsi_cfg, "PRIMARY_SIGNAL"):
                self.primary_signal = _rsi_cfg.PRIMARY_SIGNAL
            if hasattr(_rsi_cfg, "RISK_MODE"):
                self.risk_mode = _rsi_cfg.RISK_MODE
            if hasattr(_rsi_cfg, "STORE_ENTRY_ATR"):
                self.store_entry_atr = bool(_rsi_cfg.STORE_ENTRY_ATR)

            if hasattr(_rsi_cfg, "TRAILING_STOP_ENABLED"):
                try:
                    self.trailing_stop_enabled = bool(_rsi_cfg.TRAILING_STOP_ENABLED)
                except Exception:
                    pass

            if hasattr(_rsi_cfg, "CLOSE_AFTER_X_CANDLES"):
                try:
                    self.close_after_x_candles = int(getattr(_rsi_cfg, "CLOSE_AFTER_X_CANDLES") or 0)
                except Exception:
                    pass

            if hasattr(_rsi_cfg, "CLOSE_SIGNALS"):
                try:
                    cs = getattr(_rsi_cfg, "CLOSE_SIGNALS") or {}
                    if isinstance(cs, dict):
                        import copy as _cp
                        merged = _cp.deepcopy(self.close_signals_cfg)
                        _deep_update(merged, cs)
                        self.close_signals_cfg = merged
                except Exception:
                    pass

            if hasattr(_rsi_cfg, "INDICATOR_OVERRIDES"):
                _deep_update(self._indicator_config, getattr(_rsi_cfg, "INDICATOR_OVERRIDES"))

            # Optional top-level ATR_PRIMARY block (legacy/explicit config style)
            if hasattr(_rsi_cfg, "ATR_PRIMARY"):
                try:
                    ap = dict(getattr(_rsi_cfg, "ATR_PRIMARY") or {})
                    self._indicator_config.setdefault("ATR", {}).setdefault("primary", {}).update(ap)
                except Exception:
                    pass


            if hasattr(_rsi_cfg, "RISK_PARAMS_ATR_OVERRIDES"):
                self._risk_params_atr.update(getattr(_rsi_cfg, "RISK_PARAMS_ATR_OVERRIDES"))

            if hasattr(_rsi_cfg, "RISK_PARAMS_FIXED_OVERRIDES"):
                self._risk_params_fixed.update(getattr(_rsi_cfg, "RISK_PARAMS_FIXED_OVERRIDES"))

            # --- Bucketization overrides (legacy + new) ---
            # sensowne domyślne
            self.bucket_mode = getattr(self, "bucket_mode", "rolling")
            self.indicator_bucket = int(getattr(self, "indicator_bucket", 1) or 1)

            # legacy klucze (kompatybilność wstecz)
            if hasattr(_rsi_cfg, "BUCKET_MODE"):
                try:
                    self.bucket_mode = str(_rsi_cfg.BUCKET_MODE).lower()
                except Exception:
                    pass
            if hasattr(_rsi_cfg, "INDICATOR_BUCKET"):
                try:
                    self.indicator_bucket = int(_rsi_cfg.INDICATOR_BUCKET or 1)
                    if self.indicator_bucket < 1:
                        self.indicator_bucket = 1
                except Exception:
                    pass

            # nowy słownik BUCKET — nadpisuje legacy, jeśli jest podany
            _bk = getattr(_rsi_cfg, "BUCKET", None)
            if isinstance(_bk, dict):
                try:
                    t = _bk.get("type", None)
                    if t is not None:
                        self.bucket_mode = str(t).lower()
                except Exception:
                    pass
                try:
                    w = _bk.get("window", None)
                    if w is not None:
                        self.indicator_bucket = int(w or 1)
                        if self.indicator_bucket < 1:
                            self.indicator_bucket = 1
                except Exception:
                    pass

        except Exception as e:
            logging.getLogger(__name__).warning("rsi_config.py overrides not applied: %s", e)

        # per-symbol state
        self._state = {
            "ma_prev": {},               # {symbol: {"fast":..., "slow":..., "price":...}}
            "macd_prev": {},             # {symbol: {"hist":..., "diff":...}}
            "macd_hist_window": defaultdict(lambda: deque(maxlen=16)),  # {symbol: deque}
            # NEW:
            "stoch_prev": {},            # {("STOCH", symbol): {"k":..., "d":...}, ("STOCH_RSI", symbol): {...}}
        }
        self._prev_price = {}
        self._last_price = np.nan
        self._last_atr = np.nan

    # ---------- risk params for GUI ----------
    def get_risk_params(self) -> dict:
        if self.risk_mode.upper() == "FIXED":
            rp = dict(self._risk_params_fixed)
            rp.setdefault("trail_long",  self._risk_params_fixed.get("trail_long", 0.9975))
            rp.setdefault("trail_short", self._risk_params_fixed.get("trail_short", 1.0025))
            # if trailing stop globally disabled, do not project TS levels in FIXED mode
            if not getattr(self, "trailing_stop_enabled", True):
                rp["trail_long"] = 0.0
                rp["trail_short"] = 0.0
            return rp

        price = float(self._last_price) if np.isfinite(self._last_price) else np.nan
        atr = float(self._last_atr) if np.isfinite(self._last_atr) else np.nan
        r = (atr / price) if (np.isfinite(atr) and np.isfinite(price) and price > 0) else 0.0
        k = self._risk_params_atr

        trail_long = 1.0 - k["ts_k_long"] * r
        trail_short = 1.0 + k["ts_k_short"] * r

        # if trailing stop disabled, make TS multipliers zero so engine will not use TS
        if not getattr(self, "trailing_stop_enabled", True):
            trail_long = 0.0
            trail_short = 0.0

        return {
            "tp_long":    1.0 + k["tp_k_long"]  * r,
            "sl_long":    1.0 - k["sl_k_long"]  * r,
            "tp_short":   1.0 - k["tp_k_short"] * r,
            "sl_short":   1.0 + k["sl_k_short"] * r,
            "trail_long": trail_long,
            "trail_short": trail_short,
        }



    def get_close_after_x_candles(self) -> int:
        """Return configured time-based hard close in candles (0 = disabled)."""
        try:
            return int(getattr(self, "close_after_x_candles", 0) or 0)
        except Exception:
            return 0




    # --- CLOSE SIGNAL HELPERS (BB / RSI / FEAR_GREED) ---

    def _bb_close_line(self, row, side: str):
        cfg_all = getattr(self, "close_signals_cfg", {}) or {}
        cfg = (cfg_all.get("BB_close") or cfg_all.get("BB_CLOSE") or {})
        primary = (cfg.get("primary") or {})
        if not (cfg.get("enabled", False) and primary.get("enabled", False)):
            return None

        upper = _to_float(row.get("BB_UPPER")) if hasattr(row, "get") else float("nan")
        lower = _to_float(row.get("BB_LOWER")) if hasattr(row, "get") else float("nan")
        mid = _to_float(row.get("BB_MIDDLE") or row.get("BB_MID")) if hasattr(row, "get") else float("nan")
        if not np.isfinite(upper) or not np.isfinite(lower):
            return None
        if not np.isfinite(mid):
            mid = 0.5 * (upper + lower)

        span = upper - lower
        if not np.isfinite(span) or span <= 0:
            return None

        try:
            mid_offset = float(primary.get("mid_offset", 0.0))
        except Exception:
            mid_offset = 0.0
        mid_offset = max(-0.5, min(0.5, mid_offset))
        inverted = bool(primary.get("inverted", False))

        side = (side or "").lower()
        if (side == "long" and not inverted) or (side == "short" and inverted):
            line = mid + mid_offset * span
        else:
            line = mid - mid_offset * span
        return line

    def compute_bb_close_initial_side(self, row, side: str):
        """Return (initial_side, line) for BB_close at entry, or (None, None) if inactive."""
        line = self._bb_close_line(row, side)
        if line is None:
            return None, None
        price = _to_float(row.get("close")) if hasattr(row, "get") else float("nan")
        if not np.isfinite(price):
            return None, None
        initial_side = "above" if price > float(line) else "below"
        return initial_side, float(line)

    def _bb_close_hit(self, row, position: dict) -> bool:
        cfg_all = getattr(self, "close_signals_cfg", {}) or {}
        cfg = (cfg_all.get("BB_close") or cfg_all.get("BB_CLOSE") or {})
        primary = (cfg.get("primary") or {})
        if not (cfg.get("enabled", False) and primary.get("enabled", False)):
            return False
        side = str(position.get("side") or "").lower()
        if side not in ("long", "short"):
            return False

        line = self._bb_close_line(row, side)
        if line is None:
            return False
        price = _to_float(row.get("close")) if hasattr(row, "get") else float("nan")
        if not np.isfinite(price):
            return False

        init = position.get("bb_close_initial_side")
        if init not in ("above", "below"):
            return False

        if side == "long":
            return (init == "below" and price >= line)
        else:
            return (init == "above" and price <= line)

    def _rsi_close_hit(self, row, side: str):
        cfg_all = getattr(self, "close_signals_cfg", {}) or {}
        cfg = (cfg_all.get("RSI_close") or cfg_all.get("RSI") or {})
        primary = (cfg.get("primary") or {})
        if not (cfg.get("enabled", False) and primary.get("enabled", False)):
            return None
        v = _to_float(row.get("RSI")) if hasattr(row, "get") else float("nan")
        if not np.isfinite(v):
            return None
        if side == "long":
            thr = float(primary.get("close_long", primary.get("overbought", 70.0)))
            return bool(v >= thr)
        else:
            thr = float(primary.get("close_short", primary.get("oversold", 30.0)))
            return bool(v <= thr)

    def _fear_greed_close_hit(self, row, side: str):
        cfg_all = getattr(self, "close_signals_cfg", {}) or {}
        cfg = (cfg_all.get("FEAR_GREED_close") or cfg_all.get("FEAR_GREED") or {})
        primary = (cfg.get("primary") or {})
        if not (cfg.get("enabled", False) and primary.get("enabled", False)):
            return None
        v = _to_float(row.get("FEAR_GREED")) if hasattr(row, "get") else float("nan")
        if not np.isfinite(v):
            return None
        if side == "long":
            thr = float(primary.get("close_long", 70.0))
            return bool(v >= thr)
        else:
            thr = float(primary.get("close_short", 30.0))
            return bool(v <= thr)

    def filter_open_with_close_signals(self, row, side: str) -> bool:
        """Return True if opening is allowed for given side; False if close-conditions already met."""
        cfg_all = getattr(self, "close_signals_cfg", {}) or {}
        if not cfg_all.get("enabled", False):
            return True
        side = (side or "").lower()
        if side not in ("long", "short"):
            return True

        blocks = []

        # BB_close as open filter
        cfg_bb = (cfg_all.get("BB_close") or cfg_all.get("BB_CLOSE") or {})
        primary_bb = (cfg_bb.get("primary") or {})
        if cfg_bb.get("enabled", False) and primary_bb.get("enabled", False):
            line = self._bb_close_line(row, side)
            if line is not None:
                price = _to_float(row.get("close")) if hasattr(row, "get") else float("nan")
                if np.isfinite(price):
                    if side == "long":
                        blocks.append(price >= line)
                    else:
                        blocks.append(price <= line)

        rsi_hit = self._rsi_close_hit(row, side)
        if rsi_hit is not None:
            blocks.append(bool(rsi_hit))

        fg_hit = self._fear_greed_close_hit(row, side)
        if fg_hit is not None:
            blocks.append(bool(fg_hit))

        if not blocks:
            return True

        required_all = bool(cfg_all.get("required_all", False))
        if required_all:
            blocked = all(blocks)
        else:
            blocked = any(blocks)
        return not blocked

    def close_position_signal(self, row, current_position):
        """Layer 2 exit logic: strategy-level close_signals (BB/RSI/FEAR_GREED)."""
        cfg_all = getattr(self, "close_signals_cfg", {}) or {}
        if not cfg_all.get("enabled", False):
            return None
        if current_position is None:
            return None

        side = str(current_position.get("side") or "").lower()
        if side not in ("long", "short"):
            return None

        hits = {}

        bb_hit = self._bb_close_hit(row, current_position)
        if bb_hit is not None:
            hits["BB_close"] = bool(bb_hit)

        rsi_hit = self._rsi_close_hit(row, side)
        if rsi_hit is not None:
            hits["RSI_close"] = bool(rsi_hit)

        fg_hit = self._fear_greed_close_hit(row, side)
        if fg_hit is not None:
            hits["FEAR_GREED_close"] = bool(fg_hit)

        if not hits:
            return None

        required_all = bool(cfg_all.get("required_all", False))
        if required_all:
            if not all(hits.values()):
                return None
        else:
            if not any(hits.values()):
                return None

        for key in ("BB_close", "RSI_close", "FEAR_GREED_close"):
            if hits.get(key):
                return {"signal_type": key}
        return None

    def get_required_base_need(self) -> int:
        cfg = self._indicator_config
        need = 1
        if cfg.get("RSI", {}).get("enabled", False):
            need = max(need, int(cfg["RSI"]["params"].get("window", 14)))
        if cfg.get("MA", {}).get("enabled", False):
            p = cfg["MA"]["params"]
            need = max(need, int(max(p.get("window_fast", 20), p.get("window_slow", 50))))
        if cfg.get("MACD", {}).get("enabled", False):
            need = max(need, int(cfg["MACD"]["params"].get("slow", 26)))
        if cfg.get("BB", {}).get("enabled", False):
            need = max(need, int(cfg["BB"]["params"].get("window", 20)))
        if cfg.get("ATR", {}).get("enabled", False):
            need = max(need, int(cfg["ATR"]["params"].get("window", 14)))
        # NEW:
        if cfg.get("STOCH", {}).get("enabled", False):
            need = max(need, int(cfg["STOCH"]["params"].get("k_window", 14)))
        if cfg.get("STOCH_RSI", {}).get("enabled", False):
            need = max(need, int(cfg["STOCH_RSI"]["params"].get("rsi_window", 14)))
        return int(need)

    def get_risk_levels(self, row) -> dict:
        try:
            price = float(row.get("close"))
        except Exception:
            return {}
        atr = row.get("ATR")
        atr = float(atr) if atr is not None and np.isfinite(atr) else np.nan

        if self.risk_mode.upper() == "ATR":
            k = self._risk_params_atr
            atr_val = atr if np.isfinite(atr) else 0.0  # ATR is in USD now
            return {
                "TP_LONG":  price + k["tp_k_long"]  * atr_val,
                "SL_LONG":  price - k["sl_k_long"]  * atr_val,
                "TP_SHORT": price - k["tp_k_short"] * atr_val,
                "SL_SHORT": price + k["sl_k_short"] * atr_val,
                "TS_LONG":  price - k["ts_k_long"]  * atr_val,
                "TS_SHORT": price + k["ts_k_short"] * atr_val,
            }
        else:
            rp = self._risk_params_fixed
            return {
                "TP_LONG": price * rp["tp_long"],
                "SL_LONG": price * rp["sl_long"],
                "TP_SHORT": price * rp["tp_short"],
                "SL_SHORT": price * rp["sl_short"],
                "TS_LONG": price * rp["trail_long"],
                "TS_SHORT": price * rp["trail_short"],
            }

    # =========================
    # INDICATORS (all here)
    # =========================
    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._ensure_time_index(df)
        if not isinstance(df.index, pd.DatetimeIndex) or df.empty:
            return df
        df = self._prepare_numeric_ohlc(df)
        if df.empty:
            return df

        if self.bucket_mode == "fixed":
            bucketed = self._bucketize_non_overlapping(df, self.indicator_bucket)
            ind_b = self._indicators_on_bucketed(bucketed)
            out = self._ffill_to_base(df, ind_b)
        else:
            # True rolling OHLC compression (stride=1)
            bucketed = self._bucketize_rolling_ohlc(df, self.indicator_bucket)
            out = self._indicators_on_bucketed(bucketed)
            out = out.reindex(df.index)  # keep warm-up NaNs

        try:
            self._last_price = float(df["close"].iloc[-1])
        except Exception:
            self._last_price = np.nan
        try:
            self._last_atr = float(out["ATR"].iloc[-1])
        except Exception:
            self._last_atr = np.nan

        # ---- Volume averages on BASE (independent of bucket) ----
        v_cfg = (self._indicator_config.get("VOLUME") or {})
        if _as_bool(v_cfg.get("enabled", False)):
            try:
                voldf = self._compute_volume_avgs_on_base(df)
                for col in ["VOL_AVG", "BUY_VOL_AVG", "VOL_SMA"]:
                    if col in voldf.columns:
                        out[col] = voldf[col].reindex(out.index)
            except Exception as _e:
                try:
                    logging.debug(f"[RSI] volume avgs skipped: {_e}")
                except Exception:
                    pass

        return out

    def _ensure_time_index(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        if not isinstance(df.index, pd.DatetimeIndex):
            try:
                if "time" in df.columns:
                    df = df.set_index(pd.to_datetime(df["time"], unit="ms", errors="coerce"))
                elif "timestamp" in df.columns:
                    df = df.set_index(pd.to_datetime(df["timestamp"], unit="ms", errors="coerce"))
                elif "close_time" in df.columns:
                    df = df.set_index(pd.to_datetime(df["close_time"], errors="coerce", utc=True))
            except Exception:
                pass
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index, errors="coerce", utc=True)
        df = df.sort_index()
        return df

    def _prepare_numeric_ohlc(self, df: pd.DataFrame) -> pd.DataFrame:
        cols = ["open", "high", "low", "close"]
        out = df.copy()
        for c in cols:
            if c in out.columns:
                out[c] = pd.to_numeric(out[c], errors="coerce")
        if "close" in out.columns and out["close"].isnull().any():
            out = out[out["close"].notnull()]
        return out

    def _bucketize_non_overlapping(self, df: pd.DataFrame, n: int) -> pd.DataFrame:
        if n <= 1 or len(df) == 0:
            return df[["open", "high", "low", "close"]].copy()
        pos = np.arange(len(df))
        groups = pos // n
        last_idx = df.index.to_series().groupby(groups).last()
        agg = df.groupby(groups).agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        bucket_idx = pd.DatetimeIndex(last_idx)
        if bucket_idx.tz is None and getattr(df.index, "tz", None) is not None:
            bucket_idx = bucket_idx.tz_localize(df.index.tz)
        agg.index = bucket_idx
        agg.sort_index(inplace=True)
        return agg

    def _bucketize_rolling_ohlc(self, df: pd.DataFrame, n: int) -> pd.DataFrame:
        """Robust rolling OHLC aggregation with stride=1.
        open = first (shifted by n-1), high = rolling.max(n), low = rolling.min(n), close = current
        First n-1 rows have NaN for open/high/low to preserve warm-up.
        """
        # Coerce to numeric early
        for c in ("open","high","low","close"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        if n is None or n <= 1:
            return df.loc[:, ["open","high","low","close"]].copy()
        out = pd.DataFrame(index=df.index)
        out["open"]  = df["open"].shift(n-1)
        out["high"]  = df["high"].rolling(window=n, min_periods=n).max()
        out["low"]   = df["low"].rolling(window=n, min_periods=n).min()
        out["close"] = df["close"]
        return out

    def _split_by_gaps(self, df: pd.DataFrame, gap_factor: float = 1.5):
        """
        Split index into continuous segments without large gaps.
        Threshold = gap_factor * median time step.
        Returns list of (start_idx, end_idx) integer slices.
        """
        if len(df) <= 1:
            return [(0, len(df))]
        idx = df.index.view("int64")
        diffs = np.diff(idx)
        step = np.median(diffs) if len(diffs) else 0
        if not np.isfinite(step) or step <= 0:
            return [(0, len(df))]
        thr = step * gap_factor
        cuts = np.where(diffs > thr)[0]
        start = 0
        segs = []
        for c in cuts:
            segs.append((start, c + 1))
            start = c + 1
        segs.append((start, len(df)))
        return segs

    def compute_indicators_segmented(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute indicators per continuous time segment (gap-aware).
        Indicators warm up from scratch after each gap.
        """

        # Allow disabling segmentation via config flag; fall back to continuous computation.
        if not getattr(_cfg, "RSI_SEGMENTATION_ENABLED", True):
            # Default behaviour: compute indicators on full continuous series, ignoring gaps.
            return self.compute_indicators(df)

        df = self._ensure_time_index(df)
        df = self._prepare_numeric_ohlc(df)
        if df.empty or not isinstance(df.index, pd.DatetimeIndex):
            return df

        segments = self._split_by_gaps(df)
        if getattr(_cfg, "RSI_SEGMENTATION_DEBUG", False):
            sym = "?"
            try:
                if "symbol" in df.columns:
                    sym_val = df["symbol"].iloc[0]
                    sym = str(sym_val)
            except Exception:
                pass
            logging.warning("[RSI][SEG] %s: segments=%d", sym, len(segments))

        parts = []
        for s, e in segments:
            seg = df.iloc[s:e]
            if seg.empty:
                continue
            parts.append(self.compute_indicators(seg))
        if not parts:
            return self._ensure_indicator_cols(df)

        out = pd.concat(parts, axis=0).reindex(df.index)
        try:
            self._last_price = float(df["close"].iloc[-1])
        except Exception:
            self._last_price = np.nan
        try:
            self._last_atr = float(out["ATR"].iloc[-1])
        except Exception:
            self._last_atr = np.nan
        return out

    def _indicators_on_bucketed(self, bucketed: pd.DataFrame) -> pd.DataFrame:
        """
        Compute all indicators on bucketed OHLC (either rolling or fixed result).
        Returns a DF with indicator columns aligned to bucketed index.
        """
        out = bucketed.copy()

        # Precompute numeric OHLC once to avoid repeated astype/to_numeric in every indicator block
        try:
            close = out["close"].astype(float)
        except Exception:
            close = pd.to_numeric(out.get("close", out.get("CLOSE")), errors="coerce")
        try:
            high = out["high"].astype(float)
        except Exception:
            high = pd.to_numeric(out.get("high", out.get("HIGH")), errors="coerce")
        try:
            low = out["low"].astype(float)
        except Exception:
            low = pd.to_numeric(out.get("low", out.get("LOW")), errors="coerce")

        # ----- RSI (Wilder EWM) -----

        rsi_cfg = self._indicator_config.get("RSI", {})
        if _as_bool(rsi_cfg.get("enabled", False)):
            win = int(rsi_cfg.get("params", {}).get("window", 14) or 14)
            try:
                close = close
                delta = close.diff()
                up = delta.clip(lower=0.0)
                down = (-delta).clip(lower=0.0)
                roll_up = up.ewm(alpha=1.0/win, adjust=False, min_periods=win).mean()
                roll_down = down.ewm(alpha=1.0/win, adjust=False, min_periods=win).mean()
                rs = roll_up / (roll_down + 1e-12)
                out["RSI"] = 100.0 - (100.0 / (1.0 + rs))
            except Exception:
                out["RSI"] = np.nan

        # ----- MA (fast/slow) from CLOSE, unified with BB mid when SMA & same window -----
        ma_cfg = self._indicator_config.get("MA", {})
        if _as_bool(ma_cfg.get("enabled", False)):
            p = ma_cfg.get("params", {})
            w_fast = int(p.get("window_fast", 20) or 20)
            w_slow = int(p.get("window_slow", 50) or 50)
            ma_type = str(p.get("type", "SMA")).upper()
            try:
                close = close
                if ma_type == "EMA":
                    out["MA_FAST"] = _ema_close(close, w_fast)
                    out["MA_SLOW"] = _ema_close(close, w_slow)
                else:
                    out["MA_FAST"] = _sma_close(close, w_fast)
                    out["MA_SLOW"] = _sma_close(close, w_slow)
            except Exception:
                out["MA_FAST"] = np.nan
                out["MA_SLOW"] = np.nan

        # ----- MACD -----
        macd_cfg = self._indicator_config.get("MACD", {})
        if _as_bool(macd_cfg.get("enabled", False)):
            p = macd_cfg.get("params", {})
            fast = int(p.get("fast", 12) or 12)
            slow = int(p.get("slow", 26) or 26)
            signal = int(p.get("signal", 9) or 9)
            try:
                macd_line = ta.trend.macd(out["close"], window_slow=slow, window_fast=fast)
                macd_signal = ta.trend.macd_signal(out["close"], window_slow=slow, window_fast=fast, window_sign=signal)
                out["MACD"] = macd_line
                out["MACD_SIGNAL"] = macd_signal
                out["MACD_HIST"] = (macd_line - macd_signal)
            except Exception:
                out["MACD"] = np.nan
                out["MACD_SIGNAL"] = np.nan
                out["MACD_HIST"] = np.nan

        # ----- Bollinger Bands unified (BB mid = SMA(close, window)) -----
        bb_cfg = self._indicator_config.get("BB", {})
        if _as_bool(bb_cfg.get("enabled", False)):
            p = bb_cfg.get("params", {})
            win = int(p.get("window", 20) or 20)
            nstd = float(p.get("n_std", p.get("stdev", 2.0)) or 2.0)
            try:
                close = close
                mid = _sma_close(close, win)
                std = close.rolling(window=win, min_periods=win).std(ddof=0)
                out["BB_MIDDLE"] = mid
                out["BB_UPPER"] = (mid + nstd * std)
                out["BB_LOWER"] = (mid - nstd * std)
            except Exception:
                out["BB_MIDDLE"] = np.nan
                out["BB_UPPER"] = np.nan
                out["BB_LOWER"] = np.nan

        # ----- ATR (Wilder TR/ATR) -----
        atr_cfg = self._indicator_config.get("ATR", {})
        if _as_bool(atr_cfg.get("enabled", False)):
            p = atr_cfg.get("params", {})
            win = int(p.get("window", 14) or 14)
            try:
                high = high
                low  = low
                close = close
                prev_close = close.shift(1)
                range1 = (high - low).abs()
                range2 = (high - prev_close).abs()
                range3 = (low - prev_close).abs()
                tr = pd.concat([range1, range2, range3], axis=1).max(axis=1)
                atr = tr.ewm(alpha=1.0/win, adjust=False, min_periods=win).mean()
                if _as_bool(p.get("as_percent", False)):
                    out["ATR"] = (atr / close).astype(float)
                else:
                    out["ATR"] = atr.astype(float)
            
                # Always compute ATR_PCT for DB:
                out["ATR_PCT"] = (pd.to_numeric(out["ATR"], errors="coerce") / close) * 100.0
            except Exception:
                out["ATR"] = np.nan

        # ===== NEW INDICATORS =====
        # ----- PCT_CHANGE (close-to-close), in percentage points -----
        pc_cfg = self._indicator_config.get("PCT_CHANGE", {})
        if _as_bool(pc_cfg.get("enabled", False)):
            try:
                close = close
                out["PCT_CHANGE"] = (close.pct_change() * 100.0).astype(float)
            except Exception:
                out["PCT_CHANGE"] = np.nan

        # ----- FEAR_GREED placeholder (daily), engine will merge real values -----
        fg_cfg = self._indicator_config.get("FEAR_GREED", {})
        if _as_bool(fg_cfg.get("enabled", False)) and "FEAR_GREED" not in out.columns:
            out["FEAR_GREED"] = np.nan

        # ----- Stochastic Oscillator (classic %K/%D) -----
        st_cfg = self._indicator_config.get("STOCH", {})
        if _as_bool(st_cfg.get("enabled", False)):
            pp = st_cfg.get("params", {})
            kw = int(pp.get("k_window", 14) or 14)
            dw = int(pp.get("d_window", 3) or 3)
            sk = int(pp.get("smooth_k", 3) or 3)
            try:
                st = ta.momentum.StochasticOscillator(high=out["high"], low=out["low"], close=out["close"],
                                                      window=kw, smooth_window=sk)
                k = st.stoch()
                d = k.rolling(window=dw, min_periods=dw).mean()
                out["STOCH_K"] = k
                out["STOCH_D"] = d
            except Exception:
                # manual fallback
                try:
                    lowest = out["low"].rolling(window=kw, min_periods=kw).min()
                    highest = out["high"].rolling(window=kw, min_periods=kw).max()
                    k_raw = (out["close"] - lowest) / (highest - lowest + 1e-12) * 100.0
                    k = k_raw.rolling(window=sk, min_periods=sk).mean()
                    d = k.rolling(window=dw, min_periods=dw).mean()
                    out["STOCH_K"] = k
                    out["STOCH_D"] = d
                except Exception:
                    out["STOCH_K"] = np.nan
                    out["STOCH_D"] = np.nan

        # ----- Stochastic RSI -----
        sr_cfg = self._indicator_config.get("STOCH_RSI", {})
        if _as_bool(sr_cfg.get("enabled", False)):
            pp = sr_cfg.get("params", {})
            rw = int(pp.get("rsi_window", 14) or 14)
            sw = int(pp.get("stoch_window", 14) or 14)
            sk = int(pp.get("smooth_k", 3) or 3)
            dw = int(pp.get("d_window", 3) or 3)
            try:
                # compute RSI first
                cl = close
                delta = cl.diff()
                up = delta.clip(lower=0.0)
                down = (-delta).clip(lower=0.0)
                roll_up = up.ewm(alpha=1.0/rw, adjust=False, min_periods=rw).mean()
                roll_down = down.ewm(alpha=1.0/rw, adjust=False, min_periods=rw).mean()
                rs = roll_up / (roll_down + 1e-12)
                rsi = 100.0 - (100.0 / (1.0 + rs))
                rsi_min = rsi.rolling(window=sw, min_periods=sw).min()
                rsi_max = rsi.rolling(window=sw, min_periods=sw).max()
                k_raw = (rsi - rsi_min) / (rsi_max - rsi_min + 1e-12) * 100.0
                k = k_raw.rolling(window=sk, min_periods=sk).mean()
                d = k.rolling(window=dw, min_periods=dw).mean()
                out["STOCHRSI_K"] = k
                out["STOCHRSI_D"] = d
            except Exception:
                out["STOCHRSI_K"] = np.nan
                out["STOCHRSI_D"] = np.nan

        # Ensure all expected columns exist
        out = self._ensure_indicator_cols(out)
        return out

    def _ffill_to_base(self, base: pd.DataFrame, src: pd.DataFrame) -> pd.DataFrame:
        out = self._ensure_indicator_cols(base)
        if src is None or src.empty:
            return out
        out = out.sort_index()
        src = src.sort_index()
        for col in [c for c in src.columns if c in INDICATOR_OUTPUT_COLUMNS]:
            out[col] = src[col].reindex(out.index, method="ffill")
        return out

    def _rolling_stride_one_segment(self, seg: pd.DataFrame, b: int) -> pd.DataFrame:
        if len(seg) == 0:
            return self._ensure_indicator_cols(seg)

        out = pd.DataFrame(index=seg.index, columns=INDICATOR_OUTPUT_COLUMNS, dtype=float)

        # longest needed lookback among enabled indicators
        cfg = self._indicator_config
        maxneed = 1
        if cfg.get("RSI", {}).get("enabled", False):
            maxneed = max(maxneed, int(cfg["RSI"]["params"].get("window", 14)))
        if cfg.get("MA", {}).get("enabled", False):
            p = cfg["MA"]["params"]
            maxneed = max(maxneed, int(max(p.get("window_fast", 20), p.get("window_slow", 50))))
        if cfg.get("MACD", {}).get("enabled", False):
            maxneed = max(maxneed, int(cfg["MACD"]["params"].get("slow", 26)))
        if cfg.get("BB", {}).get("enabled", False):
            maxneed = max(maxneed, int(cfg["BB"]["params"].get("window", 20)))
        if cfg.get("ATR", {}).get("enabled", False):
            maxneed = max(maxneed, int(cfg["ATR"]["params"].get("window", 14)))
        # new indicators
        if cfg.get("STOCH", {}).get("enabled", False):
            maxneed = max(maxneed, int(cfg["STOCH"]["params"].get("k_window", 14)))
        if cfg.get("STOCH_RSI", {}).get("enabled", False):
            maxneed = max(maxneed, int(cfg["STOCH_RSI"]["params"].get("rsi_window", 14)))

        idx = seg.index
        for i in range(len(seg)):
            take = list(range(i, -1, -b))[: max(3, maxneed + 2)]
            chunk = seg.iloc[take].sort_index()
            got = self._indicators_on_bucketed(chunk)
            for c in [c for c in got.columns if c in INDICATOR_OUTPUT_COLUMNS]:
                out.loc[idx[i], c] = got.iloc[-1][c]

        return out

    def _ensure_indicator_cols(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        for col in INDICATOR_OUTPUT_COLUMNS:
            if col not in df.columns:
                df[col] = np.nan
        return df

    def _compute_volume_avgs_on_base(self, base: pd.DataFrame) -> pd.DataFrame:
        """Compute VOL_AVG, BUY_VOL_AVG on *base* candles (ignoring buckets)
        and VOL_SMA over confirm.window; align to base index."""
        out = pd.DataFrame(index=base.index.copy())
        try:
            vol = pd.to_numeric(base.get("volume_quote"), errors="coerce")
        except Exception:
            vol = pd.Series(np.nan, index=base.index)
        try:
            buy = pd.to_numeric(base.get("taker_buy_volume_quote"), errors="coerce")
        except Exception:
            buy = pd.Series(np.nan, index=base.index)
        v_cfg = (self._indicator_config.get("VOLUME") or {})
        p = (v_cfg.get("params") or {})
        w = int(p.get("window", 1) or 1)
        cwin = int((v_cfg.get("confirm") or {}).get("window", 1440) or 1440)
        # rolling means (NaNs preserved until window filled)
        out["VOL_AVG"] = vol.rolling(window=w, min_periods=max(1, w)).mean()
        out["BUY_VOL_AVG"] = buy.rolling(window=w, min_periods=max(1, w)).mean()
        out["VOL_SMA"] = vol.rolling(window=cwin, min_periods=max(1, cwin)).mean()
        return out

    # ---------- internal helpers for MACD trend / history ----------
    def _append_macd_hist(self, symbol: str, h_now):
        try:
            h = float(h_now)
        except Exception:
            return
        if not np.isfinite(h):
            return
        dq = self._state.get("macd_hist_window", {}).get(symbol)
        if dq is None:
            # create if missing (should exist from __init__)
            self._state.setdefault("macd_hist_window", {})[symbol] = deque(maxlen=64)
            dq = self._state["macd_hist_window"][symbol]
        dq.append(h)

    def _macd_trend_ok(self, symbol: str, hist_now, side: str, n: int) -> bool:
        """For confirmations with 'rising_n':
        LONG requires MACD_HIST rising in last n bars (hist_now > hist_{t-n}).
        SHORT requires falling (hist_now < hist_{t-n}).
        """
        dq = self._state.get("macd_hist_window", {}).get(symbol)
        try:
            h_now = float(hist_now)
        except Exception:
            return False
        if dq is None or len(dq) <= n or not np.isfinite(h_now):
            return False
        then = dq[-(n + 1)]
        eps = 1e-12
        if side == "long":
            return h_now > (then + eps)
        else:
            return h_now < (then - eps)

    # =========================
    # ENTRY / EXIT
    # =========================
    def open_position_signal(self, row, current_position):
        if row is None or isinstance(row, float) or not hasattr(row, "__getitem__"):
            return None
        try:
            price = float(row["close"])
        except Exception:
            return None

        symbol = str(row.get("symbol", "")) if hasattr(row, "get") else ""
        # keep MACD histogram history for rising_n/falling_n confirmations
        self._append_macd_hist(symbol, row.get("MACD_HIST"))

        # If we already have a position on this symbol, do NOT keep/create pending.
        # Still update internal primary state (last values) so nothing fires late after close.
        if current_position is not None:
            self._primary_signal_ok(row, symbol, blocked=True)
            return None

        long_ok, short_ok = self._primary_signal_ok(row, symbol, blocked=False)
        if long_ok or short_ok:
            long_ok, short_ok = self._apply_confirmations(row, long_ok, short_ok, symbol)

        if not long_ok and not short_ok:
            return None

        amount = POSITION_SIZE / price if price else 0.0
        entry_atr = row.get("ATR")
        entry_atr = float(entry_atr) if entry_atr is not None and np.isfinite(entry_atr) else np.nan

        if long_ok and not short_ok:
            sig = {"signal_type": "open_long", "amount": amount}
        elif short_ok and not long_ok:
            sig = {"signal_type": "open_short", "amount": amount}
        else:
            return None

        if STORE_ENTRY_ATR and np.isfinite(entry_atr):
            sig["entry_atr"] = entry_atr
        return sig

    def _primary_signal_ok(self, row, symbol: str, blocked: bool = False):
        primary = (self.primary_signal or "").strip().replace("-", "_").upper()
        if primary == "MA":
            return self._primary_ma(row, symbol, blocked=blocked)
        elif primary == "MACD":
            return self._primary_macd(row, symbol, blocked=blocked)
        elif primary == "BB":
            return self._primary_bb(row, symbol)
        elif primary in ("ATR", "ATR_PRIMARY", "ATR_SPIKE"):
            return self._primary_atr(row, symbol, blocked=blocked)
        elif primary == "STOCH":
            return self._primary_stoch_like(row, symbol, "STOCH")
        elif primary == "STOCH_RSI":
            return self._primary_stoch_like(row, symbol, "STOCH_RSI")
        elif primary == "ATR_PCT":
            return self._primary_atr_pct(row, symbol, blocked=blocked)
        elif primary == "PCT_CHANGE":
            return self._primary_pct_change(row, symbol)
        elif primary == "FEAR_GREED":
            return self._primary_fear_greed(row, symbol)
        elif primary == "VOLUME":
            return self._primary_volume(row, symbol, blocked=blocked)
        else:
            return self._primary_rsi(row)

    def _primary_rsi(self, row):
        rsi_cfg = self._indicator_config["RSI"]["primary"]
        if not rsi_cfg.get("enabled", True):
            return (False, False)
        rsi = row.get("RSI")
        if not np.isfinite(rsi):
            return (False, False)
        long_ok = bool(rsi <= float(rsi_cfg.get("oversold", 20.0)))
        short_ok = bool(rsi >= float(rsi_cfg.get("overbought", 80.0)))
        return (long_ok, short_ok)

    def _primary_ma(self, row, symbol: str, blocked: bool = False):
        """
        MA / price-vs-MA crossover with optional confirmation_bars (bars AFTER cross).
        """
        p = self._indicator_config.get("MA", {}).get("primary", {})
        if not p.get("enabled", False):
            return (False, False)

        confirm = int(p.get("confirmation_bars", 0) or 0)
        ma_fast = row.get("MA_FAST")
        ma_slow = row.get("MA_SLOW")
        price   = row.get("close")
        if not all(np.isfinite(v) for v in [ma_fast, ma_slow, price]):
            return (False, False)

        prev = self._state.setdefault("ma_prev", {}).get(symbol, {})
        state = self._state.setdefault("ma_conf_state", {})
        pend  = state.get(symbol)

        # If blocked by an existing position: clear pending, update prev values, and return no signal
        if blocked:
            self._state.setdefault("ma_conf_state", {})[symbol] = None
            self._state.setdefault("ma_prev", {})[symbol] = {"fast": float(ma_fast), "slow": float(ma_slow), "price": float(price)}
            return (False, False)

        t = str(p.get("type", "")).lower()
        long_cross = short_cross = False

        if t == "ma_cross_bullish":
            if "fast" in prev and "slow" in prev:
                long_cross = (prev["fast"] <= prev["slow"] and ma_fast > ma_slow)
        elif t == "ma_cross_bearish":
            if "fast" in prev and "slow" in prev:
                short_cross = (prev["fast"] >= prev["slow"] and ma_fast < ma_slow)
        elif t in ("price_ma_cross_bullish", "price_ma_cross_bearish"):
            which = str(p.get("price_ma", "fast")).lower()
            if "price" in prev and "fast" in prev and "slow" in prev:
                ref_prev = prev["fast"] if which == "fast" else prev["slow"]
                ref_now  = ma_fast if which == "fast" else ma_slow
                if t == "price_ma_cross_bullish":
                    long_cross  = (prev["price"] <= ref_prev and price > ref_now)
                else:
                    short_cross = (prev["price"] >= ref_prev and price < ref_now)
        else:
            if "fast" in prev and "slow" in prev:
                long_cross  = (prev["fast"] <= prev["slow"] and ma_fast > ma_slow)
                short_cross = (prev["fast"] >= prev["slow"] and ma_fast < ma_slow)

        long_ok = short_ok = False
        if long_cross:
            pend = {"side": "long", "count": 0} if confirm > 0 else None
            if confirm == 0:
                long_ok = True
        elif short_cross:
            pend = {"side": "short", "count": 0} if confirm > 0 else None
            if confirm == 0:
                short_ok = True

        if pend is not None and confirm > 0:
            if pend["side"] == "long":
                if t.startswith("price_ma"):
                    which = str(p.get("price_ma", "fast")).lower()
                    ref_now = ma_fast if which == "fast" else ma_slow
                    cond = bool(price > ref_now)
                else:
                    cond = bool(ma_fast > ma_slow)
                if cond:
                    pend["count"] += 1
                    if pend["count"] >= confirm:
                        long_ok = True
                        pend = None
                else:
                    pend = None
            else:
                if t.startswith("price_ma"):
                    which = str(p.get("price_ma", "fast")).lower()
                    ref_now = ma_fast if which == "fast" else ma_slow
                    cond = bool(price < ref_now)
                else:
                    cond = bool(ma_fast < ma_slow)
                if cond:
                    pend["count"] += 1
                    if pend["count"] >= confirm:
                        short_ok = True
                        pend = None
                else:
                    pend = None

        state[symbol] = pend
        self._state.setdefault("ma_prev", {})[symbol] = {"fast": float(ma_fast), "slow": float(ma_slow), "price": float(price)}
        return (long_ok, short_ok)

    def _primary_macd(self, row, symbol: str, blocked: bool = False):
        """
        MACD histogram zero-cross with confirmation_bars:
        Cross sets pending(count=0). Require `confirm` subsequent bars with histogram retaining the new sign.
        """
        cfg = self._indicator_config.get("MACD", {}).get("primary", {}) or {}
        confirm = cfg.get("confirmation_bars", cfg.get("confirm_bars", 0))
        try:
            confirm = int(confirm or 0)
        except Exception:
            confirm = 0

        h = row.get("MACD_HIST")
        try:
            h = float(h)
        except Exception:
            return False, False

        st = self._state.setdefault("macd_state", {})
        last_h = st.get((symbol, "last_h"))
        pend = st.get((symbol, "pending"))

        # While a position is open we do not create/keep pendings.
        # We still update last_h to keep state fresh.
        if blocked:
            st[(symbol, "pending")] = None
            st[(symbol, "last_h")] = h
            return False, False

        long_ok = short_ok = False

        if last_h is not None and np.isfinite(last_h):
            crossed_up = (last_h <= 0.0 and h > 0.0)
            crossed_down = (last_h >= 0.0 and h < 0.0)
            if crossed_up:
                pend = {"side": "long", "count": 0} if confirm > 0 else None
                if confirm == 0:
                    long_ok = True
            elif crossed_down:
                pend = {"side": "short", "count": 0} if confirm > 0 else None
                if confirm == 0:
                    short_ok = True

        if pend is not None and confirm > 0:
            if pend["side"] == "long":
                if h > 0.0:
                    pend["count"] += 1
                    if pend["count"] >= confirm:
                        long_ok = True
                        pend = None
                else:
                    pend = None
            else:
                if h < 0.0:
                    pend["count"] += 1
                    if pend["count"] >= confirm:
                        short_ok = True
                        pend = None
                else:
                    pend = None

        st[(symbol, "pending")] = pend
        st[(symbol, "last_h")] = h
        
        # PCT_CHANGE confirm
        try:
            pc_conf = self._indicator_config.get("PCT_CHANGE", {}).get("confirm", {})
            if pc_conf.get("enabled", False):
                v = _to_float(row.get("PCT_CHANGE"))
                if np.isfinite(v):
                    ok = abs(v) >= float(pc_conf.get("min_abs", 0.35))
                    if long_ok:  long_ok  = ok if pc_conf.get("combine","and").lower()!="or" else (long_ok or ok)
                    if short_ok: short_ok = ok if pc_conf.get("combine","and").lower()!="or" else (short_ok or ok)
        except Exception:
            pass

        # FEAR_GREED confirm (with inverted)
        try:
            fg_conf = self._indicator_config.get("FEAR_GREED", {}).get("confirm", {})
            if fg_conf.get("enabled", False):
                v = _to_float(row.get("FEAR_GREED"))
                if np.isfinite(v):
                    lmax = float(fg_conf.get("long_max", 40.0))
                    smin = float(fg_conf.get("short_min", 60.0))
                    lc = (v < lmax)
                    sc = (v > smin)
                    if bool(fg_conf.get("inverted", False)):
                        lc, sc = sc, lc
                    if long_ok:  long_ok  = long_ok and lc
                    if short_ok: short_ok = short_ok and sc
        except Exception:
            pass

        # PCT_CHANGE confirm
        pc_conf = self._indicator_config.get("PCT_CHANGE", {}).get("confirm", {})
        if pc_conf.get("enabled", False):
            v = _to_float(row.get("PCT_CHANGE"))
            if np.isfinite(v):
                ok = abs(v) >= float(pc_conf.get("min_abs", 0.35))
                if long_ok:  long_ok  = ok if pc_conf.get("combine","and").lower()!="or" else (long_ok or ok)
                if short_ok: short_ok = ok if pc_conf.get("combine","and").lower()!="or" else (short_ok or ok)

        # FEAR_GREED confirm
        fg_conf = self._indicator_config.get("FEAR_GREED", {}).get("confirm", {})
        if fg_conf.get("enabled", False):
            v = _to_float(row.get("FEAR_GREED"))
            if np.isfinite(v):
                lc = (v < float(fg_conf.get("long_max", 40.0)))
                sc = (v > float(fg_conf.get("short_min", 60.0)))
                if bool(fg_conf.get("inverted", False)):
                    lc, sc = sc, lc
                if long_ok:  long_ok  = long_ok and lc
                if short_ok: short_ok = short_ok and sc

        return long_ok, short_ok

    def _primary_bb(self, row, symbol: str):
        cfg = self._indicator_config.get("BB", {}).get("primary", {})
        if not cfg or not cfg.get("enabled", False):
            return (False, False)

        up = _to_float(row.get("BB_UPPER"))
        mid = _to_float(row.get("BB_MIDDLE"))
        lo  = _to_float(row.get("BB_LOWER"))
        price = _to_float(row.get("close"))
        if not all(np.isfinite(v) for v in [up, mid, lo, price]) or (up - lo) <= 0:
            return (False, False)

        rng = float(up) - float(lo)
        off = float(cfg.get("mid_offset", 0.35)) * rng
        long_level  = float(mid) - off
        short_level = float(mid) + off
        use_cross   = bool(cfg.get("use_cross", True))
        inverted    = bool(cfg.get("inverted", False))

        prev_price = self._prev_price.get(symbol, None)
        if use_cross and prev_price is not None and np.isfinite(prev_price):
            long_ok  = (prev_price >= long_level) and (price <  long_level)
            short_ok = (prev_price <= short_level) and (price >  short_level)
        else:
            long_ok  = (price <= long_level)
            short_ok = (price >= short_level)

        if inverted:
            long_ok, short_ok = short_ok, long_ok

        try:
            self._prev_price[symbol] = float(price)
        except Exception:
            pass
        return (long_ok, short_ok)

    
    def _primary_pct_change(self, row, symbol: str):
        cfg = self._indicator_config.get("PCT_CHANGE", {}).get("primary", {})
        if not cfg or not cfg.get("enabled", False):
            return (False, False)
        v = _to_float(row.get("PCT_CHANGE"))
        if not np.isfinite(v):
            return (False, False)
        up_th = float(cfg.get("short_above", 0.5))
        dn_th = float(cfg.get("long_below", -0.5))
        long_ok  = (v <= dn_th)
        short_ok = (v >= up_th)
        if bool(cfg.get("inverted", False)):
            long_ok, short_ok = short_ok, long_ok
        return (long_ok, short_ok)

    def _primary_fear_greed(self, row, symbol: str):
        cfg = self._indicator_config.get("FEAR_GREED", {}).get("primary", {})
        if not cfg or not cfg.get("enabled", False):
            return (False, False)
        v = _to_float(row.get("FEAR_GREED"))
        if not np.isfinite(v):
            return (False, False)
        long_ok  = (v < float(cfg.get("long_max", 80.0)))
        short_ok = (v > float(cfg.get("short_min", 20.0)))
        if bool(cfg.get("inverted", False)):
            long_ok, short_ok = short_ok, long_ok
        return (long_ok, short_ok)

    def _primary_atr(self, row, symbol: str, blocked: bool = False):
        """
        ATR spike primary signal with direction based on MA_FAST (not candle).
        Trigger: ATR_now > multiplier * avg(ATR of last `avg_window` previous bars), excluding current.
        Direction:
            LONG  if close < MA_FAST
            SHORT if close > MA_FAST
            Equal -> no direction
        Optional ATR confirm: use ATR.confirm.use_ma_fast (+strict) to additionally gate direction.
        """
        atr_cfg  = self._indicator_config.get("ATR", {}).get("primary", {}) or {}
        atr_conf = self._indicator_config.get("ATR", {}).get("confirm", {}) or {}

        # Warm-up when disabled: keep queues updated, but no signal.
        if not atr_cfg.get("enabled", False):
            atr_now = _to_float(row.get("ATR"))
            dq = self._state.setdefault("atr_prev", {}).setdefault(
                symbol, deque(maxlen=int(atr_cfg.get("avg_window", 5) or 5))
            )
            if np.isfinite(atr_now):
                dq.append(float(atr_now))
            c = _to_float(row.get("close"))
            if np.isfinite(c):
                self._state.setdefault("prev_close", {})[symbol] = c
            return (False, False)

    def _primary_atr_pct(self, row, symbol: str, blocked: bool = False):
        """
        Primary signal based on ATR_PCT (ATR/close * 100).

        Config:
          INDICATOR_OVERRIDES["ATR_PCT"]["primary"] = {
              "enabled": True/False,
              "threshold": float,   # np. 0.35 == 0.35%
              "MA": "ma_fast" | "ma_slow",
              "inverted": True/False  # True=breakout/continuation; False=mean reversion
          }
          INDICATOR_OVERRIDES["ATR_PCT"]["confirm"] = {
              "enabled": True/False,
              "min_pct_of_close": float,  # np. 0.0035 -> 0.35%
              "combine": "and"
          }
        Returns: (long_ok, short_ok)
        """
        cfg = getattr(self, "_indicator_config", {}) or {}
        prim = ((cfg.get("ATR_PCT") or {}).get("primary") or {})
        if not prim.get("enabled", False):
            return (False, False)

        # --- Read values as floats (no strings/None) ---
        def _f(x):
            try:
                return float(x)
            except Exception:
                return None

        atr_pct = _f(row.get("ATR_PCT"))
        close = _f(row.get("close") if row.get("close") is not None else row.get("close_price"))

        # choose MA column
        ma_key = str(prim.get("MA", "ma_fast")).strip().lower()
        ma_col = "MA_FAST" if ma_key in ("ma_fast", "fast") else "MA_SLOW"
        ma_val = _f(row.get(ma_col))

        # require valid inputs
        if atr_pct is None or close in (None, 0.0) or ma_val is None:
            return (False, False)

        # --- Primary threshold on ATR_PCT (% units already) ---
        thr = _f(prim.get("threshold"))
        if thr is None:
            thr = 0.0
        if atr_pct < thr:
            return (False, False)

        # --- Optional confirmation: min % of close (fraction) ---
        conf = ((cfg.get("ATR_PCT") or {}).get("confirm") or {})
        if conf.get("enabled", False):
            min_frac = _f(conf.get("min_pct_of_close")) or 0.0  # e.g. 0.0035 -> 0.35%
            if atr_pct < (min_frac * 100.0):
                return (False, False)

        inverted = bool(prim.get("inverted", False))

        # ---- Direction: mutual exclusive & deterministic ----
        # close == ma -> brak sygnału (żeby nie „flappować” na granicy)
        if close > ma_val:
            # powyżej MA
            if inverted:
                long_ok, short_ok = True, False  # breakout/continuation
            else:
                long_ok, short_ok = False, True  # mean reversion
        elif close < ma_val:
            # poniżej MA
            if inverted:
                long_ok, short_ok = False, True  # breakout/continuation
            else:
                long_ok, short_ok = True, False  # mean reversion
        else:
            long_ok, short_ok = False, False

        if blocked:
            return (False, False)
        return (long_ok, short_ok)

    # --- NEW: STOCH/ STOCH_RSI cross-primary (generic) ---
    def _primary_stoch_like(self, row, symbol: str, which: str):
        if which == "STOCH":
            cfg = self._indicator_config.get("STOCH", {}).get("primary", {})
            if not _as_bool(cfg.get("enabled", True)):
                return (False, False)
            k = _to_float(row.get("STOCH_K")); d = _to_float(row.get("STOCH_D"))
            state_key = ("STOCH", symbol)
        else:
            cfg = self._indicator_config.get("STOCH_RSI", {}).get("primary", {})
            if not _as_bool(cfg.get("enabled", True)):
                return (False, False)
            k = _to_float(row.get("STOCHRSI_K")); d = _to_float(row.get("STOCHRSI_D"))
            state_key = ("STOCH_RSI", symbol)

        prev = self._state["stoch_prev"].get(state_key, None)
        long_ok = short_ok = False
        if prev and np.isfinite(prev.get("k", np.nan)) and np.isfinite(prev.get("d", np.nan)) and np.isfinite(k) and np.isfinite(d):
            long_ok  = (prev["k"] <= prev["d"] and k > d)   # cross up
            short_ok = (prev["k"] >= prev["d"] and k < d)   # cross down

        self._state["stoch_prev"][state_key] = {"k": k, "d": d}
        return (long_ok, short_ok)

    def _primary_volume(self, row, symbol: str, blocked: bool = False):
        """
        PRIMARY (dwukierunkowy) na krótkoterminowych średnich:
          ratio = BUY_VOL_AVG / VOL_AVG

        Normalnie:
          - LONG  gdy ratio >= long_min_ratio  (np. 0.75)
          - SHORT gdy ratio <= short_max_ratio (np. 0.25)

        Gdy inverted=True:
          - LONG  gdy ratio <= short_max_ratio
          - SHORT gdy ratio >= long_min_ratio
        """
        v_cfg = (self._indicator_config.get("VOLUME") or {})
        prim = (v_cfg.get("primary") or {})
        if not prim.get("enabled", False):
            return (False, False)

        vol_avg = _to_float(row.get("VOL_AVG"))
        buy_avg = _to_float(row.get("BUY_VOL_AVG"))
        if not (np.isfinite(vol_avg) and np.isfinite(buy_avg)) or vol_avg <= 0.0:
            return (False, False)

        ratio = buy_avg / vol_avg
        long_thr = float(prim.get("long_min_ratio", 0.75))
        short_thr = float(prim.get("short_max_ratio", 0.25))
        inverted = bool(prim.get("inverted", False))

        if not inverted:
            long_ok = (ratio >= long_thr)
            short_ok = (ratio <= short_thr)
        else:
            long_ok = (ratio <= short_thr)
            short_ok = (ratio >= long_thr)

        # Jeśli przez złe progi oba wyjdą True — wybierz deterministycznie „silniejszy” sygnał.
        if long_ok and short_ok:
            if not inverted:
                gap_long = ratio - long_thr  # >= 0 im mocniej powyżej 0.75
                gap_short = short_thr - ratio  # >= 0 im mocniej poniżej 0.25
            else:
                # odwrócone znaczenie kierunków
                gap_long = short_thr - ratio  # LONG tym silniejszy im ratio niżej od short_thr
                gap_short = ratio - long_thr  # SHORT tym silniejszy im ratio wyżej od long_thr
            if gap_long >= gap_short:
                short_ok = False
            else:
                long_ok = False

        if blocked:
            return (False, False)
        return (long_ok, short_ok)

    def _apply_confirmations(self, row, long_ok: bool, short_ok: bool, symbol: str):
        # RSI bias
        rsi_conf = self._indicator_config["RSI"]["confirm"]
        rsi_val = row.get("RSI")
        if rsi_conf.get("enabled", True) and np.isfinite(rsi_val):
            if rsi_conf.get("use_level_50", False):
                if long_ok and not (rsi_val < 50.0):
                    long_ok = False
                if short_ok and not (rsi_val > 50.0):
                    short_ok = False
            else:
                lmax = float(rsi_conf.get("long_max", 50.0))
                smin = float(rsi_conf.get("short_min", 50.0))
                if long_ok and not (rsi_val <= lmax):
                    long_ok = False
                if short_ok and not (rsi_val >= smin):
                    short_ok = False

        # MA confirmation
        ma_conf = self._indicator_config["MA"]["confirm"]
        if ma_conf.get("enabled", True):
            ma_fast = _to_float(row.get("MA_FAST"))
            ma_slow = _to_float(row.get("MA_SLOW"))
            price = _to_float(row.get("close"))
            arr = np.array([ma_fast, ma_slow, price], dtype=float)
            if np.isfinite(arr).all():
                long_ok = long_ok and self._confirm_ma_side(ma_conf, ma_fast, ma_slow, price, side="long")
                short_ok = short_ok and self._confirm_ma_side(ma_conf, ma_fast, ma_slow, price, side="short")

        # MACD confirmation (side/trend)
        macd_conf = self._indicator_config["MACD"]["confirm"]
        if macd_conf.get("enabled", True):
            m = _to_float(row.get("MACD"))
            s = _to_float(row.get("MACD_SIGNAL"))
            arr = np.array([m, s], dtype=float)
            if np.isfinite(arr).all():
                long_ok = long_ok and self._confirm_macd_side(macd_conf, m, s, side="long", row=row)
                short_ok = short_ok and self._confirm_macd_side(macd_conf, m, s, side="short", row=row)

                # 'rising_n' trend bias
                n = int(macd_conf.get("rising_n", 0) or 0)
                if n > 0:
                    h_now = row.get("MACD_HIST")
                    if long_ok and not self._macd_trend_ok(symbol, h_now, "long", n):
                        long_ok = False
                    if short_ok and not self._macd_trend_ok(symbol, h_now, "short", n):
                        short_ok = False

        # BB confirmation (bias around middle)
        bb_conf = self._indicator_config["BB"]["confirm"]
        if bb_conf.get("enabled", True):
            up = row.get("BB_UPPER"); mid = row.get("BB_MIDDLE"); lo = row.get("BB_LOWER"); price = row.get("close")
            arr = np.array([up, mid, lo, price], dtype=float)
            if np.isfinite(arr).all() and (up - lo) > 0:
                rng = float(up) - float(lo)
                mid_off = float(bb_conf.get("mid_offset", 0.10)) * rng
                long_level = float(mid) - mid_off
                short_level = float(mid) + mid_off
                if long_ok and not (price <= long_level):
                    long_ok = False
                if short_ok and not (price >= short_level):
                    short_ok = False

        # NEW: STOCH confirm — avg(%K,%D) thresholds
        st_conf = self._indicator_config.get("STOCH", {}).get("confirm", {})
        if st_conf.get("enabled", False):
            k = _to_float(row.get("STOCH_K")); d = _to_float(row.get("STOCH_D"))
            if np.isfinite(k) and np.isfinite(d):
                avg = 0.5*(k+d)
                if long_ok and not (avg < float(st_conf.get("long_max", 20.0))): long_ok = False
                if short_ok and not (avg > float(st_conf.get("short_min", 80.0))): short_ok = False

        # NEW: STOCH_RSI confirm — avg(%K,%D) thresholds
        sr_conf = self._indicator_config.get("STOCH_RSI", {}).get("confirm", {})
        if sr_conf.get("enabled", False):
            k = _to_float(row.get("STOCHRSI_K")); d = _to_float(row.get("STOCHRSI_D"))
            if np.isfinite(k) and np.isfinite(d):
                avg = 0.5*(k+d)
                if long_ok and not (avg < float(sr_conf.get("long_max", 20.0))): long_ok = False
                if short_ok and not (avg > float(sr_conf.get("short_min", 80.0))): short_ok = False

        # ATR confirm2: ATR >= min_pct_of_close * close (AND with others)
        try:
            atrc = self._indicator_config.get("ATR", {}).get("confirm2", {})
            if atrc and atrc.get("enabled", False):
                pct = float(atrc.get("min_pct_of_close", 0.0) or 0.0)
                atr = row.get("ATR"); close_px = row.get("close")
                arr = np.array([atr, close_px], dtype=float)
                if np.isfinite(arr).all() and close_px > 0:
                    ok = bool(float(atr) >= pct * float(close_px))
                    if long_ok:
                        long_ok = ok and long_ok if atrc.get("combine","and").lower()!="or" else (long_ok or ok)
                    if short_ok:
                        short_ok = ok and short_ok if atrc.get("combine","and").lower()!="or" else (short_ok or ok)
        except Exception:
            pass

        # ATR_PCT confirm: ATR >= min_pct_of_close * close (same as ATR confirm2, using percent convenience)
        try:
            apc = self._indicator_config.get("ATR_PCT", {}).get("confirm", {})
            if apc and apc.get("enabled", False):
                pct = float(apc.get("min_pct_of_close", 0.0) or 0.0)
                atr = row.get("ATR"); close_px = row.get("close")
                arr = np.array([atr, close_px], dtype=float)
                if np.isfinite(arr).all() and close_px > 0:
                    ok = bool(float(atr) >= pct * float(close_px))
                    if long_ok:
                        long_ok = ok and long_ok if apc.get("combine","and").lower()!="or" else (long_ok or ok)
                    if short_ok:
                        short_ok = ok and short_ok if apc.get("combine","and").lower()!="or" else (short_ok or ok)
        except Exception:
            pass


        # PCT_CHANGE confirm
        pc_conf = self._indicator_config.get("PCT_CHANGE", {}).get("confirm", {})
        if pc_conf.get("enabled", False):
            v = _to_float(row.get("PCT_CHANGE"))
            if np.isfinite(v):
                ok = abs(v) >= float(pc_conf.get("min_abs", 0.35))
                if long_ok:  long_ok  = ok if pc_conf.get("combine","and").lower()!="or" else (long_ok or ok)
                if short_ok: short_ok = ok if pc_conf.get("combine","and").lower()!="or" else (short_ok or ok)

        # FEAR_GREED confirm
        fg_conf = self._indicator_config.get("FEAR_GREED", {}).get("confirm", {})
        if fg_conf.get("enabled", False):
            v = _to_float(row.get("FEAR_GREED"))
            if np.isfinite(v):
                lc = (v < float(fg_conf.get("long_max", 40.0)))
                sc = (v > float(fg_conf.get("short_min", 60.0)))
                if bool(fg_conf.get("inverted", False)):
                    lc, sc = sc, lc
                if long_ok:  long_ok  = long_ok and lc
                if short_ok: short_ok = short_ok and sc

        # --- VOLUME confirm: VOL_AVG > min_mult * VOL_SMA ---
        v_conf = ((self._indicator_config.get("VOLUME", {}) or {}).get("confirm", {}) or {})
        if bool(v_conf.get("enabled", False)):
            vol_avg = _to_float(row.get("VOL_AVG"))
            vol_sma = _to_float(row.get("VOL_SMA"))
            ok = (np.isfinite(vol_avg) and np.isfinite(vol_sma) and vol_sma > 0.0 and
                  vol_avg > float(v_conf.get("min_mult", 4.0)) * vol_sma)
            if long_ok:
                long_ok = long_ok and ok
            if short_ok:
                short_ok = short_ok and ok

        return long_ok, short_ok

    def _confirm_ma_side(self, conf, ma_fast, ma_slow, price, side: str) -> bool:
        rules = conf.get("long_rules" if side == "long" else "short_rules", [])
        combine = str(conf.get("combine", "any")).lower()
        checks = []
        for r in rules:
            if r == "fast_gt_slow":
                checks.append(ma_fast > ma_slow)
            elif r == "fast_lt_slow":
                checks.append(ma_fast < ma_slow)
            elif r == "price_gt_fast":
                checks.append(price > ma_fast)
            elif r == "price_gt_slow":
                checks.append(price > ma_slow)
            elif r == "price_lt_fast":
                checks.append(price < ma_fast)
            elif r == "price_lt_slow":
                checks.append(price < ma_slow)
        if combine == "all":
            return all(checks) if checks else True
        return any(checks) if checks else True

    def _confirm_macd_side(self, conf, macd, signal, side: str, row=None) -> bool:
        combine = str(conf.get("combine", "any")).lower()
        rules = conf.get("long_rules" if side == "long" else "short_rules", [])
        checks = []
        if "signal_below_zero" in rules:
            checks.append(signal < 0 if side == "long" else signal > 0)
        if "signal_lt_macd" in rules:
            checks.append(signal < macd if side == "long" else signal > macd)
        if combine == "all":
            return all(checks) if checks else True
        return any(checks) if checks else True

    # =========================
    # EXITS (TP/SL/TS handled by engine/worker using get_risk_params/risk levels)
    # =========================

    def _primary_pct_change(self, row, symbol: str):
        cfg = self._indicator_config.get("PCT_CHANGE", {}).get("primary", {})
        if not cfg or not cfg.get("enabled", False):
            return (False, False)
        v = _to_float(row.get("PCT_CHANGE"))
        if not np.isfinite(v):
            return (False, False)
        up_th = float(cfg.get("short_above", 0.5))
        dn_th = float(cfg.get("long_below", -0.5))
        long_ok  = (v <= dn_th)
        short_ok = (v >= up_th)
        if bool(cfg.get("inverted", False)):
            long_ok, short_ok = short_ok, long_ok
        return (long_ok, short_ok)

    def _primary_fear_greed(self, row, symbol: str):
        cfg = self._indicator_config.get("FEAR_GREED", {}).get("primary", {})
        if not cfg or not cfg.get("enabled", False):
            return (False, False)
        v = _to_float(row.get("FEAR_GREED"))
        if not np.isfinite(v):
            return (False, False)
        long_ok  = (v < float(cfg.get("long_max", 80.0)))
        short_ok = (v > float(cfg.get("short_min", 20.0)))
        if bool(cfg.get("inverted", False)):
            long_ok, short_ok = short_ok, long_ok
        return (long_ok, short_ok)

# --- compatibility guard: ensure RSIStrategy has _apply_confirmations ---
try:
    _ = RSIStrategy._apply_confirmations
except Exception:
    def _apply_confirmations(self, row, long_ok: bool, short_ok: bool, symbol: str):
        # Fallback: pass-through if real confirmations missing
        try:
            rsi_conf = self._indicator_config["RSI"]["confirm"]
        except Exception:
            return long_ok, short_ok
        return long_ok, short_ok
    RSIStrategy._apply_confirmations = _apply_confirmations

