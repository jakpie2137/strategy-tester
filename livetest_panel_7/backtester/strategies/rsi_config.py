# External config that deep-merges into rsi.py defaults.

# --- Global switches ---
PRIMARY_SIGNAL   = "MACD"      # "RSI" | "MA" | "MACD" | "BB" | "ATR" | "STOCH" | "STOCH_RSI" | .... (see dict below)
RISK_MODE        = "ATR"      # "FIXED" | "ATR"
STORE_ENTRY_ATR  = True       # store ATR at entry for logging/TS

TRAILING_STOP_ENABLED =  True
CLOSE_AFTER_X_CANDLES   = 70         # 0 = OFF; >0 -> force-close after X candles

# === Close_position execution style ===
CLOSE_EXECUTION_TYPE    = "on_candle_close"  # "on_candle_close # "on_crossover"
# CLOSE_EXECUTION_TYPE    = "on_crossover"

# === Slippage ===
CLOSE_EXECUTION_SLIPPAGE = 0.0003  # 0.0005 = 0.05 % = 5 bps

ENTRY_EXECUTION_SLIPPAGE = 0.0002  # 0.05% slippage na wejściu (0.0 = wyłączony)


# === SLU (StopLoss Updater) ===
SL_UPDATER = {
    "enabled": True,              # globalny włącznik całego SLU

    "static_jump_enabled": False,  # włącza/wyłącza tylko static jump

    # STATIC JUMP (BE / pół-BE itp.)
    "trigger_move_SL": 0.3,   # 30% drogi od entry do TP (0.10 - 1.00)
    "move_SL_to": 0.0,        # 0.0 = BE, 0.5 = w połowie drogi entry→TP
    "range_type": "entry_to_tp",

    # DYNAMIC SL – background mover
    "dynamic_SL": True,           # osobna flaga dla dynamicznego SLU
}

# Bucketization parameters
BUCKET = {
    "type": "rolling",    # rolling / fixed
    "window": 1,        # e.g., 5 -> 5-bar rolling OHLC
}


# --- Entry validation (percent thresholds for minimal distance from entry) ---
# Numbers are PERCENT values (e.g. 0.30 means 0.30%).
# A trade is opened only if BOTH are satisfied:
#   abs(TP - entry)/entry*100 >= min_tp_threshold
#   abs(SL - entry)/entry*100 >= min_sl_threshold
min_tp_threshold = 0.004  # default min 0.02 (2%)
min_sl_threshold = 0.002  # default min 0.01 (1%)

