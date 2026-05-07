from tools.fng_integration import sync_fear_greed
from data.db_pg import Database  # updated import

if __name__ == "__main__":
    total = sync_fear_greed(db=Database())
    print(f"Fear&Greed synced: {total} rows")
