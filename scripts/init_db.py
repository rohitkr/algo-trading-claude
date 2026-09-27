"""Create (or upgrade) the DuckDB database and print its tables.

    python3 scripts/init_db.py
"""
import sys

import _path  # noqa: F401

from trading_data.app import bootstrap


def main() -> int:
    settings, store = bootstrap("init_db")
    print(f"DuckDB database: {settings.paths.database}")
    tables = store.query_df("SELECT table_name, table_type FROM information_schema.tables "
                            "WHERE table_schema = 'main' ORDER BY table_type, table_name")
    for r in tables.itertuples():
        n = store.query_df(f'SELECT count(*) AS n FROM "{r.table_name}"')["n"].iloc[0]
        print(f"  {r.table_type:<10} {r.table_name:<32} {n:>12,} rows")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
