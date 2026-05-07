# main.py
from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(usecwd=True), override=True)

import sys
from queue import Queue

from PyQt5.QtWidgets import QApplication
from gui.main_window import MainWindow
import gui.ma_overlay_patch  # aktywuje overlay MA na głównym wykresie
from data.db_pg import Database

import tracemalloc

# start monitoring memory allocations (opcjonalne, ale przydatne)
tracemalloc.start()

# globalne instancje bazy i kolejki zadań do DB
db = Database()
db_queue = Queue()


def main():
    """Główna funkcja aplikacji – tworzy GUI i przekazuje db + kolejkę do MainWindow."""
    app = QApplication(sys.argv)
    main_window = MainWindow(db, db_queue)
    main_window.resize(1600, 1000)
    main_window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
