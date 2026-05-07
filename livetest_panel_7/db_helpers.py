# -*- coding: utf-8 -*-
"""
db_helpers.py
Utilities for datetime coercion and candle interval inference.
- NO timezone normalization is performed here.
- Functions are defensive: they accept mixed inputs and fail "softly".
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd


def coerce_ts(val: Any) -> Optional[pd.Timestamp]:
    """
    Best-effort coercion of a single timestamp-like value to pandas.Timestamp.
    - Accepts datetime-like objects, ISO strings (with or without 'Z'), ints/floats (epoch s/ms/us/ns).
    - DOES NOT force UTC nor change timezone info.
    - Returns None if parsing fails.
    """
    if val is None:
        return None

    # Fast-path: already a pandas Timestamp or numpy datetime64
    if isinstance(val, pd.Timestamp):
        return val

    # Try numpy datetime64
    try:
        if str(type(val)).endswith("datetime64'") or "datetime64" in str(type(val)):
            return pd.to_datetime(val, errors="coerce")
    except Exception:
        pass

    # Numbers: try epoch seconds -> ms -> us -> ns
    if isinstance(val, (int, float, np.integer, np.floating)):
        for unit in ("s", "ms", "us", "ns"):
            ts = pd.to_datetime(val, unit=unit, errors="coerce")
            if pd.notna(ts):
                return ts
        return None

    # Strings: allow 'Z' and plain ISO, do not force tz
    try:
        s = str(val).strip()
        if not s:
            return None
        s = s.replace("Z", "+00:00") if "Z" in s and "+" not in s else s
        ts = pd.to_datetime(s, errors="coerce")
        if pd.notna(ts):
            return ts
    except Exception:
        return None

    # Fallback: generic coercion
    try:
        ts = pd.to_datetime(val, errors="coerce")
        return ts if pd.notna(ts) else None
    except Exception:
        return None


def coerce_dt_series(values: Iterable[Any]) -> pd.Series:
    """
    Coerce a sequence/Series to a pandas.Series[datetime64[ns, *]] WITHOUT UTC normalization.
    Unparseable entries become NaT.
    """
    try:
        return pd.to_datetime(pd.Series(list(values)), errors="coerce")
    except Exception:
        # Extremely defensive fallback
        out = []
        for v in values:
            out.append(coerce_ts(v))
        return pd.Series(out, dtype="datetime64[ns]")


def infer_candle_interval_seconds(data, default=None):
    """
    Wyciągnij dominujący krok (sekundy) ze zbioru czasów; nie zmieniamy stref.
    """
    import pandas as pd, numpy as np

    def _series(x):
        if x is None:
            return pd.Series([], dtype='datetime64[ns]')
        if hasattr(x, "columns"):
            df = x
            if 'close_time' in df.columns:
                s = pd.to_datetime(df['close_time'], errors='coerce')
            elif 'open_time' in df.columns:
                s = pd.to_datetime(df['open_time'], errors='coerce')
            else:
                s = pd.to_datetime(df.index, errors='coerce')
            return s.dropna().sort_values()
        if hasattr(x, "dtype") or isinstance(x, (list, tuple, set)):
            return pd.to_datetime(pd.Series(list(x)), errors='coerce').dropna().sort_values()
        try:
            return pd.to_datetime(pd.Series([x]), errors='coerce').dropna().sort_values()
        except Exception:
            return pd.Series([], dtype='datetime64[ns]')

    s = _series(data)
    if s.empty or len(s) < 3:
        if isinstance(default, (list, tuple)):
            return int(default[0]) if default else None
        try:
            return int(default) if default is not None else None
        except Exception:
            return None

    diffs = s.diff().dt.total_seconds().dropna()
    diffs = diffs[diffs > 0]
    if diffs.empty:
        return int(default) if isinstance(default, (int, float, str)) else None

    d = diffs.round().astype('int64')
    return int(d.value_counts().idxmax())



_INTERVAL_MAP = {
    60: "1m", 120: "2m", 180: "3m", 240: "4m", 300: "5m",
    600: "10m", 900: "15m", 1200: "20m", 1800: "30m",
    3600: "1h", 7200: "2h", 14400: "4h", 21600: "6h",
    28800: "8h", 43200: "12h", 86400: "1d", 172800: "2d",
    604800: "1w",
}


def seconds_to_interval_label(seconds: Optional[int]) -> Optional[str]:
    """
    Map seconds to a human label, if known.
    Otherwise return e.g. '123s' for 123 seconds, or None for invalid input.
    """
    if seconds is None:
        return None
    try:
        sec = int(seconds)
    except Exception:
        return None
    return _INTERVAL_MAP.get(sec, f"{sec}s")