# --- Indicator overrides (structure mirrors rsi.py INDICATOR_CONFIG) ---
INDICATOR_OVERRIDES = {

    "RSI": {
        "enabled": True,
        "display": "sub1",
        "params": {"window": 14},  # (default = 14)
        "primary": {
            "enabled": False,
            "oversold": 30.0,
            "overbought": 70.0,
        },
        "confirm": {
            "enabled": True,
            "use_level_50": False,
            "long_max": 40.0,
            "short_min": 60.0,
        },
    },

    "MA": {
        "enabled": True,
        "display": "main",
        "params": {
            "type": "SMA",      # "SMA" | "EMA"
            "window_fast": 4,  # MA_WINDOW is further multiplied by BUCKET value - in FIXED bucket mode only!
            "window_slow": 36,  # MA_WINDOW is further multiplied by BUCKET value - in FIXED bucket mode only!
        },
        "primary": {
            "enabled": False,
            "type": "ma_cross_bullish",   # or: "ma_cross_bearish", "price_ma_cross_bullish"/"bearish"
            "price_ma": "fast",
            "confirmation_bars": 2,
        },
        "confirm": {
            "enabled": False,
            "long_rules":  ["fast_gt_slow"],  #, "price_gt_fast", "price_gt_slow"],
            "short_rules": ["fast_lt_slow"],  #, "price_lt_fast", "price_lt_slow"],
            "combine": "any",
        },
    },

    "MACD": {
        "enabled": True,
        "display": "sub2",
        "params": { "fast": 12, "slow": 26, "signal": 9 },
        "primary": {
            "enabled": True,
            "need_cross": True,
            "confirm_bars": 5,  # bars AFTER cross (histogram must keep new sign)
            "min_hist": 0.0,
            "min_delta": 0.0,
            "epsilon": 0.0,
        },
        "confirm": {
            "enabled": True,
            "long_rules":  ["signal_below_zero", "signal_lt_macd"],
            "short_rules": ["signal_above_zero", "signal_gt_macd"],
            "combine": "any",
            "rising_n": 5,
        },
    },

    "BB": {
        "enabled": True,
        "display": "main",
        "params": {"window": 20, "stdev": 3.0},  # default window = 20
        "primary": {
            "enabled": False,
            "mid_offset": 0.45,
            "use_cross": False,
            "inverted": True,  # False=mean reversion, True=breakout/continuation
        },
        "confirm": {
            "enabled": False,
            "mid_offset": 0.25,
        },
    },

    "ATR": {
        "enabled": True,
        "display": "sub3",
        "params": {"window": 14},  # default: 14
        "color": "#ffd166",
        "is_zero_always_visible": True,

        "primary": {
            "enabled": False,
            "avg_window": 3,
            "multiplier": 2.0,
            "direction": "rule",  # "rule" | "candle" | "close_change" | "both" | "long_only" | "short_only"
        },
        "confirm": {
            "enabled": False,  # opcjonalny gate vs MA_FAST
            "use_ma_fast": False,  # LONG: close<MA_FAST; SHORT: close>MA_FAST
            "strict": True  # równość (==) blokuje
        },
        "confirm2": {
            "enabled": False,
            "min_pct_of_close": 0.0015,  # 0.15% ceny
            "combine": "and"
        },
    },

    "ATR_PCT": {
        "enabled": True,
        "display": "sub3",
        "slot": 2,
        "color": "#C0C0C0",  # jasne srebro
        "is_zero_always_visible": True,
        "primary": {
            "enabled": False,
            "threshold": 0.08,  # procent, np. 0.8 == 0.8%
            "MA": "ma_fast",  # "ma_fast" | "ma_slow"
            "inverted": False  # False=mean reversion, True=breakout/continuation
        },
        "confirm": {
            "enabled": False,
            "min_pct_of_close": 0.0035,  # 0.0035 == 0.35% ceny
            "combine": "and"
        },
    },

    "PCT_CHANGE": {
        "enabled": True,
        "display": "sub3",
        "slot": 3,
        "primary": {"enabled": False, "long_below": -0.5, "short_above": 0.5, "inverted": False},
        "confirm": {"enabled": False, "min_abs": 0.65, "combine": "and"}
    },

    "STOCH": {
        "enabled": True,
        "display": "sub2",
        "slot": 2,
        "params": {"k_window": 14, "d_window": 3, "smooth_k": 3},
        "primary": { "enabled": False },
        "confirm": { "enabled": False, "long_max": 20.0, "short_min": 80.0 }
    },
    "STOCH_RSI": {
        "enabled": True,
        "display": "sub1",
        "slot": 2,
        "params": {"rsi_window": 14, "stoch_window": 14, "smooth_k": 3, "d_window": 3},
        "primary": { "enabled": False },
        "confirm": { "enabled": False, "long_max": 20.0, "short_min": 80.0 }
    },

    "FEAR_GREED": {
        "enabled": True,
        "display": "sub1",
        "slot": 3,
        "primary": { "enabled": False, "long_max": 40.0, "short_min": 60.0, "inverted": False },
        "confirm": { "enabled": False, "long_max": 30.0, "short_min": 70.0, "inverted": False }
    },

    "VOLUME": {
        "enabled": True,
        "display": "sub2",
        "slot": 3,
        "color": "#cccccc",
        "params": {"window": 2},  # dla VOL_AVG / BUY_VOL_AVG (krótki horyzont)
        "primary": {
            "enabled": False,
            "long_min_ratio": 0.75,  # próg dla LONG
            "short_max_ratio": 0.25,  # próg dla SHORT
            "inverted": False  # False => default (above) || True => LONG<0.25 ; SHORT>0.75
        },
        "confirm": {
            "enabled": False,
            "min_mult": 2.0,  # VOL_AVG > min_mult * VOL_SMA
            "window": 10  # okno VOL_SMA (długi horyzont)
        },
    },

}


