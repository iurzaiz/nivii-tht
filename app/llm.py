import json
import logging
import os
import re
from datetime import date
from typing import AsyncIterator

import httpx
import sqlparse

logger = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL_NAME = os.environ.get("MODEL_NAME", "qwen2.5-coder:7b")

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

SYSTEM_SQL = (
    "You are an expert PostgreSQL query generator. "
    "Your ONLY output is a single valid SQL SELECT query — "
    "no markdown, no explanation, no comments. "
    "Never use INSERT, UPDATE, DELETE, DROP, or ALTER. "
    "Always end the query with a semicolon."
)

SYSTEM_ANSWER_EN = (
    "You are a concise data analyst. "
    "Given a question and query results, write ONE clear sentence that directly answers the question."
)
SYSTEM_ANSWER_ES = (
    "Sos un analista de datos conciso. "
    "Dada una pregunta y los resultados de una consulta, escribí UNA oración clara que responda directamente la pregunta. "
    "Respondé siempre en español."
)

FEW_SHOT = [
    (
        "What is the most sold product?",
        "SELECT product_name, SUM(quantity) AS total_sold "
        "FROM sales GROUP BY product_name ORDER BY total_sold DESC LIMIT 1;",
    ),
    (
        "What are total sales by day of the week?",
        "SELECT week_day, SUM(total) AS revenue "
        "FROM sales GROUP BY week_day ORDER BY revenue DESC;",
    ),
    (
        "How many unique tickets were there?",
        "SELECT COUNT(DISTINCT ticket_number) AS unique_tickets FROM sales;",
    ),
    (
        "What is the average sale amount per day of the week?",
        "SELECT week_day, ROUND(AVG(total)::numeric, 2) AS avg_total "
        "FROM sales GROUP BY week_day ORDER BY avg_total DESC;",
    ),
    (
        "How do sales vary by hour of the day?",
        "SELECT EXTRACT(HOUR FROM hour)::int AS hour_of_day, SUM(total) AS total_sales "
        "FROM sales GROUP BY hour_of_day ORDER BY hour_of_day;",
    ),
]


def _build_sql_prompt(question: str, schema_str: str, failed_sql: str = None, error: str = None) -> str:
    today = date.today()
    date_ctx = (
        f"Today's date: {today.isoformat()} "
        f"({today.strftime('%A, %B %d, %Y')}). "
        "Use this ONLY when the question explicitly mentions relative time expressions like "
        "'last month', 'this week', 'yesterday', 'today'. "
        "Do NOT add any date filter unless the question explicitly requests a specific time period."
    )
    parts = [date_ctx, schema_str, "\nExamples:"]
    for q, sql in FEW_SHOT:
        parts.append(f"Q: {q}\nSQL: {sql}")

    if failed_sql and error:
        parts.append(
            f"\nThe previous attempt produced invalid SQL.\n"
            f"Failed SQL: {failed_sql}\n"
            f"Error: {error}\n"
            f"Generate a corrected query."
        )

    parts.append(f"\nQ: {question}\nSQL:")
    return "\n".join(parts)


