# LIVETEST_PANEL – Dokumentacja strategii (RSI / multi-indicator)

_Scope: rsi.py, rsi_config.py, test_worker.py, engine.py, context.py, db_pg.py_

## Spis treści

- [1. Overview: jak żyje strategia w systemie](#1.-overview:-jak-zyje-strategia-w-systemie)
  - [1.1. Rodzaje testów](#1.1.-rodzaje-testow)
- [2. Flow testu: krok po kroku](#2.-flow-testu:-krok-po-kroku)
  - [2.1. Moduły w grze](#2.1.-moduly-w-grze)
  - [2.2. Szczegółowy flow (backtest z GUI)](#2.2.-szczegolowy-flow-(backtest-z-gui))
- [3. `rsi_config.py` – konfiguracja strategii](#3.-`rsi_config.py`-–-konfiguracja-strategii)
  - [3.1. Globalne przełączniki](#3.1.-globalne-przelaczniki)
  - [3.2. SL_UPDATER (StopLoss Updater)](#3.2.-sl_updater-(stoploss-updater))
  - [3.3. Bucketizacja wskaźników](#3.3.-bucketizacja-wskaznikow)
  - [3.4. Minimalne progi TP/SL (entry validation)](#3.4.-minimalne-progi-tp/sl-(entry-validation))
  - [3.5. RISK_PARAMS_ATR_OVERRIDES](#3.5.-risk_params_atr_overrides)
  - [3.6. CLOSE_SIGNALS – dodatkowe sygnały zamknięcia](#3.6.-close_signals-–-dodatkowe-sygnaly-zamkniecia)
  - [3.7. INDICATOR CONFIG – rodziny wskaźników](#3.7.-indicator-config-–-rodziny-wskaznikow)
- [4. `rsi.py` – mechanika strategii](#4.-`rsi.py`-–-mechanika-strategii)
  - [4.1. Obliczanie wskaźników (`compute_indicators`)](#4.1.-obliczanie-wskaznikow-(`compute_indicators`))
  - [4.2. Sygnały otwarcia (`open_position_signal`)](#4.2.-sygnaly-otwarcia-(`open_position_signal`))
  - [4.3. Sygnały zamknięcia (`close_position_signal`)](#4.3.-sygnaly-zamkniecia-(`close_position_signal`))
  - [4.4. Risk params (`get_risk_params`)](#4.4.-risk-params-(`get_risk_params`))
- [5. `test_worker.py` – jak z configu robi się trade (sygnał)](#5.-`test_worker.py`-–-jak-z-configu-robi-sie-trade)
  - [5.1. Inicjalizacja](#5.1.-inicjalizacja)
  - [5.2. Główna pętla po świecach](#5.2.-glowna-petla-po-swiecach)
  - [5.3. Kaskada close-candidates](#5.3.-kaskada-close-candidates)
  - [5.4. Wejście pozycji – TP/SL/TS/SLU](#5.4.-wejscie-pozycji-–-tp/sl/ts/slu)
- [6. `engine.py`, `context.py`, `db_pg.py` – rola we flow strategii](#6.-`engine.py`,-`context.py`,-`db_pg.py`-–-rola-we-flow-strategii)
  - [6.1. `MultiSymbolEngine` (`engine.py`)](#6.1.-`multisymbolengine`-(`engine.py`))
  - [6.2. `SymbolContext` (`context.py`)](#6.2.-`symbolcontext`-(`context.py`))
  - [6.3. `db_pg.py` – Postgres backend](#6.3.-`db_pg.py`-–-postgres-backend)
- [7. Podsumowanie](#7.-podsumowanie)


Strategia `RSIStrategy` w LIVETEST_PANELU to tak naprawdę **multi-indicator engine** – nazwa historyczna, ale logika obsługuje wiele typów sygnałów (RSI, MA, MACD, BB, ATR, STOCH, STOCH_RSI, PCT_CHANGE, FEAR_GREED, VOLUME).

Ta dokumentacja opisuje:

- jak wygląda **flow testu** (od świecy do trade’a),
- jak komunikują się: `RSIStrategy` ↔ `test_worker` ↔ `MultiSymbolEngine` ↔ `SymbolContext` ↔ DB,
- jakie są **ważniejsze pola w `rsi_config.py`** i co robią w praktyce,
- jak działa logika **open/close**, **risk-limity** (TP/SL/TS, ATR-based) i dodatkowe **close_signals** (BB/RSI/FEAR_GREED).

## 1. Overview: jak żyje strategia w systemie

Na wysokim poziomie przepływ wygląda tak:

```text
[DANE (świece)]  ->  [RSIStrategy.compute_indicators]  ->  [ramka z ceną + wskaźnikami]
                                                |
                                          (per-candle loop)
                                                v
                         [RSIStrategy.open_position_signal / close_position_signal]
                                                |
                                                v
                       [test_worker: risk-engine, TP/SL/TS/SLU, slippage]
                                                |
                                                v
     [MultiSymbolEngine + SymbolContext]  <->  [DB: trades / indicators]  <->  [GUI / raporty]
```

### 1.1. Rodzaje testów

System wspiera dwa główne tryby wykorzystywania strategii:

1. **Backtest headless** (np. `headless_tester.py`):
   - Bez GUI.
   - Bez zapisu wskaźników/trejdów do DB (wyniki zwykle w pamięci / do pliku).
   - Maksymalna prędkość, do masówek i grid-searchy.
2. **Backtest z GUI (LIVETEST_PANEL):**
   - `test_worker.py` + `engine.MultiSymbolEngine` + `RSIStrategy`.
   - Zapis wskaźników do `indicators_historical` (DB) oraz trejdów/statystyk.
   - GUI pokazuje wykresy, trejdy, equity, global stats – do analizy jakościowej.

Logika strategii (`rsi.py` / `rsi_config.py`) jest **ta sama**. Różni się tylko „otoczką” (czy zapisujemy do DB, czy tylko w RAM/plik).

## 2. Flow testu: krok po kroku

### 2.1. Moduły w grze

- **`rsi_config.py`** – konfig strategii (parametry wskaźników, progi, tryby, risk-config).
- **`rsi.py` (`RSIStrategy`)** – implementacja strategii: obliczanie wskaźników, sygnały open/close, risk params.
- **`test_worker.py`** – worker, który dociąga świece, woła strategię, odpala risk-engine, generuje trejdy.
- **`engine.py` (`MultiSymbolEngine`)** – spina wiele symboli, zarządza `SymbolContext` i zapisami do DB.
- **`context.py` (`SymbolContext`)** – stan per symbol: aktualna pozycja, lista trejdów, buffery itd.
- **`db_pg.py`** – adapter do Postgresa (świece, wskaźniki, trejdy).

### 2.2. Szczegółowy flow (backtest z GUI)

```text
[1] test_worker.start()
    |
    |  ładuje strategy_class (RSIStrategy) + rsi_config
    v
[2] MultiSymbolEngine(...) dla listy symboli
    |  - tworzy SymbolContext per symbol
    |  - rejestruje DB connectory
    v
[3] Dla każdego symbolu:
    |  - pobiera świece z DB (db_pg.get_candles)
    |  - normalizuje daty (test_worker._normalize_candles_df)
    |  - woła strategy.compute_indicators(...) / compute_indicators_segmented(...)
    |  -> dostaje DataFrame z ceną + kolumnami wskaźników
    v
[4] Pętla po każdej świecy (w test_worker):
    |  - row_dict = row.to_dict()
    |  - strategy.open_position_signal(row, current_position)
    |  - strategy.close_position_signal(row, current_position)
    |  - risk-engine (TP/SL/TS/SLU, time-based close, execution mode, slippage)
    |  - MultiSymbolEngine.open_position / close_position aktualizują SymbolContext
    |  - ticki wokół trade’a + zapisy do DB (jeśli GUI/test z DB)
    v
[5] Po zakończeniu symbolu/testu:
    |  - zapis trejdów/statystyk
    |  - GUI odbiera dane i rysuje wykresy + tabele
```

W trybie headless (masowe testy) krok [4] jest podobny, tylko zamiast DB/GUI wyniki lądują w strukturach w pamięci / plikach, a DB może w ogóle nie wchodzić w grę.

## 3. `rsi_config.py` – konfiguracja strategii

`rsi_config.py` jest zewnętrznym plikiem, który **nadpisuje** domyślne wartości z `rsi.py` (deep-merge). Dzięki temu nie trzeba grzebać w kodzie strategii – można tworzyć różne presety po prostu przez modyfikację configu.

### 3.1. Globalne przełączniki

Kluczowe pola u góry pliku:

- `PRIMARY_SIGNAL` – wybór głównego źródła sygnału **OPEN**:
  - wartości: `"RSI" | "MA" | "MACD" | "BB" | "ATR" | "STOCH" | "STOCH_RSI" | "ATR_PCT" | "PCT_CHANGE" | "FEAR_GREED" | "VOLUME" ...`
  - steruje tym, którą funkcję `_primary_*` w `RSIStrategy` wołamy (`_primary_rsi`, `_primary_ma`, `_primary_macd`, `_primary_bb`, itd.).
- `RISK_MODE` – sposób wyznaczania TP/SL/TS:
  - `"FIXED"` – stałe mnożniki na cenę (`RISK_PARAMS_FIXED` w `rsi.py`).
  - `"ATR"` – mnożniki oparte o ATR (`RISK_PARAMS_ATR` w `rsi.py` + nadpisy `RISK_PARAMS_ATR_OVERRIDES` w `rsi_config.py`).
- `STORE_ENTRY_ATR` – jeśli `True`, ATR z momentu wejścia (`entry_atr`) jest zapisywany w sygnale (logi/statystyki, TS).
- `TRAILING_STOP_ENABLED` – globalny włącznik trailing stopa (jeśli `False`, `get_risk_params` ustawia `trail_long/short=0`).
- `CLOSE_AFTER_X_CANDLES` – twardy limit czasu w świecach:
  - `0` = wyłączone.
  - `>0` = po tylu świecach od wejścia następuje **time-based close**, niezależnie od TP/SL/TS.

#### Styl egzekucji zamknięcia i slippage

- `CLOSE_EXECUTION_TYPE`:
  - `"on_candle_close"` – zamknięcie zawsze po **close** świecy, na której padł sygnał (po uwzględnieniu slippage).
  - `"on_crossover"` – zamknięcie po **intra-bar crossoverze** (TP/SL/TS/BB_close), z logiką, która próbuje przybliżyć cenę do rzeczywistego momentu przebicia.
- `CLOSE_EXECUTION_SLIPPAGE` – slippage **na wyjściu**:
  - liczba w **ułamku ceny** (np. `0.0005` = 0.05% = 5 bps).
  - dodatnia wartość = zawsze „gorsza” egzekucja (dla long: niżej niż idealnie, dla short: wyżej niż idealnie).
- `ENTRY_EXECUTION_SLIPPAGE` (w nowszej wersji) – analogiczny slippage **na wejściu**, używany w test_workerze przy ustalaniu realnego `entry_price`.

### 3.2. SL_UPDATER (StopLoss Updater)

Sekcja: `SL_UPDATER = { ... }` – logika przesuwania SL-a w trakcie trade’a.

Kluczowe pola:

- `enabled` – globalny włącznik całego SLU.
- `static_jump_enabled` – włącznik **tylko** „statycznego skoku” SL (BE/pół-BE itp.).
- **Static jump:**
  - `trigger_move_SL` – próg (0.10–1.00) jako **procent drogi** od entry do TP, przy którym robimy JEDNORAZOWY skok SL-a.
  - `move_SL_to` – nowa pozycja SL-a, znowu w [0.0–1.0] drogi entry→TP:
    - `0.0` = break-even,
    - `0.5` = połowa drogi.
  - `range_type` – aktualnie `"entry_to_tp"` (range referencyjny).
- **Dynamic SL:**
  - `dynamic_SL` – jeśli `True`, SL **podąża za new high/low** w trakcie trade’a:
    - dla longa: gdy cena robi nowe high, SL podnosi się o tę samą różnicę względem entry (ale nigdy nie spada niżej niż początkowy SL / dotychczasowy SL).
    - dla shorta: odwrotnie (nowy low przesuwa SL w dół, ale nigdy w górę względem entry niż pierwotny poziom).

Wszystko to jest zaimplementowane w **`test_worker.py`** w sekcji z komentarzami o `StopLoss updater: static jump (BE/custom) + dynamic SL` – tam widać, kiedy i w jaki sposób modyfikowany jest `position['sl_level']`.

### 3.3. Bucketizacja wskaźników

Sekcja `BUCKET = { ... }` steruje tym, jak RSIStrategy kompresuje świece przed liczeniem wskaźników:

- `type` – `rolling / `fixed`:
  - `"rolling"` – w pełni przesuwne okno (OHLC z okna, stride=1).
  - `"fixed"` – nieprzesuwne segmenty (np. co 5 świec nowy bucket).
- `window` – długość okna w świecach (np. `5`).

Na tej podstawie `RSIStrategy.compute_indicators` używa `_bucketize_rolling_ohlc` lub `_bucketize_non_overlapping`, a potem `_indicators_on_bucketed` do liczenia wskaźników, które następnie są **rozciągane** z powrotem do bazy (FFILL) jeśli trzeba.

### 3.4. Minimalne progi TP/SL (entry validation)

W configu znajdują się też progi bezpieczeństwa dla otwierania pozycji:

- `min_tp_threshold` – minimalna odległość TP od entry w % (np. `0.004` = 0.4%).
- `min_sl_threshold` – minimalna odległość SL od entry w %.

Trade jest otwierany tylko wtedy, gdy:

- `abs(TP - entry)/entry * 100 >= min_tp_threshold`,
- `abs(SL - entry)/entry * 100 >= min_sl_threshold`.

Zaimplementowane w logice test_workera: jeśli ryzyko/target jest „za blisko”, sygnał jest ignorowany.

### 3.5. RISK_PARAMS_ATR_OVERRIDES

Sekcja:

```python
RISK_PARAMS_ATR_OVERRIDES = {
    "tp_k_long":  5.0,
    "sl_k_long":  3.0,
    "tp_k_short": 5.0,
    "sl_k_short": 3.0,
    "ts_k_long":  0.8,
    "ts_k_short": 0.8,
    # opcjonalne limity min/max w FRAKCJACH ceny (0.001 = 0.1%)
}
```

Te wartości nadpisują domyślne `RISK_PARAMS_ATR` z `rsi.py`. W `RSIStrategy.__init__` następuje deep-merge tych struktur.

Interpretacja (w `get_risk_params`):

- ATR liczone jest w **jednostkach ceny**; `r = ATR / price`.
- Dla longa: 
  - `tp_long = 1.0 + tp_k_long * r`,
  - `sl_long = 1.0 - sl_k_long * r`,
  - `trail_long = 1.0 - ts_k_long * r` (jeśli trailing włączony).
- Analogicznie dla shorta: `tp_short`, `sl_short`, `trail_short`.

Dodatkowo można skonfigurować clampy `limits_min` / `limits_max` jako **minimalne/maksymalne odległości** od ceny, w ułamku ceny (np. 0.001 = 0.1%).

### 3.6. CLOSE_SIGNALS – dodatkowe sygnały zamknięcia

Sekcja `CLOSE_SIGNALS = { ... }` w `rsi_config.py` steruje tzw. **warstwą 2** wyjść (poza TP/SL/TS):

- `enabled` – globalny switch tej warstwy.
- `required_all` – jeśli `False` → wystarczy dowolny sygnał z aktywnych; jeśli `True` → wymagane wszystkie.

Podsekcje:

- `"BB_close"` – zamknięcia na podstawie Bollinger Bands:
  - `primary.enabled` – włącz/wyłącz BB_close.
  - `primary.mid_offset` – offset (–0.5…0.5) od środka pasma (MA) jako punkt odniesienia dla zamknięć.
  - `primary.inverted` – odwrócenie logiki mean-reversion/breakout.
- `"RSI_close"` – zamknięcia na poziomach RSI:
  - `close_long` – jeśli RSI ≥ close_long → zamknij longa.
  - `close_short` – jeśli RSI ≤ close_short → zamknij shorta.
- `"FEAR_GREED_close"` – zamknięcia na indeksie Fear & Greed:
  - `close_long` – jeśli FNG ≥ próg → close LONG.
  - `close_short` – jeśli FNG ≤ próg → close SHORT.

Zaimplementowane w `RSIStrategy.close_position_signal`: jeśli aktywny jest `CLOSE_SIGNALS`, strategia zwraca np. `{"signal_type": "BB_close"}` i risk-engine w `test_worker` dodaje odpowiedni close-candidate.

### 3.7. INDICATOR CONFIG – rodziny wskaźników

W `rsi_config.py` znajdziesz duży słownik opisujący wskaźniki (`"RSI"`, `"MA"`, `"MACD"`, `"BB"`, `"ATR"`, `"STOCH"`, `"STOCH_RSI"`, `"PCT_CHANGE"`, `"VOLUME"`, `"FEAR_GREED"`).

Każdy wpis ma mniej więcej strukturę:

```python
"MACD": {
    "enabled": True,
    "display": "sub2",
    "params": { ... },
    "primary": { ... },   # jeśli PRIMARY_SIGNAL == "MACD"
    "confirm": { ... },   # dodatkowe potwierdzenia
}
```

Wspólne pola:

- `enabled` – czy wskaźnik jest liczony i (potencjalnie) wyświetlany.
- `display` – gdzie ma się pojawić w GUI (`"main"`, `"sub1"`, `"sub2"`, `"sub3"`).
- `params` – parametry obliczeń (okna, typy średnich, itp.).
- `primary` – logika PRIMARY_SIGNAL dla tego wskaźnika (jeśli wybrany).
- `confirm` – dodatkowe warunki, które **zawężają** long_ok/short_ok (np. rising_n, min_abs, inverted).

Przykładowo:

- **MACD:**
  - `primary.need_cross=True` – sygnał wymaga przejścia histogramu przez zero.
  - `primary.confirm_bars` – ile świec po crossie histogram musi utrzymać nowy znak.
  - `confirm.long_rules` / `short_rules` – dodatkowe warunki typu `signal_below_zero`, `signal_lt_macd` itd.
- **RSI:**
  - `primary.oversold` / `overbought` – klasyczne progi 30/70 (domyślnie).
  - `confirm.use_level_50` – czy wymagamy, żeby RSI było po „właściwej” stronie 50 dla danego kierunku.
- **BB:**
  - `primary.mid_offset` – gdzie względem środka pasma definiujemy poziomy dla long/short.
  - `primary.use_cross` – czy wymagamy faktycznego crossa ceny z poziomem, czy wystarczy sama pozycja.
- **STOCH / STOCH_RSI:**
  - progi K/D dla overbought/oversold + logika crossów w `_primary_stoch_like`.
- **PCT_CHANGE / FEAR_GREED / VOLUME:**
  - wszystko sprowadza się do prostych progów / ratio, z możliwością `inverted=True`.

## 4. `rsi.py` – mechanika strategii

`rsi.py` zawiera klasę **`RSIStrategy(BaseStrategy)`**, która implementuje wszystkie wymagane metody dla engine/test_workera:

- `compute_indicators` / `compute_indicators_segmented` – liczenie wskaźników na ramce świec.
- `open_position_signal` – logika sygnałów otwarcia pozycji.
- `close_position_signal` – warstwa close_signals (BB/RSI/FNG).
- `get_risk_params` – zwraca aktualne mnożniki TP/SL/TS dla bieżącej ceny/ATR.
- `get_close_after_x_candles` – twardy limit czasu w świecach.
- `get_indicator_names`, `get_display_config` – info dla GUI/DB jakie kolumny mamy i gdzie je rysować.

### 4.1. Obliczanie wskaźników (`compute_indicators`)

Fragment kluczowy:

```python
def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
    df = self._ensure_time_index(df)
    df = self._prepare_numeric_ohlc(df)
    ...
    if self.bucket_mode == "fixed":
        bucketed = self._bucketize_non_overlapping(df, self.indicator_bucket)
    else:
        bucketed = self._bucketize_rolling_ohlc(df, self.indicator_bucket)
    out = self._indicators_on_bucketed(bucketed)
    # dodatkowe kolumny (ATR, VOLUME avgs, itd.)
    return out
```

Dodatkowo:

- `compute_indicators_segmented` – wersja **gap-aware**, która dzieli dane na segmenty czasowe bez dużych dziur (na podstawie `config.RSI_SEGMENTATION_*`) i liczy wskaźniki osobno na każdą ciągłą sekwencję.
- `get_required_base_need` – mówi engine’owi, ile świec „rozgrzewki” potrzeba na najdłuższy wskaźnik (np. slow MA, ATR czy STOCH).

### 4.2. Sygnały otwarcia (`open_position_signal`)

Sygnatura:

```python
def open_position_signal(self, row, current_position):
```

Logika (w skrócie):

1. Jeśli `current_position` **nie jest None** (już mamy pozycję na symbolu) → nie tworzymy nowego sygnału (ale aktualizujemy stan primary, żeby nic nie „strzeliło” z opóźnieniem).
2. `self._primary_signal_ok(row, symbol, blocked=False)` – w zależności od `PRIMARY_SIGNAL` wybiera i woła odpowiednią funkcję:
   - `_primary_rsi`, `_primary_ma`, `_primary_macd`, `_primary_bb`, `_primary_atr`, `_primary_stoch_like`, `_primary_atr_pct`, `_primary_pct_change`, `_primary_fear_greed`, `_primary_volume`.
   - każda zwraca `(long_ok, short_ok)`.
3. Jeśli któryś kierunek jest potencjalnie ok, `_apply_confirmations` odpala dodatkowe warstwy confirm (`confirm` sekcje w rsi_config).
4. Jeśli finalnie `long_ok` lub `short_ok` jest True → budujemy sygnał:

```python
if long_ok and not short_ok:
    sig = {"signal_type": "open_long", "amount": POSITION_SIZE / price}
elif short_ok and not long_ok:
    sig = {"signal_type": "open_short", "amount": POSITION_SIZE / price}
else:
    return None

if STORE_ENTRY_ATR and np.isfinite(entry_atr):
    sig["entry_atr"] = entry_atr
return sig
```

Test_worker później dobudowuje z tego pełną pozycję (TP/SL/TS, SLU, czas wejścia, itp.).

### 4.3. Sygnały zamknięcia (`close_position_signal`)

`close_position_signal` implementuje tylko **warstwę CLOSE_SIGNALS** – nie dotyka TP/SL/TS:

```python
def close_position_signal(self, row, current_position):
    cfg_all = self.close_signals_cfg
    if not cfg_all.get("enabled", False) or current_position is None:
        return None
    side = current_position["side"]
    ...  # BB_close, RSI_close, FEAR_GREED_close
    if required_all:
        ... # wszystkie muszą być True
    else:
        ... # wystarczy dowolny True
    for key in ("BB_close", "RSI_close", "FEAR_GREED_close"):
        if hits.get(key):
            return {"signal_type": key}
    return None
```

Test_worker wykorzystuje ten `signal_type` i dodaje odpowiedni candidate z `base_price` równym `close` (plus slippage) w zależności od trybu egzekucji.

### 4.4. Risk params (`get_risk_params`)

`get_risk_params` spina `RISK_MODE`, ATR/fixed i globalny toggle trailing stopa:

```python
if self.risk_mode.upper() == "FIXED":
    rp = dict(self._risk_params_fixed)
    rp.setdefault("trail_long",  self._risk_params_fixed.get("trail_long", 0.9975))
    rp.setdefault("trail_short", self._risk_params_fixed.get("trail_short", 1.0025))
    if not self.trailing_stop_enabled:
        rp["trail_long"] = 0.0
        rp["trail_short"] = 0.0
    return rp

price = self._last_price
atr = self._last_atr
r = atr / price
k = self._risk_params_atr
tp_long   = 1.0 + k["tp_k_long"]   * r
sl_long   = 1.0 - k["sl_k_long"]   * r
... itd. ...
```

Test_worker przy wejściu pozycji mnoży `entry_price` przez te mnożniki, żeby wyznaczyć nominalne poziomy TP/SL/TS. Dalej w grę wchodzą jeszcze SLU i execution-type ze swojej logiki.

## 5. `test_worker.py` – jak z configu robi się trade

`TestWorker` to QThread, który odpala właściwy backtest dla danej strategii (tutaj `RSIStrategy`). Jest to **najważniejsze miejsce**, jeśli chodzi o to, jak wszystkie parametry strategii przekładają się na konkretne wejścia/wyjścia i finalne trade’y.

### 5.1. Inicjalizacja

Przy starcie worker robi m.in.:

- ładuje klasę strategii (`strategy_class`, zwykle `RSIStrategy`),
- tworzy `MultiSymbolEngine` z listą symboli i DB,
- zapisuje snapshot `rsi_config` (do DB, jeśli obsługiwane),
- czyści tabele wskaźników dla danego testu,
- odpala pętlę po symbolach i świecach.

### 5.2. Główna pętla po świecach

W dużym skrócie, dla każdego symbolu:

1. Pobiera ramkę świec z DB (`db_pg.get_candles`).
2. Normalizuje kolumny czasu (`_normalize_candles_df`).
3. Woła `strategy.compute_indicators_segmented` (jeśli dostępne) albo `compute_indicators`.
4. Dla każdej świecy (iterrows):
   - buduje `row_dict` (słownik close, high, low, wskaźniki, FNG, itp.),
   - odczytuje `current_position` z `engine.contexts[symbol].current_position`,
   - **aktualizuje trailing stop i SLU** (na podstawie TP/SL/ATR, new high/low, cooldown itd.),
   - zbiera listę **close candidates**: TP, SL, TS, CLOSE_AFTER_X_CANDLES, BB_close, RSI_close, FEAR_GREED_close, itp.,
   - osobno rozważa **open signal** (`strategy.open_position_signal`) jeśli nie ma pozycji.

Istotne kawałki:

- Trailing stop z cooldownem jednej świecy (dla `on_crossover`) – żeby TS nie wyzwalał się w tej samej świecy, w której został uzbrojony / zaktualizowany.
- SL_UPDATER (static jump + dynamic SL) – działa **niezależnie** od trailing stopa, przesuwając tylko poziom SL-a.
- `CLOSE_EXECUTION_TYPE` + `CLOSE_EXECUTION_SLIPPAGE` – decydują, **kiedy** i na jakiej cenie zamkniemy pozycję, gdy mamy kilka możliwych przyczyn.

### 5.3. Kaskada close-candidates

Test_worker buduje listę `candidates`, gdzie każdy element ma mniej więcej postać:

```python
{
    "reason": "TP" / "SL" / "TS" / "BB_close" / "RSI_close" / "FEAR_GREED_close" / "time_close" / itd.,
    "side":   "long"/"short",
    "base_price": <cena wynikająca z danego mechanizmu>
}
```

Następnie:

1. Na końcu świecy (po sprawdzeniu logiki intra-bar i CLOSE_SIGNALS) worker **aplikuje globalny slippage** (`_apply_slippage`) do każdej `base_price` → `exec_price`.
2. Dla longa wybierany jest **najgorszy** (najniższy) `exec_price`, dla shorta – **najgorszy** (najwyższy).
3. Tworzony jest „fake candle” z `close_time = idx` (zawsze **czas zamknięcia świecy**) oraz `close = exec_price`.
4. Wywoływany jest `engine.close_position(context, fake_row, reason)`, który aktualizuje `SymbolContext`, liczy PnL, fees (`FEE_RATE`) itd., a wynik leci do DB/GUI.

### 5.4. Wejście pozycji – TP/SL/TS/SLU

Gdy `open_position_signal` zwróci sygnał:

1. Worker wyciąga `side` (long/short) z sygnału.
2. Odczytuje `risk = strategy.get_risk_params()` – dostaje mnożniki `tp_*`, `sl_*`, `trail_*`.
3. Ustala `entry_price` (zależnie od execution slippage na wejściu).
4. Wylicza:
   - `tp_open` (wartość nominalna TP),
   - `sl_initial` (początkowy SL),
   - `ts_level` (poziom trailing stopa, jeśli włączony).
5. Zakłada strukturę pozycji w `SymbolContext.current_position`, z polami typu:

```python
{
  "trade_id": ...,
  "symbol":   symbol,
  "side":     "long"/"short",
  "entry_price": <po slippage>,
  "entry_price_raw": <close bez slippage>,
  "tp_open": <target>,
  "sl_initial": <pierwotny SL>,
  "sl_level": <aktualny SL>,
  "ts_level": <aktualny TS> (jeśli aktywny),
  "sl_jump_enabled": <czy static jump jest aktywny>,
  "sl_jump_triggered": False,
  ...
}
```

Dalej podczas trwania trade’a TS i SLU przesuwają odpowiednio `ts_level` i `sl_level`, a crossy z tymi poziomami w zależności od `CLOSE_EXECUTION_TYPE` generują close-candidate’y.

## 6. `engine.py`, `context.py`, `db_pg.py` – rola we flow strategii

### 6.1. `MultiSymbolEngine` (`engine.py`)

- Utrzymuje mapę `symbol -> SymbolContext`.
- Odpowiada za tworzenie/zamykanie pozycji w sposób spójny z DB i z GUI.
- Ma metody typu `open_position`, `close_position`, które:
  - tworzą wpis pozycji w `SymbolContext`,
  - aktualizują listę trejdów,
  - raportują ticki wokół trejdów (dla `ticks_table`),
  - przygotowują paczki do zapisu w DB (wrzucane na `db_queue`).
- W trybie „online” (`on_tick`) może być wykorzystywany do live/livetestu (na razie głównie używany batchowo przez `test_worker`).

### 6.2. `SymbolContext` (`context.py`)

`SymbolContext` to „stan wszystkiego” dla pojedynczego symbolu:

- `current_position` – dict z aktualnie otwartą pozycją (albo `None`).
- `trades` – lista zakończonych trade’ów.
- `tick_buffer` – bufor tików w okolicach trade’ów (do późniejszej analizy).
- liczniki typu `trade_id_counter`, `last_closed_trade_time` itd.

Engine i test_worker **zawsze** pracują na tym kontekście – to jest jedyne źródło prawdy o stanie pozycji na dany symbol.

### 6.3. `db_pg.py` – Postgres backend

- Odpowiada za zapisywanie świec, wskaźników, trejdów, ticków, konfiguracji runu (tam, gdzie jest obsługa).
- `indicators_historical` – wspólna tabela na wskaźniki z wszystkich strategii / symboli (klucze: `symbol, open_time, close_time`).
- Funkcje typu `save_run_config`, `clear_indicators_table`, `insert_ticks`, `insert_trades` są wykorzystywane przez engine/test_worker.
- Strategia sama w sobie (`RSIStrategy`) **nie dotyka DB** – komunikuje się tylko przez wartości zwracane do test_workera/engine’u.

## 7. Podsumowanie

W skrócie, cała ścieżka wygląda tak:

- **`rsi_config.py`** – definiuje *jak myślimy* o rynku: które wskaźniki, jakie progi, tryby risku, TS, SLU, close_signals.
- **`RSIStrategy` (`rsi.py`)** – zamienia to na: `compute_indicators`, `open_position_signal`, `close_position_signal`, `get_risk_params`.
- **`test_worker.py`** – łączy logikę strategii z mechaniką egzekucji: TS, SLU, cooldowny, execution_type, slippage, time-based close.
- **`MultiSymbolEngine` + `SymbolContext`** – pilnują spójności stanu pozycji na wielu symbolach i dbają o trade lifecycle.
- **`db_pg.py`** – trzyma wynik testu w DB, tak aby **GUI** i dalsza analiza mogły to łatwo odczytać.

Z poziomu tradera oznacza to: możesz **kombinować dowolnie** w `rsi_config.py` (PRIMARY_SIGNAL, ATR vs FIXED, progi, SLU, TS, CLOSE_SIGNALS), a ta sama logika będzie spójnie przechodzona przez backtest headless i backtest z GUI – różnić się będzie tylko to, czy zapisujesz rzeczy do DB oraz jak to potem oglądasz.