# --- ATR risk tuning (used when RISK_MODE == "ATR") ---
RISK_PARAMS_ATR_OVERRIDES = {
    "tp_k_long":  5.0,
    "sl_k_long":  3.0,
    "tp_k_short": 5.0,
    "sl_k_short": 3.0,
    "ts_k_long":  0.8,   # dynamic trailing stop factor
    "ts_k_short": 0.8,

    # Optional percent clamps (relative to price) for ATR-based TP/SL and TS
    # values are FRACTIONS of price, e.g. 0.001 = 0.1%
    # "limits_min": {
    #     "tp_long": 0.0,
    #     "sl_long": 0.0,
    #     "tp_short": 0.0,
    #     "sl_short": 0.0,
    #     "ts_long": 0.0,
    #     "ts_short": 0.0
    # },
    # "limits_max": {
    #     "tp_long": 0.0,
    #     "sl_long": 0.0,
    #     "tp_short": 0.0,
    #     "sl_short": 0.0,
    #     "ts_long": 0.0,
    #     "ts_short": 0.0
    # },
    # values are FRACTIONS of price, e.g. 0.001 = 0.1%
    # "limits_min": {
    #     "tp_long": 0.015,
    #     "sl_long": 0.005,
    #     "tp_short": 0.015,
    #     "sl_short": 0.005,
    #     "ts_long": 0.0025,
    #     "ts_short": 0.0025
    # },
    "limits_max": {
        "tp_long": 0.05,
        "sl_long": 0.02,
        "tp_short": 0.05,
        "sl_short": 0.02,
        "ts_long": 0.01,
        "ts_short": 0.01
    },

    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    # TrailingStop clamp by % of TP / benchmark (to be checked):
    # enforce  +min_pct_change from TakeProfit OR Benchmark:
    # enforce  +min_pct*entry <= |benchmark - x*ATR - benchmark| <= +max_pct*entry
    "ts_atr_min_pct": 0.0015,   # +-0.2% * entry (lower clamp) (0.005 = 0.5%, 0.002 = 0.2%)
    "ts_atr_max_pct": 0.0050,    # +-1.0% * entry (upper clamp)
    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
}

# --- Fixed risk (used when RISK_MODE == "FIXED") ---
RISK_PARAMS_FIXED_OVERRIDES = {
    "tp_long": 1.005,
    "sl_long": 0.996,
    "tp_short": 0.995,
    "sl_short": 1.004,
    "trail_long": 0.998,
    "trail_short": 1.002,
}

# --- Optional strategy-level close signals (layer 2) ---
# BB/RSI/FEAR_GREED close use existing indicators defined for open-signals.
CLOSE_SIGNALS = {
    "enabled": True,        # master switch for all strategy close signals
    "required_all": False,   # False: any hit closes; True: require all active close signals

    "BB_close": {
        "enabled": False,
        "primary": {
            "enabled": False,
            # mid_offset in <-0.5; 0.5>, as FRACTION of band span:
            #   -0.5 -> lower band
            #    0.0 -> middle (MA)
            #   +0.5 -> upper band   (dla LONG / non-inverted)
            "mid_offset": 0.25,
            # inverted=True odwraca logikę LONG/SHORT przy interpretacji offsetu
            "inverted": False,
        },
    },

    "RSI_close": {
        "enabled": False,
        "primary": {
            "enabled": False,
            "close_long": 80.0,   # RSI >= close_long  -> close LONG
            "close_short": 20.0,  # RSI <= close_short -> close SHORT
        },
    },

    "FEAR_GREED_close": {
        "enabled": False,
        "primary": {
            "enabled": False,
            "close_long": 60.0,   # FNG >= -> close LONG
            "close_short": 40.0,  # FNG <= -> close SHORT
        },
    },
}

