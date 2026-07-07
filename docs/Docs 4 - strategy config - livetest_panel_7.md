# Docs 4 – Strategy config – LIVETEST_PANEL_7

_Szczegółowa dokumentacja konfiguracji i implementacji strategii `RSIStrategy` (pliki `rsi.py` + `rsi_config.py`, z odniesieniami do `test_worker.py`)._

## Spis treści

- [1. Filozofia konfiguracji RSIStrategy](#1.-filozofia-konfiguracji-rsistrategy)
- [2. Globalne przełączniki strategii (rsi_config.py)](#2.-globalne-przelaczniki-strategii-(rsi_config.py))
  - [2.1. PRIMARY_SIGNAL – co generuje sygnały OPEN](#2.1.-primary_signal-–-co-generuje-sygnaly-open)
  - [2.2. RISK_MODE – jak liczymy TP/SL/TS](#2.2.-risk_mode-–-jak-liczymy-tp/sl/ts)
  - [2.3. BUCKET – agregacja świec pod wskaźniki](#2.3.-bucket-–-agregacja-swiec-pod-wskazniki)
  - [2.4. CLOSE_EXECUTION_TYPE + slippage](#2.4.-close_execution_type-+-slippage)
  - [2.5. CLOSE_AFTER_X_CANDLES – limit czasu pozycji](#2.5.-close_after_x_candles-–-limit-czasu-pozycji)
  - [2.6. Progi minimalnego TP/SL (entry validation)](#2.6.-progi-minimalnego-tp/sl-(entry-validation))
  - [2.7. CLOSE_SIGNALS – warstwa dodatkowych wyjść](#2.7.-close_signals-–-warstwa-dodatkowych-wyjsc)
  - [2.8. SL_UPDATER – static jump + dynamic SL](#2.8.-sl_updater-–-static-jump-+-dynamic-sl)
- [3. Rodziny wskaźników – konfiguracja i logika](#3.-rodziny-wskaznikow-–-konfiguracja-i-logika)
  - [3.1. RSI](#3.1.-rsi)
  - [3.2. MA (średnie kroczące)](#3.2.-ma-(srednie-kroczace))
  - [3.3. MACD](#3.3.-macd)
  - [3.4. Bollinger Bands (BB)](#3.4.-bollinger-bands-(bb))
  - [3.5. STOCH i STOCH_RSI](#3.5.-stoch-i-stoch_rsi)
  - [3.6. PCT_CHANGE](#3.6.-pct_change)
  - [3.7. FEAR_GREED](#3.7.-fear_greed)
  - [3.8. VOLUME](#3.8.-volume)
- [4. Jak config przekłada się na open/close (w skrócie)](#4.-jak-config-przeklada-sie-na-open/close-(w-skrocie))
  - [4.1. Open](#4.1.-open)
  - [4.2. Close (warstwa strategii)](#4.2.-close-(warstwa-strategii))
- [5. Checklist – co jest ważne przy konfiguracji RSIStrategy](#5.-checklist-–-co-jest-wazne-przy-konfiguracji-rsistrategy)


## 1. Filozofia konfiguracji RSIStrategy

`RSIStrategy` jest w tym projekcie **silnikiem strategii wielowskaźnikowej**. Nazwa została po „pierwszym RSI”, ale aktualnie config pozwala budować setupy oparte o:

- oscylatory: `RSI`, `STOCH`, `STOCH_RSI`,
- trend/price action: `MA`, `MACD`, `BB` (Bollinger Bands),
- zmienność/risk: `ATR`, `ATR_PCT`,
- momentum: `PCT_CHANGE`,
- czynniki zewnętrzne: `FEAR_GREED`,
- wolumen: `VOLUME`.

Podział odpowiedzialności:

- **`rsi_config.py`** – deklaratywny opis *co* liczymy i na jakich zasadach generujemy sygnały / confirmy / zamknięcia,
- **`rsi.py`** – implementacja *jak* to liczymy: obliczanie wskaźników, `_primary_*`, confirmy, `get_risk_params`, `open_position_signal`, `close_position_signal`,
- **`test_worker.py`** – łączy sygnały strategii z risk-engine (TP/SL/TS/SLU, slippage, execution type) i faktycznym lifecyklem pozycji.

W tym dokumencie skupiamy się na **polach konfiguracyjnych** i ich wpływie na zachowanie strategii – bez wchodzenia drugi raz w pełny opis całego backtest flow (to jest w Docs 3).

## 2. Globalne przełączniki strategii (rsi_config.py)

### 2.1. PRIMARY_SIGNAL – co generuje sygnały OPEN

- `PRIMARY_SIGNAL` – wybiera **główny wskaźnik**, na podstawie którego powstają sygnały otwarcia pozycji.
- Przykładowe wartości (wg komentarza w configu):
  - `"RSI" | "MA" | "MACD" | "BB" | "ATR" | "STOCH" | "STOCH_RSI" | ...`

Od strony implementacji:

- `PRIMARY_SIGNAL` mapuje się na odpowiednią metodę `_primary_*` w `RSIStrategy`, np.:
  - `"RSI"` → `_primary_rsi`
  - `"MA"` → `_primary_ma`
  - `"MACD"` → `_primary_macd`
  - `"BB"` → `_primary_bb`
  - `"STOCH"` / `"STOCH_RSI"` → `_primary_stoch_like`
  - `"PCT_CHANGE"` → `_primary_pct_change` itd.

Każda `_primary_*` zwraca krotkę `(long_ok, short_ok)` i używa do decyzji swoich sekcji `primary` + `confirm` z `INDICATOR_OVERRIDES`.

### 2.2. RISK_MODE – jak liczymy TP/SL/TS

`RISK_MODE` określa, jak z ceny wejścia (i ewentualnie ATR) wyliczamy poziomy TP/SL/TS:

- `"FIXED"` – używane są stałe mnożniki z `RISK_PARAMS_FIXED` w `rsi.py` (np. 1% w górę / 1% w dół, plus opcjonalny trail).
- `"ATR"` – użyte zostaną mnożniki z `RISK_PARAMS_ATR` (w `rsi.py`) nadpisane przez `RISK_PARAMS_ATR_OVERRIDES` z `rsi_config.py`.

W trybie ATR, w kodzie jest mniej więcej tak:

- `r = ATR / price`
- `tp_long   = 1.0 + tp_k_long * r`
- `sl_long   = 1.0 - sl_k_long * r`
- `trail_long = 1.0 - ts_k_long * r` (jeśli trailing włączony)

Opcjonalne pola `limits_min` i `limits_max` (zakomentowane w configu) pozwalają dodatkowo **clampować** odległości TP/SL/TS w *ułamkach ceny* (np. `0.001 = 0.1%`).

### 2.3. BUCKET – agregacja świec pod wskaźniki

W `rsi_config.py` bucket jest opisany komentarzem i wartościami domyślnymi. W praktyce służy do tego, żeby:

- liczyć wskaźniki na świecach „zagregowanych” (np. jak pseudo-M5 na bazie M1),
- mieć kontrolę, czy bucket jest `"rolling"` (przesuwny co świecę) czy `"fixed"` (kawałki nieprzesuwne),
- sterować efektywną długością okien (np. `window_fast` w MA mnożone przez BUCKET w trybie `FIXED`).

Szczegóły implementacyjne są w `RSIStrategy.compute_indicators` i `_bucketize_*`, ale z punktu widzenia configu ważne jest:

- większy bucket = bardziej wygładzone wskaźniki, mniej szumu,
- `type="fixed"` – decyzje są „schodkowe” (co bucket),                                 # WAŻNE: przy bucket FIXED, wskaźniki typu ATR, VOL będą znacznie większe - w rolling są dzielone przez wielkość okna
- `type="rolling"` – wskaźniki reagują płynniej na każdą świecę.                       # WAŻNE: przy bucked rolling, wskaźniki typu ATR, VOL będą mniejsze niż w FIXED o wysokim interwale, gdyż są ważone wielkością okna

- TYPY BUCKETÓW - WAŻNE !! :
- `type`:
  - `"rolling"` – klasyczne „window” przesuwane co świecę,                             # WAŻNE: przy bucked rolling, wskaźniki typu ATR, VOL będą mniejsze niż w FIXED o wysokim interwale, gdyż są ważone wielkością okna
  - `"fixed"` – okna nieprzesuwne (np. co 5 świec powstaje nowa świeca „agregowana”).  # WAŻNE: przy bucket FIXED, wskaźniki typu ATR, VOL będą znacznie większe - w rolling są dzielone przez wielkość okna
- `window`:

### 2.4. CLOSE_EXECUTION_TYPE + slippage

Z sekcji:

```python
CLOSE_EXECUTION_TYPE    = "on_candle_close"  # lub "on_crossover"
CLOSE_EXECUTION_SLIPPAGE = 0.0003            # 0.0005 = 0.05 % = 5 bps
```

- `CLOSE_EXECUTION_TYPE`:
  - `"on_candle_close"` – zamknięcia następują na `close` świecy, w której pojawił się sygnał (TP/SL/TS/BB/RSI/FNG/time-based),
  - `"on_crossover"` – `test_worker` próbuje „idealizować” moment przecięcia ceny z poziomami (TP, SL, TS, BB_close), wyliczając bardziej optymistyczny `exit_price`.
- `CLOSE_EXECUTION_SLIPPAGE`:
  - to jest zawsze **gorsza cena** niż idealna,
  - wartość to **ułamek ceny** (0.0003 = 3 bps),
  - long: cena egzekucji minimalnie niżej; short: minimalnie wyżej.

Analogicznie w projekcie jest osobny parametr na **slippage wejścia** (entry), używany w `test_worker` do korygowania `entry_price` – ale TP/SL pozostają liczone z surowego close’a.

### 2.5. CLOSE_AFTER_X_CANDLES – limit czasu pozycji

Z configu:

```python
CLOSE_AFTER_X_CANDLES   = 70  # 0 = OFF; >0 -> force-close after X candles
```

- `0` → mechanizm wyłączony, pozycja żyje tylko TP/SL/TS/CLOSE_SIGNALS.
- `>0` → po tylu świecach od wejścia `test_worker` dorzuca do candidate’ów `reason="time_close"` i wymusza wyjście z pozycji.

### 2.6. Progi minimalnego TP/SL (entry validation)

W dolnej części configu:

```python
min_tp_threshold = 0.004  # 0.4%
min_sl_threshold = 0.002  # 0.2%
```

Komentarz mówi jasno – są to **wartości w procentach** (czyli `0.30` to 0.30%, nie 30%). Warunek wejścia:

- `abs(TP - entry) / entry * 100 >= min_tp_threshold`
- `abs(SL - entry) / entry * 100 >= min_sl_threshold`

Jeśli którykolwiek z nich nie jest spełniony, `test_worker` ignoruje sygnał OPEN, nawet jeśli PRIMARY_SIGNAL mówi „long_ok/short_ok = True`.

### 2.7. CLOSE_SIGNALS – warstwa dodatkowych wyjść

Fragment z configu:

```python
CLOSE_SIGNALS = {
    "enabled": True,
    "required_all": False,

    "BB_close": { ... },
    "RSI_close": { ... },
    "FEAR_GREED_close": { ... },
}
```

- `enabled` – globalny switch całej warstwy close’ów strategii,
- `required_all` – jeśli `True`, wszystkie aktywne close-mechanizmy muszą naraz „trafić”; jeśli `False` (domyślnie) – wystarczy pierwszy hit.

Podsekcje w tym configu:

- `BB_close`:
  - `enabled` – włącz/wyłącz ten mechanizm,
  - `primary.mid_offset` – offset (–0.5…0.5) w **ułamku** pasma wokół środka (MA) – z tego poziomu strategia buduje poziom referencyjny dla zamknięcia,
  - `primary.inverted` – `False` → domyślnie mean-reversion, `True` → breakout/continuation (odwraca stronę sygnału).
- `RSI_close`:
  - `enabled` – włącz/wyłącz,
  - `primary.close_long` – jeśli RSI ≥ ten próg → zamykamy longa,
  - `primary.close_short` – jeśli RSI ≤ ten próg → zamykamy shorta.
- `FEAR_GREED_close`:
  - `enabled` – włącz/wyłącz,
  - `primary.close_long` / `primary.close_short` – analogicznie jak RSI, tylko na indeksie FNG.

`RSIStrategy.close_position_signal` używa tych pól do budowania `hits` i zwraca np. `{"signal_type": "BB_close"}` – `test_worker` zamienia to w konkretną cenę zamknięcia i candidate w risk-engine.

### 2.8. SL_UPDATER – static jump + dynamic SL

Sekcja `SL_UPDATER` w configu (u Ciebie już z dodatkowymi flagami) wygląda w przybliżeniu tak:

```python
SL_UPDATER = {
    "enabled": True,
    "static_jump_enabled": True,
    "trigger_move_SL": 0.3,   # 30% drogi entry→TP
    "move_SL_to": 0.0,        # 0.0 = BE, 0.5 = połowa drogi entry→TP
    "range_type": "entry_to_tp",
    "dynamic_SL": False,
}
```

- `enabled` – globalny switch całego mechanizmu SLU,
- `static_jump_enabled` – **osobny** switch tylko dla jednorazowego skoku SL-a,
- `trigger_move_SL` – próg osiągnięcia *drogi od entry do TP*, przy którym wywołujemy jump (np. 0.3 = 30% tej drogi),
- `move_SL_to` – docelowa pozycja SL-a w [0.0–1.0] *tej samej drogi* (0.0 = BE, 0.5 = pół drogi, itd.),
- `range_type` – obecnie `"entry_to_tp"`, trzyma spójność referencji zakresu,
- `dynamic_SL` – jeśli `True`, to po aktywacji trade’a SL przesuwa się *w stronę zysku* za nowymi high/low:
  - long: nowe high → SL podąża w górę; spadek ceny nie obniża SL poniżej aktualnego poziomu,
  - short: nowe low → SL podąża w dół; wzrost ceny nie podnosi SL w górę.

Implementacja tego wszystkiego siedzi w `test_worker.py` – w sekcji `StopLoss updater: static jump (BE/custom) + dynamic SL`.

## 3. Rodziny wskaźników – konfiguracja i logika

Poniżej opisane są **realne pola** z `INDICATOR_OVERRIDES` w `rsi_config.py` – bez zmyślonych flag. Skupiam się na tym, co faktycznie jest w configu oraz na tym, jak te pola są używane w `rsi.py`.

### 3.1. RSI

Fragment z `INDICATOR_OVERRIDES`:

```python
"RSI": {
    "enabled": True,
    "display": "sub1",
    "params": {"window": 14},
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
```

Znaczenie pól:

- `enabled` – czy liczymy RSI i rysujemy go w GUI (tu: w `sub1`).
- `params.window` – długość okna RSI (standardowe 14).
- `primary`:
  - `enabled` – czy RSI może pełnić rolę PRIMARY_SIGNAL (jeśli `PRIMARY_SIGNAL == "RSI"`).
  - `oversold` / `overbought` – klasyczne progi, które `_primary_rsi` używa jako warunek kandydata:
    - `RSI < oversold` → kandydat na LONG,
    - `RSI > overbought` → kandydat na SHORT.
- `confirm`:
  - `enabled` – czy włączać warstwę confirmu dla RSI (działa zarówno gdy RSI jest primary, jak i gdy jest tylko filtrem).
  - `use_level_50` – jeśli `True`, `_primary_rsi` / confirm mogą wymagać, aby RSI było po odpowiedniej stronie 50,
  - `long_max` – maksymalny poziom RSI, przy którym LONG jest dozwolony (np. jeśli RSI > long_max → blokujemy long),
  - `short_min` – minimalny poziom RSI, przy którym SHORT jest dozwolony (np. jeśli RSI < short_min → blokujemy short).

### 3.2. MA (średnie kroczące)

Z configu:

```python
"MA": {
    "enabled": True,
    "display": "main",
    "params": {
        "type": "SMA",          # "SMA" | "EMA"
        "window_fast": 4,
        "window_slow": 36,
    },
    "primary": {
        "enabled": False,
        "type": "ma_cross_bullish",   # albo: "ma_cross_bearish", "price_ma_cross_bullish" / "price_ma_cross_bearish"
        "price_ma": "fast",
        "confirmation_bars": 2,
    },
    "confirm": {
        "enabled": False,
        "long_rules":  ["fast_gt_slow"],
        "short_rules": ["fast_lt_slow"],
        "combine": "any",
    },
}
```

Znaczenie:

- `params.type` – typ średnich (`SMA` vs `EMA`).
- `window_fast` / `window_slow` – długości fast/slow MA (w trybie `FIXED` mnożone jeszcze przez BUCKET).
- `primary.type`:
  - `"ma_cross_bullish"` – sygnał, gdy fast MA przecina slow MA w górę,
  - `"ma_cross_bearish"` – przecina w dół,
  - `"price_ma_cross_bullish"` / `"price_ma_cross_bearish"` – sygnał oparty o przecięcie ceny z MA (fast/slow zależnie od `price_ma`).
- `primary.price_ma` – określa, względem której MA porównujemy cenę, jeśli używamy wariantu price-vs-MA.
- `primary.confirmation_bars`:
  - liczba świec, przez które warunek ma się utrzymać **po crossie**,
  - np. `2` oznacza, że po crossie fast>slow sygnał jest uznany dopiero jeśli w kolejnych 2 barach relacja nadal się utrzymuje (mechaniczny filtr na „fałszywe” pojedyncze przecięcia).
- `confirm.long_rules` / `confirm.short_rules`:
  - są to listy nazw reguł, interpretowanych w `rsi.py`, np.:
    - `"fast_gt_slow"` – long wymaga fast > slow,
    - `"fast_lt_slow"` – short wymaga fast < slow,
    - dodatkowe reguły jak `"price_gt_fast"`, `"price_gt_slow"`, `"price_lt_fast"`, `"price_lt_slow"` mogą być wspierane w kodzie (część jest w komentarzu w configu),
  - `combine` (`"any"` / `"all"`) definiuje, czy wystarczy jedna reguła, czy wszystkie muszą być spełnione.

### 3.3. MACD

Z `rsi.py` (domyślna konfiguracja) oraz z `rsi_config.py` (overrides) mamy:

```python
"MACD": {
    "enabled": True,
    "display": "sub2",
    "params": {"fast": 12, "slow": 26, "signal": 9},
    "color": "#ff6b6b",
    "is_zero_always_visible": True,
    "primary": {
        "enabled": True,
        "need_cross": True,       # require histogram crossing 0
        "confirm_bars": 5,        # jeśli >0: wymagamy rising/dropping PRZED crossem, ale wchodzimy NA crossie
        "min_hist": 0.0,          # minimalne |hist| po obu stronach crossa
        "min_delta": 0.0,         # minimalna zmiana |macd-signal|
        "epsilon": 0.0,           # deadband wokół 0; jeśli 0 → używa min_hist,
    },
    "confirm": {
        "enabled": True,
        "long_rules":  ["signal_below_zero", "signal_lt_macd"],
        "short_rules": ["signal_above_zero", "signal_gt_macd"],
        "combine": "any",
        "rising_n": 5,  # trend bias (MACD_HIST rośnie/spada przez n barów)
    },
}
```

Najważniejsze parametry:

- `params.fast/slow/signal` – klasyczne ustawienia MACD.
- `primary.need_cross`:
  - jeśli `True`, sygnał powstaje **na przecięciu** histogramu przez 0; sama pozycja powyżej/poniżej zera nie wystarczy.
- `primary.confirm_bars`:
  - to jest dokładnie to, o co pytałeś – liczba barów, na których histogram musi mieć „konsekwentny” kierunek **przed** cross’em,
  - w komentarzu: „require rising/dropping BEFORE cross, but still open AT cross” – czyli:
    - LONG: przed crossem histogram powinien rosnąć przez `confirm_bars` świec,
    - SHORT: przed crossem histogram powinien spadać przez `confirm_bars` świec,
  - jeśli `0` → brak tego warunku, sygnał generowany tylko z samego crossa + innych progów.
- `primary.min_hist`:
  - minimalne |MACD_HIST| po obu stronach crossa; odsiewa sygnały, gdy histogram był „mikroskopijny” (prawie płaski).
- `primary.min_delta`:
  - minimalna zmiana |macd-signal| – zapewnia, że przed crossem w ogóle był jakiś sensowny ruch MACD względem signal-line.
- `primary.epsilon`:
  - „martwa strefa” wokół 0 – jeśli >0, histogram w przedziale (−epsilon, +epsilon) jest traktowany jak 0; jeśli 0 → fallback na `min_hist`.
- `confirm.long_rules` / `confirm.short_rules`:
  - `"signal_below_zero"` – dla LONG sygnał tylko, gdy liniowa MACD/signal jest poniżej 0,
  - `"signal_above_zero"` – dla SHORT sygnał tylko, gdy powyżej 0,
  - `"signal_lt_macd"` / `"signal_gt_macd"` – wymagają konkretnej relacji sygnał vs macd (typowe crossy MACD-line vs signal-line).
- `confirm.combine` – `"any"` vs `"all"` dla tych rules.
- `confirm.rising_n`:
  - to jest Twój „is_rising” – implementacja jest w `_macd_trend_ok` w `rsi.py`,
  - LONG wymaga, by `MACD_HIST` był **większy** niż `n` barów temu (hist rośnie w horyzoncie n),
  - SHORT wymaga, by `MACD_HIST` był **mniejszy** niż `n` barów temu (hist spada).

Jeśli `rising_n == 0`, ta część trend bias jest wyłączona.

### 3.4. Bollinger Bands (BB)

Z configu:

```python
"BB": {
    "enabled": True,
    "display": "main",
    "params": {"window": 20, "stdev": 2.0},
    "color": {"upper": "#cccccc", "middle": "#aaaaaa", "lower": "#cccccc"},
    "primary": {
        "enabled": False,
        "mid_offset": 0.0,
        "use_cross": True,
        "inverted": False,
    },
    "confirm": {
        "enabled": False,
        "min_pct_of_close": 0.0035,
        "combine": "and",
    },
}
```

- `params.window` / `stdev` – klasyczne parametry BB.
- `primary.mid_offset`:
  - w `[-0.5, 0.5]`, jako **ułamek szerokości pasma**,
  - 0.0 = środek pasma; dodatnie wartości przesuwają poziom referencyjny bliżej górnej bandy, ujemne – bliżej dolnej.
- `primary.use_cross` – jeśli True, `_primary_bb` wymaga realnego przecięcia ceny z poziomem (cena musi „przebić”), a nie tylko samego przebywania powyżej/poniżej.
- `primary.inverted` – przełącza mean-reversion vs breakout/continuation (zamienia logikę long/short).
- `confirm.min_pct_of_close` – minimalna odległość w ułamku ceny (np. 0.0035 = 0.35%), jaką cena musi zrobić względem poziomu BB, żeby sygnał był uznany za wystarczająco „mocny”.
- `confirm.combine` – tryb łączenia warunków z warstwy confirm (jeśli jest ich więcej).

### 3.5. STOCH i STOCH_RSI

W `INDICATOR_OVERRIDES` są bardzo podobne:

```python
"STOCH": {
    "enabled": True,
    "display": "sub2",
    "slot": 2,
    "params": {"k_window": 14, "d_window": 3, "smooth_k": 3},
    "primary": {"enabled": False},
    "confirm": {"enabled": False, "long_max": 20.0, "short_min": 80.0},
},
"STOCH_RSI": {
    "enabled": True,
    "display": "sub1",
    "slot": 2,
    "params": {"rsi_window": 14, "stoch_window": 14, "smooth_k": 3, "d_window": 3},
    "primary": {"enabled": False},
    "confirm": {"enabled": False, "long_max": 20.0, "short_min": 80.0},
},
```

- `params` – standardowe parametry oscylatorów (K/D + smoothing).
- `primary.enabled` – pozwala używać któregoś z tych oscylatorów jako PRIMARY_SIGNAL (przez `_primary_stoch_like`).
- `confirm.long_max` / `short_min`:
  - dla LONG typowo wymagasz, żeby oscylator był **poniżej** `long_max` (np. <20) – czyli wychodził ze strefy oversold,
  - dla SHORT – żeby był **powyżej** `short_min` (np. >80) – wychodził z overbought.

### 3.6. PCT_CHANGE

Fragment:

```python
"PCT_CHANGE": {
    "enabled": True,
    "display": "sub3",
    "slot": 3,
    "primary": {
        "enabled": False,
        "long_below": -0.5,
        "short_above": 0.5,
        "inverted": False,
    },
    "confirm": {
        "enabled": False,
        "min_abs": 0.65,
        "combine": "and",
    },
},
```

- `primary.long_below` – LONG, gdy zmiana procentowa jest mniejsza niż ten próg (np. silny spadek → mean-reversion),
- `primary.short_above` – SHORT, gdy zmiana jest większa niż próg (np. silny wzrost → mean-reversion),
- `primary.inverted` – odwraca logikę (kontrarian ↔ momentum),
- `confirm.min_abs` – minimalna bezwzględna zmiana, żeby sygnał był brany pod uwagę,
- `confirm.combine` – tryb łączenia warunków confirm (gdy jest ich więcej).

### 3.7. FEAR_GREED

Z configu:

```python
"FEAR_GREED": {
    "enabled": True,
    "display": "sub1",
    "slot": 3,
    "primary": {
        "enabled": False,
        "long_max": 40.0,
        "short_min": 60.0,
        "inverted": False,
    },
    "confirm": {
        "enabled": False,
        "long_max": 30.0,
        "short_min": 70.0,
        "inverted": False,
    },
},
```

- `primary.long_max` / `short_min`:
  - LONG dozwolony tylko, jeśli FNG ≤ `long_max` (rynek „w strachu”),
  - SHORT tylko, jeśli FNG ≥ `short_min` (rynek „w chciwości”).
- `primary.inverted` – można łatwo odwrócić interpretację (np. grać kontrarian).
- `confirm.*` – dodatkowe zawężenie tych progów, gdy FEAR_GREED jest używany jako filtr dla innego PRIMARY_SIGNAL.

### 3.8. VOLUME

Z configu:

```python
"VOLUME": {
    "enabled": True,
    "display": "sub2",
    "slot": 3,
    "color": "#cccccc",
    "params": {"window": 2},  # VOL_AVG / BUY_VOL_AVG (krótki horyzont)
    "primary": {
        "enabled": False,
        "long_min_ratio": 0.75,
        "short_max_ratio": 0.25,
        "inverted": False,
    },
    "confirm": {
        "enabled": False,
        "min_mult": 2.0,   # VOL_AVG > min_mult * VOL_SMA
        "window": 10,      # okno VOL_SMA (długi horyzont)
    },
},
```

- `params.window` – okno do liczenia krótkoterminowego VOL_AVG / BUY_VOL_AVG.
- `primary.long_min_ratio` / `short_max_ratio`:
  - w logice w `rsi.py` przelicza się to na coś w stylu „jaki udział wolumenu jest buy/sell” i porównuje do progów,
  - `inverted=False` oznacza domyślne mapowanie (powyżej/pniżej progu), `True` – odwraca warunek.
- `confirm.min_mult` + `window`:
  - dodatkowy filtr: wolumen musi być co najmniej `min_mult` * `VOL_SMA(window)`,
  - praktycznie: LONG/SHORT wchodzą tylko na świecach z „anomalią” wolumenową względem długoterminowej średniej.

## 4. Jak config przekłada się na open/close (w skrócie)

To już raczej powtórka z Docs 3, więc tylko skrót ułożony pod kątem configu:

### 4.1. Open

1. Dla każdej świecy `RSIStrategy` ma dostęp do wszystkich kolumn wskaźników (policzonych w `compute_indicators`).
2. `PRIMARY_SIGNAL` wybiera właściwą `_primary_*`, która korzysta z odpowiedniej sekcji:
   - `INDICATOR_OVERRIDES[PRIMARY_SIGNAL]['primary']`,
   - i jeśli `...['confirm']['enabled']` → także z `...['confirm']` (np. `long_rules`, `min_abs`, `use_level_50`, `rising_n`).
3. `_primary_*` zwraca `(long_ok, short_ok)`.
4. `open_position_signal` decyduje, czy na tej podstawie stworzyć sygnał `open_long` / `open_short` (jeśli aktualnie brak pozycji na symbolu).
5. `test_worker` nakłada na to jeszcze:
   - `RISK_MODE` + `RISK_PARAMS_*` → wyznacza TP/SL/TS,
   - minimalne progi TP/SL (`min_tp_threshold` / `min_sl_threshold`),
   - execution slippage na wejściu.

### 4.2. Close (warstwa strategii)

- `close_position_signal` używa tylko `CLOSE_SIGNALS` (BB_close / RSI_close / FEAR_GREED_close),
- każdy z nich ma swój `enabled` + parametry progów (`mid_offset`, `close_long`, `close_short`, itd.),
- jeśli coś „trafia”, strategia zwraca np. `{"signal_type": "RSI_close"}`,
- `test_worker` dodaje to jako kolejny `close_candidate` obok TP/SL/TS/time_close i na końcu wybiera kandydat z najgorszą ceną dla danej strony (long/short), po doliczeniu `CLOSE_EXECUTION_SLIPPAGE`.

## 5. Checklist – co jest ważne przy konfiguracji RSIStrategy

Zebrałem to, co realnie jest w configu i co najmocniej wpływa na zachowanie strategii:

1. **Logika wejścia:**
   - `PRIMARY_SIGNAL`,
   - `INDICATOR_OVERRIDES[PRIMARY_SIGNAL]['primary']` (typ crossa, progi oversold/overbought, mid_offset, long_below/short_above, itd.),
   - `...['confirm']` (np. `use_level_50`, `long_rules` / `short_rules`, `min_abs`, `rising_n`).
2. **Risk / poziomy TP/SL/TS:**
   - `RISK_MODE` (`FIXED` vs `ATR`),
   - `RISK_PARAMS_ATR_OVERRIDES` (tp_k/sl_k/ts_k + opcjonalne limity min/max),
   - globalny `TRAILING_STOP_ENABLED`,
   - `SL_UPDATER` (`static_jump_enabled`, `trigger_move_SL`, `move_SL_to`, `dynamic_SL`).
3. **Dodatkowe wyjścia:**
   - `CLOSE_SIGNALS.enabled`,
   - konfiguracje `BB_close`, `RSI_close`, `FEAR_GREED_close` (progi domykania pozycji).
4. **Egzekucja i realizm:**
   - `CLOSE_EXECUTION_TYPE` (`on_candle_close` vs `on_crossover`),
   - `CLOSE_EXECUTION_SLIPPAGE` + slippage na wejściu,
   - `CLOSE_AFTER_X_CANDLES` (twardy limit life-time pozycji).
5. **Geometria danych:**
   - `BUCKET` (type + window),
   - `min_tp_threshold` / `min_sl_threshold` – filtry czułości na „mikro-setupy”.

Po poprawkach powyżej dokument powinien teraz być w 100% spójny z realnym `rsi_config.py` i rzeczywistą logiką w `rsi.py` (m.in. MACD `confirm_bars` + `rising_n` zamiast abstrakcyjnego „is_rising”).
