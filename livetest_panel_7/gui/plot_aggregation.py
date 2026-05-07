# gui/plot_aggregation.py
from typing import Optional
import logging

import numpy as np
import pandas as pd


def build_plot_df(
    df_raw: pd.DataFrame,
    agg_n: int,
    max_plot: int,
    *,
    symbol: Optional[str] = None,
    log_prefix: str = "[plot_aggregation]",
) -> pd.DataFrame:
    """
    Wspólny helper do budowania DataFrame pod wykres:
    - opcjonalna agregacja świec i wskaźników po agg_n świec bazowych,
    - docięcie do max_plot po agregacji,
    - timestamp z close_time (UNIX sekundy), jeśli dostępny.

    Zasady agregacji:
      - open  = open pierwszej świecy w buckecie
      - close = close ostatniej świecy w buckecie
      - high  = max(high)
      - low   = min(low)
      - volume* = suma
      - wskaźniki (numeryczne, nie-OHLC/czas/volume/risk) = średnia
      - TP / SL / TS / TS_BENCHMARK = last (poziome segmenty zamiast „pochyłych”)
    """

    if df_raw is None:
        return df_raw
    try:
        if len(df_raw) == 0:
            return df_raw
    except Exception:
        pass

    # --- normalizacja parametrów ---
    try:
        agg_n = int(agg_n) if agg_n else 1
    except Exception:
        agg_n = 1
    if agg_n <= 0:
        agg_n = 1

    try:
        max_plot_int = int(max_plot) if max_plot else 0
    except Exception:
        max_plot_int = 0

    # --- ścieżka bez agregacji ---
    if agg_n == 1:
        try:
            df = df_raw.copy()
        except Exception:
            df = pd.DataFrame(df_raw)

        try:
            if "close_time" in df.columns:
                df["close_time"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
                df = df[df["close_time"].notna()].sort_values("close_time").reset_index(drop=True)
            elif isinstance(df.index, pd.DatetimeIndex):
                df = df.sort_index().reset_index(drop=False).rename(columns={"index": "close_time"})
            elif "timestamp" in df.columns:
                df = df.sort_values("timestamp").reset_index(drop=True)
            else:
                df = df.reset_index(drop=True)
        except Exception:
            pass

        if max_plot_int:
            try:
                if len(df) > max_plot_int:
                    df = df.tail(max_plot_int)
            except Exception:
                pass

        try:
            if "close_time" in df.columns:
                df["timestamp"] = (df["close_time"].astype("int64") // 10**9).astype("int64")
        except Exception:
            pass

        return df.reset_index(drop=True)

    # --- ścieżka z agregacją ---
    try:
        df = df_raw.copy()
    except Exception:
        df = pd.DataFrame(df_raw)

    # normalizacja / sortowanie
    try:
        if "close_time" in df.columns:
            df["close_time"] = pd.to_datetime(df["close_time"], utc=True, errors="coerce")
            df = df[df["close_time"].notna()].sort_values("close_time").reset_index(drop=True)
        elif isinstance(df.index, pd.DatetimeIndex):
            df = df.sort_index().reset_index(drop=False).rename(columns={"index": "close_time"})
        elif "timestamp" in df.columns:
            df = df.sort_values("timestamp").reset_index(drop=True)
        else:
            df = df.reset_index(drop=True)
    except Exception as e:
        logging.debug("%s normalize failed for %s: %s", log_prefix, symbol or "?", e)

    try:
        n = len(df)
    except Exception:
        n = 0
    if n == 0:
        return df

    # bucket index
    try:
        bucket_idx = (np.arange(n, dtype=np.int64) // int(agg_n)).astype(np.int64)
    except Exception:
        bucket_idx = np.arange(n, dtype=np.int64)
    df["__bucket"] = bucket_idx

    # kolumny specjalne
    ohlc_cols = ["open", "high", "low", "close"]
    time_cols = ["open_time", "close_time"]
    volume_cols = ["volume", "volume_quote", "taker_buy_volume", "taker_buy_volume_quote"]
    risk_cols = {"TP", "SL", "TS", "TS_BENCHMARK"}

    # wskaźniki = numeryczne, które nie są OHLC/czas/volume/aux
    numeric_cols = []
    for col in df.columns:
        if col in ohlc_cols or col in time_cols or col in volume_cols:
            continue
        if col in ("symbol", "__bucket", "timestamp"):
            continue
        try:
            if np.issubdtype(df[col].dtype, np.number):
                numeric_cols.append(col)
        except Exception:
            continue

    agg_dict = {
        "open_time": "first" if "open_time" in df.columns else "first",
        "close_time": "last" if "close_time" in df.columns else "last",
        "open": "first" if "open" in df.columns else "first",
        "high": "max" if "high" in df.columns else "max",
        "low": "min" if "low" in df.columns else "min",
        "close": "last" if "close" in df.columns else "last",
    }

    for col in volume_cols:
        if col in df.columns:
            agg_dict[col] = "sum"

    for col in numeric_cols:
        if col in agg_dict:
            continue
        if col in risk_cols:
            agg_dict[col] = "last"
        else:
            agg_dict[col] = "mean"

    try:
        grouped = df.groupby("__bucket", sort=True)
        out = grouped.agg(agg_dict).reset_index(drop=True)
    except Exception as e:
        logging.exception("%s aggregation failed for %s: %s", log_prefix, symbol or "?", e)
        try:
            base = pd.DataFrame(df_raw)
            if max_plot_int and len(base) > max_plot_int:
                base = base.tail(max_plot_int)
            return base.reset_index(drop=True)
        except Exception:
            return pd.DataFrame(df_raw)

    # timestamp = close_time
    try:
        if "close_time" in out.columns:
            out["timestamp"] = (out["close_time"].astype("int64") // 10**9).astype("int64")
        elif "timestamp" in out.columns:
            out["timestamp"] = out["timestamp"].astype("int64")
    except Exception:
        pass

    # wygładzenie wskaźników (RSI/ATR itd.), żeby nie było dziur
    try:
        indicator_cols = []
        for col in out.columns:
            if col in ohlc_cols or col in time_cols or col in volume_cols or col in risk_cols:
                continue
            if col in ("symbol", "__bucket", "timestamp"):
                continue
            try:
                if np.issubdtype(out[col].dtype, np.number):
                    indicator_cols.append(col)
            except Exception:
                continue

        if indicator_cols:
            out[indicator_cols] = out[indicator_cols].ffill()
    except Exception:
        pass

    # docięcie po agregacji
    if max_plot_int:
        try:
            if len(out) > max_plot_int:
                out = out.tail(max_plot_int)
        except Exception:
            pass

    return out.reset_index(drop=True)
