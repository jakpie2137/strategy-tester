# livefetcher.py
import requests
from datetime import datetime, timedelta, timezone
import time
import logging
import pandas as pd  # <-- potrzebne do konwersji czasu
from config import DEFAULT_FETCH_INTERVAL, DEFAULT_CANDLE_INTERVAL
import threading


def _fmt_no_tz(ts_s: int):
    """Zwraca UTC-aware datetime dla podanych sekund epoki.

    Uwaga:
    - Funkcja zostawiona dla zgodności, ale zamiast stringa zwraca
      datetime z tz=UTC, żeby TIMESTAMPTZ w bazie nie przesuwał godzin.
    """
    return datetime.fromtimestamp(ts_s, tz=timezone.utc)


def _to_epoch_ms(t):
    """
    Helper do konwersji różnych reprezentacji czasu na epoch ms (int)
    - obsługuje datetime, np.datetime64, stringi, float/int (sekundy).
    """
    if t is None:
        return None
    try:
        if isinstance(t, (int, float)):
            return int(float(t) * 1000)
        if isinstance(t, datetime):
            if t.tzinfo is None:
                # traktujemy jako UTC, żeby nie zależeć od lokalnej strefy serwera
                t = t.replace(tzinfo=timezone.utc)
            return int(t.timestamp() * 1000)
        # pandas / numpy datetime64
        if hasattr(t, "to_datetime64"):
            t = t.to_pydatetime()
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return int(t.timestamp() * 1000)
        # string
        if isinstance(t, str):
            ts = pd.to_datetime(t, utc=True, errors='coerce')
            if ts is None or pd.isna(ts):
                return None
            return int(ts.value // 10**6)  # ns -> ms
    except Exception:
        return None


def get_bucket_bounds_utc(ts_s: int, interval_s: int) -> tuple[int, int]:
    """Zwraca (start, end) wiadra o długości interval_s sekund, liczone od 00:00 UTC.

    Algorytm:
    - zaokrąglenie w dół do najbliższej wielokrotności interval_s (floor),
    - end = start + interval_s - 1.

    Dzięki temu:
    - dla 15m (900s): 12:02, 12:14 -> bucket 12:00–12:14; 12:15 -> 12:15–12:29
    - dla 1h (3600s): 12:47 -> bucket 12:00–12:59
    - dla 4h (14400s): 13:10 -> bucket 12:00–15:59
    """
    interval_s = int(interval_s)
    if interval_s <= 0:
        raise ValueError(f"interval_s must be > 0, got {interval_s}")
    start = ts_s - (ts_s % interval_s)
    end = start + interval_s - 1
    return start, end


class CandleBuilder:
    """
    Buduje świeczki o stałym interwale (wiadra) z ticków:
    - Wyrównuje do początku wiadra (floor).
    - Domyka WSZYSTKIE zaległe wiadra (jeśli przeskoczyliśmy o >1 interwał).
    - Braki wypełnia świecami O=H=L=C=ostatni close (wolumen=0; i tak dociągamy z klines).
    - Zwraca po jednej zamkniętej świecy na wywołanie (kolejka).
    """
    def __init__(self, interval_seconds=60):
        self.interval_seconds = int(interval_seconds)
        self.current_candle = None
        self.lock = threading.Lock()
        self._closed_queue = []  # kolejka świec do oddania przy kolejnych wywołaniach
        self._last_close = None  # ostatni znany close (do wypełniania dziur)

    def _bucket_start_end(self, ts_sec: int):
        """Zwraca (start, end) wiadra dla danego timestampu, korzystając z helpera
        get_bucket_bounds_utc, tak aby wiadra były wyrównane do 00:00 UTC
        i miały długość self.interval_seconds.
        """
        start, end = get_bucket_bounds_utc(ts_sec, self.interval_seconds)
        return start, end

    def _make_candle_from_price(self, bucket_start_s: int, price: float):
        """Zbuduj świecę startującą w bucket_start_s (sekundy epoki, UTC).

        - open_time: dokładny początek wiadra (UTC, ms = 000)
        - close_time: koniec wiadra z lekkim przesunięciem na 59.998s,
          żeby zgadzało się z konwencją danych historycznych.
        """
        bucket_end_s = bucket_start_s + self.interval_seconds - 1

        open_dt = datetime.fromtimestamp(bucket_start_s, tz=timezone.utc)
        close_dt = datetime.fromtimestamp(bucket_end_s, tz=timezone.utc).replace(microsecond=998_000)

        return {
            'bucket': bucket_start_s,
            'open': price,
            'high': price,
            'low': price,
            'close': price,
            'open_time': open_dt,
            'close_time': close_dt,
        }

    def _finalize_current(self):
        """Zamknij bieżącą świecę i dorzuć do kolejki."""
        if not self.current_candle:
            return
        # update last_close
        self._last_close = float(self.current_candle['close'])
        # do kolejki zamkniętych
        self._closed_queue.append({
            'bucket': self.current_candle['bucket'],
            'open': float(self.current_candle['open']),
            'high': float(self.current_candle['high']),
            'low': float(self.current_candle['low']),
            'close': float(self.current_candle['close']),
            'open_time': self.current_candle['open_time'],
            'close_time': self.current_candle['close_time'],
        })
        self.current_candle = None

    def add_tick(self, price):
        with self.lock:
            now_s = int(time.time())
            b_start, b_end = self._bucket_start_end(now_s)

            # Jeśli mamy zamknięte świece w kolejce (z wcześniejszych luk), oddaj pierwszą
            if self._closed_queue:
                closed = self._closed_queue.pop(0)
                # nadal aktualizujemy świecę live (poniżej)
                # ale już możemy zwrócić zamkniętą
                # Uwaga: nie "return" teraz – najpierw zaktualizuj live
                queued_closed = closed
            else:
                queued_closed = None

            # Brak świecy -> otwórz bieżącą
            if self.current_candle is None:
                base_price = float(price if price is not None else (self._last_close or 0.0))
                self.current_candle = self._make_candle_from_price(b_start, base_price)

            # Jeśli przeskoczyliśmy do NOWEGO wiadra
            elif b_start > self.current_candle['bucket']:
                # 1) Domknij bieżącą
                self._finalize_current()

                # 2) Wypełnij ewentualne LUKI pustymi świecami
                prev_bucket_start = self._closed_queue[-1]['bucket'] if self._closed_queue else None
                if prev_bucket_start is None:
                    prev_bucket_start = b_start - self.interval_seconds  # ostatnio domknięte wiadro to poprzednia świeca
                gap_start = prev_bucket_start + self.interval_seconds

                while gap_start < b_start:
                    # pusta świeca z ostatnim close
                    p = float(self._last_close if self._last_close is not None else price)
                    gap_candle = self._make_candle_from_price(gap_start, p)
                    self._closed_queue.append({
                        'bucket': gap_candle['bucket'],
                        'open': p, 'high': p, 'low': p, 'close': p,
                        'open_time': gap_candle['open_time'],
                        'close_time': gap_candle['close_time'],
                    })
                    gap_start += self.interval_seconds

                # 3) Otwórz nową bieżącą świecę dla *aktualnego* wiadra
                base_price = float(price if price is not None else (self._last_close or 0.0))
                self.current_candle = self._make_candle_from_price(b_start, base_price)

            # Aktualizuj bieżącą świecę nowym tickiem
            p = float(price)
            c = self.current_candle
            c['high'] = max(c['high'], p)
            c['low'] = min(c['low'], p)
            c['close'] = p

            # Jeśli mieliśmy kolejkę zamkniętych świec (z luk), to zwróć najstarszą
            if queued_closed is not None:
                return queued_closed

            # W przeciwnym razie nic się nie domknęło teraz
            return None

    def get_current_candle(self):
        with self.lock:
            if self.current_candle is None:
                return None
            # zwracamy kopię, żeby GUI nie mogło popsuć stanu
            return dict(self.current_candle)


class LiveFetcher:
    """
    Pobiera ticki z Binance Futures i buduje z nich świece w zadanym interwale.
    - freq_seconds: co ile sekund robimy request do /fapi/v1/ticker/price
    - candle_interval: interwał świecy w MINUTACH (np. 1, 15, 60, 240)
    """
    MAX_RAM_TICKS = 300
    MAX_RAM_CANDLES = 300

    def __init__(self, symbol="BTCUSDT", freq_seconds=None, candle_interval=None):
        self.symbol = symbol
        self.freq_seconds = freq_seconds if freq_seconds is not None else DEFAULT_FETCH_INTERVAL
        # w projekcie candle_interval jest w MINUTACH
        self.candle_interval = candle_interval if candle_interval is not None else DEFAULT_CANDLE_INTERVAL
        self.candle_builder = CandleBuilder(interval_seconds=int(self.candle_interval) * 60)
        self.candles = []
        self.ticks = []
        self.fetch_counter = 0
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'LivetestPanel/1.0'})

    # ---------- API BINANCE ----------

    def _fetch_price(self) -> float:
        """Pobierz ostatnią cenę z Binance Futures."""
        url = f"https://fapi.binance.com/fapi/v1/ticker/price"
        params = {"symbol": self.symbol}
        r = self.session.get(url, params=params, timeout=5)
        r.raise_for_status()
        data = r.json()
        return float(data["price"])

    def _fetch_kline_volume_for_bucket(self, bucket_start_s: int, interval_s: int):
        """
        Dociąga z Binance wolumen dla świecy, której początek to bucket_start_s (UTC).
        Zwraca (quote_volume, taker_buy_quote_volume).
        """
        start_dt = datetime.fromtimestamp(bucket_start_s, tz=timezone.utc)
        end_dt = start_dt + timedelta(seconds=interval_s)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {
            "symbol": self.symbol,
            "interval": self._interval_to_binance_str(interval_s),
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 1,
        }
        r = self.session.get(url, params=params, timeout=5)
        r.raise_for_status()
        data = r.json()
        if not data:
            return 0.0, 0.0

        k = data[0]
        quote_volume = float(k[7])
        taker_buy_quote_volume = float(k[10])
        return quote_volume, taker_buy_quote_volume

    def _interval_to_binance_str(self, interval_s: int) -> str:
        """Konwersja sekund na string interwału Binance (np. 60 -> '1m')."""
        if interval_s % 60 == 0 and interval_s < 3600:
            return f"{interval_s // 60}m"
        if interval_s % 3600 == 0 and interval_s < 86400:
            return f"{interval_s // 3600}h"
        if interval_s % 86400 == 0:
            return f"{interval_s // 86400}d"
        # fallback
        return "1m"

    # ---------- PĘTLA TICKÓW / ŚWIEC ----------

    def tick(self):
        """
        Główna funkcja: pobiera cenę, aktualizuje CandleBuilder
        i ewentualnie zwraca zamkniętą świecę (dict) lub None.
        """
        try:
            price = self._fetch_price()
        except Exception as e:
            logging.warning("LiveFetcher[%s] _fetch_price error: %s", self.symbol, e)
            return None, None

        closed_candle = self.candle_builder.add_tick(price)

        # przechowujemy w RAM max N ostatnich ticków (debug / GUI)
        self.ticks.append({
            "ts": time.time(),
            "price": price,
        })
        if len(self.ticks) > self.MAX_RAM_TICKS:
            self.ticks = self.ticks[-self.MAX_RAM_TICKS:]

        if closed_candle is not None:
            # dopisz świecę do RAM (opcjonalnie dla debug/GUI)
            self.candles.append(closed_candle)
            if len(self.candles) > self.MAX_RAM_CANDLES:
                self.candles = self.candles[-self.MAX_RAM_CANDLES:]

            # Dociągnij wolumeny z klines
            bucket_start_s = closed_candle['bucket']
            q_vol, taker_q = self._fetch_kline_volume_for_bucket(
                bucket_start_s, self.candle_builder.interval_seconds
            )
            closed_candle['volume_quote'] = q_vol
            closed_candle['taker_buy_volume_quote'] = taker_q

            return price, closed_candle

        return price, None

    # ---------- WSPARCIE DLA GUI / WORKERÓW ----------

    def get_inprogress_candle(self):
        return self.candle_builder.get_current_candle()