def _extract_sql(raw: str) -> str:
    """Strip markdown fences and return the first SQL statement."""
    raw = re.sub(r"```sql\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"```\s*", "", raw)
    raw = raw.strip()
    if not raw.endswith(";"):
        raw += ";"
    return raw


_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|MERGE|GRANT|REVOKE|EXECUTE|EXEC)\b",
    re.IGNORECASE,
)


def _validate_sql(sql: str) -> str | None:
    """Return an error string if the SQL is invalid or unsafe, else None.

    Two independent layers:
    1. sqlparse — checks the parsed statement type is SELECT.
    2. Keyword blocklist — catches dangerous verbs even if sqlparse
       fails to determine the type (e.g. ambiguous or multi-statement input).
    """
    # Layer 1: keyword blocklist on raw text
    match = _FORBIDDEN_KEYWORDS.search(sql)
    if match:
        return f"Forbidden keyword '{match.group().upper()}': only SELECT queries are allowed."

    # Layer 2: sqlparse structural check
    try:
        statements = [s for s in sqlparse.parse(sql) if s.tokens]
        if not statements:
            return "Empty or unparseable SQL."
        if len(statements) > 1:
            return "Multiple statements are not allowed."
        stmt_type = statements[0].get_type()
        if stmt_type is None:
            return "Could not determine statement type; only SELECT queries are allowed."
        if stmt_type.upper() != "SELECT":
            return f"Only SELECT queries are allowed, got: {stmt_type}."
        return None
    except Exception as exc:
        return str(exc)


# ---------------------------------------------------------------------------
# Ollama helpers
# ---------------------------------------------------------------------------

async def _chat(system: str, user: str, temperature: float = 0.0) -> str:
    """Send a non-streaming chat request and return the model's text response."""
    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL_NAME,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": temperature, "stop": ["\n\n", "Q:", "Question:"]},
            },
        )
        response.raise_for_status()
        return response.json()["message"]["content"]


async def pull_model() -> None:
    """Pull MODEL_NAME from Ollama if it is not already cached."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{OLLAMA_URL}/api/tags")
        cached = [m["name"] for m in resp.json().get("models", [])]

    if any(MODEL_NAME in name for name in cached):
        logger.info("Model %s already cached.", MODEL_NAME)
        return

    logger.info("Pulling model %s — this may take several minutes on first run...", MODEL_NAME)
    async with httpx.AsyncClient(timeout=1800.0) as client:
        async with client.stream("POST", f"{OLLAMA_URL}/api/pull", json={"name": MODEL_NAME}) as resp:
            async for line in resp.aiter_lines():
                if line:
                    try:
                        data = json.loads(line)
                        status = data.get("status", "")
                        completed = data.get("completed")
                        total = data.get("total")
                        if completed and total:
                            pct = round(completed / total * 100)
                            logger.info("Pulling %s: %d%%", MODEL_NAME, pct)
                        elif status:
                            logger.info("Pull status: %s", status)
                    except json.JSONDecodeError:
                        pass
    logger.info("Model %s ready.", MODEL_NAME)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def generate_sql(
    question: str,
    schema_str: str,
    failed_sql: str = None,
    db_error: str = None,
    max_retries: int = 2,
) -> tuple[str, int]:
    """
    Generate a SQL SELECT query from a natural language question.
    Implements a self-correction loop: if validation fails, the error is
    fed back to the model for up to max_retries attempts.
    Returns (sql, attempts_used).
    """
    failed = failed_sql
    error = db_error
    last_sql = ""

    for attempt in range(1, max_retries + 1):
        prompt = _build_sql_prompt(question, schema_str, failed, error)
        raw = await _chat(SYSTEM_SQL, prompt, temperature=0.0)
        sql = _extract_sql(raw)
        last_sql = sql

        validation_error = _validate_sql(sql)
        if validation_error is None:
            logger.info("SQL generated in %d attempt(s).", attempt)
            return sql, attempt

        logger.warning("Attempt %d validation error: %s", attempt, validation_error)
        failed = sql
        error = validation_error

    logger.warning("Returning best-effort SQL after %d attempts.", max_retries)
    return last_sql, max_retries


async def stream_answer(
    question: str, sql: str, rows: list[dict], lang: str = "es"
) -> AsyncIterator[str]:
    """Stream a natural language answer token by token."""
    system = SYSTEM_ANSWER_ES if lang == "es" else SYSTEM_ANSWER_EN
    results_preview = str(rows[:15])
    instruction = "Escribí una oración clara que responda directamente la pregunta." if lang == "es" \
        else "Write one clear sentence that directly answers the question."
    user_prompt = (
        f'Question: "{question}"\n'
        f'SQL used: {sql}\n'
        f"Results: {results_preview}\n\n"
        f"{instruction}"
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL_NAME,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": True,
                "options": {"temperature": 0.3},
            },
        ) as resp:
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    token = data.get("message", {}).get("content", "")
                    if token:
                        yield token
                    if data.get("done"):
                        break
                except json.JSONDecodeError:
                    continue
