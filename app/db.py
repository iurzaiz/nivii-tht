import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

DATABASE_URL = os.environ["DATABASE_URL"]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute_query(sql: str, row_limit: int = 500) -> list[dict]:
    """Execute a SELECT query and return results as a list of dicts."""
    with db_cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchmany(row_limit)
        return [dict(row) for row in rows]


def get_schema_with_samples() -> dict:
    """Return table schema with sample values per column and row count."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'sales'
            ORDER BY ordinal_position
        """)
        columns = cur.fetchall()

        schema: dict = {"columns": {}, "row_count": 0}

        for col in columns:
            col_name = col["column_name"]
            col_type = col["data_type"]

            cur.execute(
                f"SELECT DISTINCT {col_name}::text FROM sales "
                f"WHERE {col_name} IS NOT NULL ORDER BY 1 LIMIT 5"
            )
            samples = [row[col_name] for row in cur.fetchall()]

            schema["columns"][col_name] = {"type": col_type, "samples": samples}

        cur.execute("SELECT COUNT(*) AS count FROM sales")
        schema["row_count"] = cur.fetchone()["count"]

        return schema
