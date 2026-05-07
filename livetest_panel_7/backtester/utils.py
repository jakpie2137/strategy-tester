# backtester/utils.py

def smart_price_format(price):
    """Format price to reasonable precision, depending on its value."""
    if price is None:
        return "-"
    try:
        price = float(price)
    except Exception:
        return str(price)
    if price > 1000:
        return f"{price:.2f}"
    elif price > 1:
        return f"{price:.4f}"
    elif price > 0.01:
        return f"{price:.6f}"
    else:
        return f"{price:.8f}"

def format_amount(amount):
    """Format amount for display."""
    if amount is None:
        return "-"
    try:
        amount = float(amount)
    except Exception:
        return str(amount)
    if amount > 100:
        return f"{amount:.2f}"
    elif amount > 1:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

import time
import logging

logger = logging.getLogger(__name__)


class PerfTimer:
    """Kontekstowy pomiar czasu sekcji kodu.

    Przykład:
        from config import PERF_DEBUG
        from backtester.utils import PerfTimer

        with PerfTimer("opis", PERF_DEBUG):
            ...
    """
    def __init__(self, name: str, enabled: bool = True):
        self.name = name
        self.enabled = enabled
        self._start = None

    def __enter__(self):
        if self.enabled:
            self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.enabled and self._start is not None:
            elapsed = (time.perf_counter() - self._start) * 1000.0
            # WARNING level so that it is always visible in standard logs
            logger.warning("[PERF] %s took %.2f ms", self.name, elapsed)


def perf_log(name: str, start_ts: float, enabled: bool = True) -> None:
    """Jednorazowy log czasu (ms) dla sekcji mierzonej ręcznie.

    Przykład:
        t0 = time.perf_counter()
        ... kod ...
        perf_log("opis", t0, PERF_DEBUG)
    """
    if not enabled:
        return
    elapsed = (time.perf_counter() - start_ts) * 1000.0
    logger.warning("[PERF] %s took %.2f ms", name, elapsed)

