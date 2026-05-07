# livefetcher.py
import requests
from datetime import datetime, timedelta, timezone
import time
import logging
import pandas as pd  # <-- potrzebne do konwersji czasu
from config import DEFAULT_FETCH_INTERVAL, DEFAULT_CANDLE_INTERVAL
import threading


def _fmt_no_tz(ts_s: int) -> str:
    """Format dokładnie jak w historycznych: 'YYYY-MM-DD HH:MM:SS' (UTC, bez sufiksu)."""
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _to_epoch_ms(t):
    """Konwertuje datetime/str/epoch -> epoch ms (int)."""
    if t is None:
        return None
    try:
        if isinstance(t, (int, float)):
            return int(t if t > 1e12 else t * 1000)
        ts = pd.to_datetime(t, utc=True, errors="coerce")
        if ts is pd.NaT:
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
        bucket_end_s = bucket_start_s + self.interval_seconds - 1
        return {
            'bucket': bucket_start_s,
            'open': price,
            'high': price,
            'low': price,
            'close': price,
            'open_time': _fmt_no_tz(bucket_start_s),
            'close_time': _fmt_no_tz(bucket_end_s),
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
        self.session.headers.update({"User-Agent": "UtpLiveFetcher/1.0"})

    def _fetch_price_tick(self):
        url = f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={self.symbol}"
        try:
            resp = self.session.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            return float(data['price'])
        except Exception as e:
            logging.warning(f"[LiveFetcher:{self.symbol}] Błąd pobierania ticka: {e}")
            return None

    def _fetch_kline_volume_for_bucket(self, bucket_start_s: int, interval_seconds: int):
        """
        Dociąga z Binance klines wolumen QUOTE oraz taker_buy_volume_quote dla
        świecy o danym bucket_start (UTC).

        - interval_seconds -> konwertujemy na label Binance (np. '1m', '15m', '1h', '4h').
        - Porównujemy openTime (ms) klina z naszym bucket_start.
        """
        interval_label = self._binance_interval_label(interval_seconds)
        if interval_label is None:
            return None, None

        base_url = "https://fapi.binance.com/fapi/v1/klines"
        params = {
            "symbol": self.symbol,
            "interval": interval_label,
            "limit": 3,  # 3 sztuki wystarczą
        }
        try:
            resp = self.session.get(base_url, params=params, timeout=5)
            resp.raise_for_status()
            klines = resp.json()
        except Exception as e:
            logging.warning(f"[LiveFetcher:{self.symbol}] Błąd pobierania klines: {e}")
            return None, None

        target_start_ms = bucket_start_s * 1000
        for k in klines:
            # Binance futures:
            # [ openTime, open, high, low, close, volume, closeTime,
            #   quote_asset_volume, number_of_trades, taker_buy_base_volume,
            #   taker_buy_quote_volume, ignore ]
            open_time_ms = int(k[0])
            if open_time_ms == target_start_ms:
                quote_vol = float(k[7]) if k[7] is not None else None
                taker_buy_quote = float(k[11]) if len(k) > 11 and k[11] is not None else None
                return quote_vol, taker_buy_quote

        return None, None

    def _binance_interval_label(self, interval_seconds: int) -> str | None:
        """
        Mapuje interwał w sekundach na label Binance:
        60 -> '1m', 900 -> '15m', 3600 -> '1h', 14400 -> '4h', itp.
        """
        mins = interval_seconds // 60
        if mins < 1:
            return None
        if mins < 60:
            return f"{mins}m"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h"
        days = hours // 24
        return f"{days}d"

    def tick(self):
        """
        Główna funkcja wywoływana w pętli przez GUI:
        - pobiera 1 tick (price),
        - dokleja do listy ticków (pamięć RAM),
        - aktualizuje CandleBuilder,
        - jeśli któraś świeca się domknęła, zwraca ją.
        """
        price = self._fetch_price_tick()
        self.fetch_counter += 1

        if price is not None:
            ts = int(time.time())
            self.ticks.append({'ts': ts, 'price': price})
            # ograniczamy pamięć
            if len(self.ticks) > self.MAX_RAM_TICKS:
                self.ticks = self.ticks[-self.MAX_RAM_TICKS:]

            closed_candle = self.candle_builder.add_tick(price)
            if closed_candle:
                # świecę zamkniętą zapisujemy też w RAM
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

    # ---------- WSPARCIE GUI ----------

    def get_candles_live(self):
        candles = self.candles.copy()
        curr = self.candle_builder.get_current_candle()
        if curr:
            candles.append(curr)
        return candles[-self.MAX_RAM_CANDLES:]

    def get_inprogress_candle(self):
        return self.candle_builder.get_current_candle()
