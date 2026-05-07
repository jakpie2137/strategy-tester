# config.py

# === WRITE INDICATORS TO DB FLAG ===
WRITE_INDICATORS_TO_DB = True  # save indicators to database during test?

# Jeśli powyżej jest False, możemy po zakończeniu testu (gdy wszystko już policzone w RAM)
# zrzucić wskaźniki do DB w trybie "bulk z RAM-u", bez spowalniania samego testu.
WRITE_INDICATORS_TO_DB_FROM_RAM_AFTER_TEST = False  # True = po teście zapisz wskaźniki z RAM do DB

# === GAP DETECTION / SEGMENTATION ===
RSI_SEGMENTATION_ENABLED = False
RSI_SEGMENTATION_DEBUG = False

# --- DB / indicators insert modes ---

# Czy przy wstawianiu wskaźników robić UPSERT (INSERT ... ON CONFLICT ... DO UPDATE ...),
# czy zwykły INSERT (szybszy, ale wymaga braku duplikatów).
INDICATOR_UPSERT = False  # True = bezpieczna opcja, False = performance, ale zakładamy brak duplikatów
INDICATORS_BACKFILL_OPEN_TIME = False  # jeśli chcesz max performance na testach

# --- intervals ---
DEFAULT_FETCH_INTERVAL = 5      # seconds
DEFAULT_CANDLE_INTERVAL = 1     # minutes

# --- strategies & symbols ---
STRATEGY_CHOICES = {
    "RSI": "RSIStrategy",
    "MA": "MAStrategy"
}

AVAILABLE_PAIRS = [
    # "BTCUSDT",
    "ETHUSDT",
    # "BNBUSDT",
    # "SOLUSDT",
    "DOGEUSDT",
    # "ADAUSDT",
    # "LINKUSDT",
    # "XRPUSDT",
    # "HBARUSDT",
    # "XLMUSDT",
    # "SUSDT",
    # "FARTCOINUSDT",
    # "1000PEPEUSDT",
    # "1000BONKUSDT"
    # "1000SHIBUSDT"
]
DEFAULT_PAIR = "BTCUSDT"
PAIR_PRICE_PRECISION = {
    "BTCUSDT": 1,  # 108980.5
    "ETHUSDT": 2,
    "BNBUSDT": 3,
    "SOLUSDT": 3,
    "LINKUSDT": 3,
    "ADAUSDT": 4,
    "DOGEUSDT": 5,  # 0.173868
    "XRPUSDT": 4,
    "HBARUSDT": 5,
    "XLMUSDT": 5,
    "SUSDT": 4,
    "1000PEPEUSDT": 7,
    "FARTCOINUSDT": 4,
    "1000BONKUSDT": 6,
    "1000SHIBUSDT": 6
}

# --- Strategy global params ---
BIAS_CHOICES = ["None", "Long", "Short"]
DEFAULT_BIAS = "None"


POSITION_SIZE = 1000   # Docelowa wartość pozycji w USD
LEVERAGE = 10

FEE_RATE = 0.00045  # Binance Futures fee rate (Regular User) = 0.05 % (0.0005), paid in BNB 10% off = 0.045 % (0.00045)
TICKS_BEFORE_AFTER = 40

# --- STARTING BALANCE dla equity curve ---
STARTING_BALANCE = 10000.0


# ---- GUI parameters ----
# ---- LIMITY GUI ----
MAX_GUI_CANDLES = 10000         # ile pobierać z bazy do GUI
MAX_PLOT_CANDLES = 10000        # ile rysować na wykresie świeczek
MAX_WORKER_CANDLES = 10000      # na ilu swieczkach puszczamy test strategii

MAX_GUI_TRADES = 15000            # ile wyświetlać trade’ów w tabeli i wykresie
MAX_GUI_TICKS = 100              # ile ticków do tabeli
MAX_GUI_INDICATORS = 15          # ile wskaźników do tabeli


# --- SOFT RESTART / MEMORY LIMITS ---
GUI_SOFT_RESTART_RAM_MB = 3700   # RAM w MB – po przekroczeniu tego soft-restart GUI
GUI_KILL_RAM_MB = 4096           # RAM w MB – po przekroczeniu tego hard kill (sys.exit)
GUI_MAX_SOFT_RESTARTS = 4        # Ile razy próbować restartować zanim kill

# --- Charts visuals ---
# kolory świec
CANDLE_BULL_COLOR = "#00E676"  # zielony (bullish) LIGHT
CANDLE_BEAR_COLOR = "#FF1744"  # czerwony (bearish) LIGHT
# CANDLE_BULL_COLOR = "#005C2A"  # zielony (bullish) DARK
# CANDLE_BEAR_COLOR = "#990016"  # czerwony (bearish) DARK

# --- TP/SL line width (px) & TrailingStop line width (px) ---
ORDER_LINE_WIDTH = 1.0      # regular TP/SL width
TS_LINE_MULTIPLIER = 3.0    # TS width = ORDER_LINE_WIDTH * TS_LINE_MULTIPLIER

# transparency (0..255) for bodies and wicks
CANDLE_BODY_ALPHA = 255
CANDLE_WICK_ALPHA = 255

# === Trade marker styling (GUI) ===
# ENTRY
TRADE_ENTRY_MARKER_SIZE = 20
TRADE_ENTRY_LONG_COLOR  = "#00C800"   # ciemnozielony (0,200,0)
TRADE_ENTRY_SHORT_COLOR = "#DC0000"   # ciemnoczerwony (220,0,0)
TRADE_ENTRY_BORDER_COLOR = "#FFFFFF"  # biała obwódka
TRADE_ENTRY_BORDER_WIDTH = 1          # px

