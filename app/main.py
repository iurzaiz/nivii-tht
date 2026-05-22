import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import execute_query, get_schema_with_samples
from llm import generate_sql, pull_model, stream_answer, _validate_sql
from loader import load_csv_if_needed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]
CSV_PATH = os.environ.get("CSV_PATH", "/app/data.csv")

schema_cache: dict = {}


def _build_schema_str(schema: dict) -> str:
    lines = [f"Table: sales  ({schema['row_count']:,} rows)", "Columns:"]
    for col_name, info in schema["columns"].items():
        samples = ", ".join(str(s) for s in info["samples"][:3])
        lines.append(f"  - {col_name} ({info['type']}): e.g. {samples}")
    return "\n".join(lines)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pull_model()
    load_csv_if_needed(DATABASE_URL, CSV_PATH)
    schema_cache.update(get_schema_with_samples())
    logger.info("Application ready.")
    yield


app = FastAPI(title="Nivii.ai x Imanol Urzaiz", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/schema")
async def schema():
    return schema_cache


class QueryRequest(BaseModel):
    question: str


@app.post("/query")
async def query(req: QueryRequest):
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    schema_str = _build_schema_str(schema_cache)

    # 1. Generate SQL
    t0 = time.perf_counter()
    sql, attempts = await generate_sql(question, schema_str)
    sql_ms = round((time.perf_counter() - t0) * 1000)

    # Safety gate: reject non-SELECT even after generation
    err = _validate_sql(sql)
    if err:
        raise HTTPException(status_code=422, detail=f"Could not generate valid SQL: {err}")

    # 2. Execute SQL — retry once with DB error context
    t1 = time.perf_counter()
    try:
        rows = execute_query(sql)
    except Exception as db_err:
        logger.warning("DB error on first attempt, retrying with error context: %s", db_err)
        sql, extra = await generate_sql(question, schema_str, failed_sql=sql, db_error=str(db_err))
        attempts += extra
        try:
            rows = execute_query(sql)
        except Exception as db_err2:
            raise HTTPException(status_code=500, detail=f"SQL execution failed: {db_err2}")
    exec_ms = round((time.perf_counter() - t1) * 1000)

    return {
        "sql": sql,
        "rows": rows,
        "row_count": len(rows),
        "attempts": attempts,
        "timing": {"sql_generation_ms": sql_ms, "execution_ms": exec_ms},
    }


class AnswerRequest(BaseModel):
    question: str
    sql: str
    rows: list[dict]
    lang: str = "es"


@app.post("/answer/stream")
async def answer_stream(req: AnswerRequest):
    """Stream the natural language answer as Server-Sent Events."""
    async def event_gen():
        try:
            async for token in stream_answer(req.question, req.sql, req.rows, req.lang):
                yield f"data: {json.dumps({'token': token})}\n\n"
        except Exception as exc:
            logger.error("Answer streaming error: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
