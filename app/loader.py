import logging
import os

import pandas as pd
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sales (
    id            SERIAL PRIMARY KEY,
    date          DATE,
    week_day      VARCHAR(20),
    hour          TIME,
    ticket_number VARCHAR(60),
    waiter        INTEGER,
    product_name  VARCHAR(255),
    quantity      INTEGER,
    unitary_price NUMERIC(14, 2),
    total         NUMERIC(14, 2)
);
CREATE INDEX IF NOT EXISTS idx_sales_week_day     ON sales (week_day);
CREATE INDEX IF NOT EXISTS idx_sales_date         ON sales (date);
CREATE INDEX IF NOT EXISTS idx_sales_product_name ON sales (product_name);
"""

COLUMNS = [
    "date", "week_day", "hour", "ticket_number",
    "waiter", "product_name", "quantity", "unitary_price", "total",
]


def load_csv_if_needed(database_url: str, csv_path: str) -> None:
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE_SQL)
            conn.commit()

            cur.execute("SELECT COUNT(*) FROM sales")
            count = cur.fetchone()[0]

        if count > 0:
            logger.info("Database already populated (%d rows), skipping CSV load.", count)
            return

        logger.info("Loading %s into database...", csv_path)
        df = pd.read_csv(csv_path)

        df["date"] = pd.to_datetime(df["date"], dayfirst=False).dt.date
        df["hour"] = pd.to_datetime(df["hour"], format="%H:%M").dt.time
        df["waiter"] = pd.to_numeric(df["waiter"], errors="coerce").fillna(0).astype(int)
        df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0).astype(int)
        df["unitary_price"] = pd.to_numeric(df["unitary_price"], errors="coerce").fillna(0)
        df["total"] = pd.to_numeric(df["total"], errors="coerce").fillna(0)

        records = df[COLUMNS].values.tolist()

        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO sales ({', '.join(COLUMNS)}) VALUES %s",
                records,
                page_size=1000,
            )
        conn.commit()
        logger.info("Loaded %d rows into sales table.", len(records))

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