# EXIT
TRADE_EXIT_MARKER_SIZE = 20
TRADE_EXIT_BRUSH_COLOR = "#000000"    # czarne wypełnienie
TRADE_EXIT_BORDER_COLOR = "#FFFFFF"   # biała obwódka
TRADE_EXIT_BORDER_WIDTH = 1           # px


# === AGREGACJA WYKRESÓW ===

# dynamiczna agregacja — LOD
PLOT_DYNAMIC_AGG_ENABLED = True

# ---- Candle aggregation for faster charts - fixed (dynamic == False):
PLOT_AGGREGATION = 60  # 60 świec bazowych = 1 świeca na wykresie

# ile maksymalnie świeczek chcemy na ekranie (docelowo, przy największym zoom-out) (dynamic == True):
PLOT_MAX_VISIBLE_CANDLES = 240   # np. 800 / 1000 / 1200 – jak wolisz

# z tego wyliczamy sweet-spot MIN/MAX
PLOT_TARGET_MAX_BINS = PLOT_MAX_VISIBLE_CANDLES
PLOT_TARGET_MIN_BINS = max(100, PLOT_MAX_VISIBLE_CANDLES // 3)

# piramida interwałów (w minutach)
PLOT_PYRAMID_MINUTES = [1, 3, 5, 15, 60, 240, 1440, 10080]   # 1m,3m,5m,15m,1h,4h,1d,1W

# debounce LOD – ile ms po ostatnim zoom/pan czekamy, zanim przeliczymy
PLOT_LOD_DEBOUNCE_MS = 150

# histereza jakości – kandydat musi dać co najmniej o 30% mniejszy błąd względem targetu
PLOT_LOD_IMPROVEMENT_FACTOR = 0.7  # err_best < err_cur * 0.7 → dopiero wtedy zmieniamy poziom

# limity przewijania osi X
PLOT_X_MARGIN_MIN_BARS = 1000   # minimum świeczek marginesu po obu stronach
PLOT_X_MARGIN_FRAC = 0.10       # minimum 10% długości danych marginesu po obu stronach


# --- Performance / debug flags ---
# Uwaga: ustaw na True tylko podczas profilowania – logi mogą być bardzo obszerne.
PERF_DEBUG = True          # ogólne pomiary czasu (np. w StrategyTestWorker)
DB_DEBUG = False            # dodatkowe logi i pomiary czasu zapytań SQL w db_pg
WORKER_RAM_DEBUG = False    # opcjonalne logi użycia RAM w workerach


# === TEST WORKER PARAMETERS ===

# === BATCH SIZE / FLUSH SETTINGS ===

# Ile wierszy wskaźników trzymamy w pamięci w workerze,
# zanim wrzucimy je na kolejkę DB.
INDICATOR_FLUSH_ROWS = 10000

# Ile wierszy wskaźników na pojedynczy UPSERT do Postgresa.
INDICATOR_BATCH_SIZE = 10000

# Co ile świec logujemy chunk w [PERF] main_loop_chunk_X – czysto diagnostyczne.
PERF_CHUNK_ROWS = 10000

# Ile trejdów w jednym INSERT batchu.
TRADE_BATCH_SIZE = 500


# === GUI Layout presets ===
# Available modes:
#   1 -> Left: Charts, Right: Widgets (tabs), Bottom: Logs
#   2 -> Top: Charts, Bottom: [Logs | Widgets] split horizontally
#   3 -> Left: Charts, Right: [Widgets over Logs] split vertically
LAYOUT_DEFAULT = 2

# Per-section constraints per layout (px).
# Keys: 'charts', 'widgets', 'logs' -> dict with 'min_w', 'min_h', optional 'max_w', 'max_h'.
# You can use None for no limit. 'default_sizes' describe splitter proportions used on first load.
LAYOUT_LIMITS = {
    1: {
        'charts':  {'min_w': 800, 'min_h': 500},
        'widgets': {'min_w': 300, 'min_h': 500},
        'logs':    {'min_w': 800, 'min_h': 300},
        'default_sizes': {'h': [1100, 450], 'v': [1, 0]}  # horizontal (charts|widgets), v not used
    },
    2: {
        'charts':  {'min_w': 800, 'min_h': 400},
        'widgets': {'min_w': 800, 'min_h': 300},
        'logs':    {'min_w': 300, 'max_w': 720, 'min_h': 300},
        'default_sizes': {'v': [800, 300], 'h_bottom': [800, 500]}  # vertical (charts|bottom), bottom split (logs|widgets)
    },
    3: {
        'charts':  {'min_w': 800, 'min_h': 500},
        'widgets': {'min_w': 400, 'min_h': 500},
        'logs':    {'min_w': 300, 'min_h': 300},
        'default_sizes': {'h': [1100, 450], 'v_right': [500, 300]}  # horizontal (charts|rightcol), rightcol split (widgets|logs)
    }
}

# Minimum window size so the presets have room to breathe
MAINWINDOW_MIN_WIDTH  = 1250
MAINWINDOW_MIN_HEIGHT = 850

# === CHECK, czy FEAR_GREED jest aktualne przed HEADLESS TESTEM
SYNC_FEAR_GREED_ON_TEST_START = True