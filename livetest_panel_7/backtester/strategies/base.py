class BaseStrategy:
    def open_position_signal(self, df, current_position):
        raise NotImplementedError

    def close_position_signal(self, df, current_position):
        raise NotImplementedError

    @staticmethod
    def get_strategy_name():
        return "base"

    @staticmethod
    def get_indicator_names():
        return []
