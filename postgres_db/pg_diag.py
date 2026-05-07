# -*- coding: utf-8 -*-
from pathlib import Path
from config_pg import load_env, get_dsn, mask_dsn
from db_pg import ensure_database_exists
from psycopg import connect

ENV_DIR = Path(__file__).resolve().parent

def main():
    load_env(ENV_DIR / ".env")
    dsn = get_dsn()
    print("Using DSN:", mask_dsn(dsn))
    ensure_database_exists(dsn)
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("select version(), current_database(), current_user, current_setting('port'), current_setting('data_directory'), current_setting('search_path')")
        version, dbname, user, port, data_dir, sp = cur.fetchone()
        print("version      :", version)
        print("database     :", dbname)
        print("user         :", user)
        print("port         :", port)
        print("data_directory:", data_dir)
        print("search_path  :", sp)
        cur.execute("SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1,2")
        rows = cur.fetchall()
        print("-- public schema tables --")
        for r in rows:
            print(f"{r[0]}.{r[1]}")
        if not rows:
            print("(none)")
if __name__ == "__main__":
    main()
