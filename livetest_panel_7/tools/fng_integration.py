from __future__ import annotations
import logging
from typing import List, Dict, Optional

from data.db_pg import Database  # updated import

try:
    from tools.altme_fng import get_fng_series
except Exception as e:
    logging.warning("tools.altme_fng not importable: %r; using fallback http client", e)
    import json, urllib.request, datetime as dt

    def get_fng_series() -> List[Dict]:
        ALTME_URL = "https://api.alternative.me/fng/?limit=0&format=json"
        with urllib.request.urlopen(ALTME_URL, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        out: List[Dict] = []
        for item in data.get("data", []):
            try:
                v = float(item.get("value"))
            except Exception:
                continue
            if not (0.0 <= v <= 100.0):
                continue
            ts = item.get("timestamp")
            if ts is None:
                continue
            day = dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc).date().isoformat()
            out.append({"day": day, "value": v, "source": "alternative.me"})
        out.sort(key=lambda r: r["day"])
        # dedupe per day, keep last
        seen = set(); dedup: List[Dict] = []
        for r in reversed(out):
            if r["day"] in seen:
                continue
            seen.add(r["day"]); dedup.append(r)
        dedup.reverse()
        return dedup

log = logging.getLogger("fng_integration")

def sync_fear_greed(db: Optional[Database] = None, rows: Optional[List[Dict]] = None) -> int:
    """Fetch (if rows is None) and upsert Fear&Greed index into DB.

    Deleguje całą logikę integracji do Database.replace_fear_greed(data),
    która w Twoim projekcie odpowiada za:
      - zapis / aktualizację szeregów F&G,
      - ewentualne doklejanie FEAR_GREED do tabeli wskaźników.
    Zwraca liczbę wierszy wejściowych (po deduplikacji).
    """
    db = db or Database()
    data = rows if rows is not None else get_fng_series()
    db.replace_fear_greed(data)
    return len(data)

def load_fear_greed(db: Optional[Database] = None):
    """Ładuje cały szereg F&G przy użyciu metody DB get_fear_greed_df()."""
    db = db or Database()
    return db.get_fear_greed_df()
