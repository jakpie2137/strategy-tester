from collections import deque
from config import MAX_GUI_TRADES

class SymbolContext:
    def __init__(self, symbol, tick_buffer_size=120):
        self.symbol = symbol
        self.current_position = None  # dict: side, entry_price, entry_timestamp, amount, trade_id
        self.trade_id_counter = 1
        self.trades = deque(maxlen=MAX_GUI_TRADES)  # Najnowsze zamknięte trejdy
        self.tick_buffer = deque(maxlen=tick_buffer_size)
        self.last_closed_trade_time = None

    def next_trade_id(self):
        tid = self.trade_id_counter
        self.trade_id_counter += 1
        return tid

    def reset(self):
        self.current_position = None
        self.trades.clear()
        self.tick_buffer.clear()
        self.trade_id_counter = 1
        self.last_closed_trade_time = None

    def ram_debug(self):
        # Opcjonalnie: szybki RAM-report dla pojedynczego contextu
        import sys, gc
        print(f"[SymbolContext:{self.symbol}] Trades: {len(self.trades)}, "
              f"Current pos: {self.current_position is not None}, "
              f"Tick buffer: {len(self.tick_buffer)}")
        print(f"  Size: {sys.getsizeof(self)} bytes, "
              f"TickBuffer: {sys.getsizeof(self.tick_buffer)} bytes")
        print(f"  All SymbolContext: {sum(1 for o in gc.get_objects() if type(o).__name__ == 'SymbolContext')}")
