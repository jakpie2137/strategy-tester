import argparse
from tools.fng_integration import sync_fear_greed
from data.db_pg import Database  # updated import

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=str, help="YYYY-MM-DD", default=None)
    p.add_argument("--end", type=str, help="YYYY-MM-DD", default=None)
    _ = p.parse_args()

    db = Database()
    total = sync_fear_greed(db=db)
    print(f"Fear&Greed synced: {total} rows")

if __name__ == "__main__":
    main()
