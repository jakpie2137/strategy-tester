from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
import requests

ALTME_URL = "https://api.alternative.me/fng/"

def _to_date_from_ts(ts: str | int | float) -> str:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return dt.date().isoformat()

def _validate_value(v) -> Optional[float]:
    try:
        f = float(v)
        if 0.0 <= f <= 100.0:
            return f
        return None
    except Exception:
        return None

def fetch_history(limit: Optional[int] = None, sleep_secs: float = 0.0) -> List[Dict]:
    """Fetch full (or limited) Fear&Greed history from alternative.me API.

    Returns list of dicts:
        {"day": "YYYY-MM-DD", "value": float 0-100, "source": "alternative.me"}
    """
    params = {"format": "json"}
    if limit is not None:
        params["limit"] = int(limit)

    rows: List[Dict] = []
    r = requests.get(ALTME_URL, params=params, timeout=10)
    r.raise_for_status()
    payload = r.json() or {}
    data = payload.get("data") or []

    for item in data:
        v = _validate_value(item.get("value"))
        if v is None:
            continue
        ts = item.get("timestamp") or item.get("time_until_update") or item.get("time")
        if ts is None:
            continue
        rows.append({"day": _to_date_from_ts(ts), "value": v, "source": "alternative.me"})
        if sleep_secs > 0:
            time.sleep(sleep_secs)

    rows.sort(key=lambda r: r["day"])  # ascending
    # dedupe: keep last per day
    seen = set(); dedup: List[Dict] = []
    for r in reversed(rows):
        if r["day"] in seen:
            continue
        seen.add(r["day"]); dedup.append(r)
    dedup.reverse()
    return dedup

def get_fng_series(limit: Optional[int] = None) -> List[Dict]:
    """Convenience wrapper used by fng_integration.sync_fear_greed."""
    return fetch_history(limit=limit)

def fetch_latest() -> Optional[Dict]:
    arr = fetch_history(limit=1)
    return arr[-1] if arr else None
