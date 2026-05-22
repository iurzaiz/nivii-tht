# Nivii.ai × Imanol Urzaiz — SQL Query Assistant

Ask questions about your sales data in plain language (Spanish or English). The system translates them to SQL, runs them against a PostgreSQL database, and returns the results alongside a natural language answer in the same language you asked in.

---

## Architecture

```
Browser
  └─▶ FastAPI app (port 8000)
        ├─▶ Ollama  (text-to-SQL + answer generation)
        └─▶ PostgreSQL  (sales data from data.csv)
```

Three containers, each with a single responsibility. The FastAPI app is the only public-facing service.

---

## Quick start

```bash
# 1. Clone / download the repo (data.csv must be at the project root)

# 2. (Optional) customise the model
cp .env.example .env
# edit .env if needed

# 3. Build and run
docker compose up --build
```

**The first run takes several minutes** while the model is downloaded (~4 GB for the default 7B model). The model is cached in a Docker volume, so subsequent starts are instant.

Once you see `Application ready.` in the logs, open **http://localhost:8000**.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MODEL_NAME` | `qwen2.5-coder:7b` | Ollama model tag. Use `qwen2.5-coder:3b` for faster (but lower quality) inference on low-RAM machines. |

---

## Performance notes by platform

| Platform | Inference speed |
|---|---|
| NVIDIA GPU | Fast (seconds per query) |
| Apple Silicon (native Ollama) | Fast via Metal |
| Apple Silicon (Docker) | **Slow** — Docker on Mac lacks Metal access; Ollama falls back to CPU |
| CPU-only Linux/Windows | Slow — plan for 1–3 min per query on the 7B model; use the 3B model if needed |

### Faster option for Apple Silicon

Run Ollama natively (outside Docker) for Metal acceleration:

```bash
# Install Ollama: https://ollama.com
ollama pull qwen2.5-coder:7b

# Point the app container to your local Ollama
OLLAMA_URL=http://host.docker.internal:11434 docker compose up --build app db
```

---

## API reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/schema` | Table schema with sample values |
| `POST` | `/query` | Generate SQL and execute it |
| `POST` | `/answer/stream` | Stream a natural language answer (SSE) |
| `GET` | `/health` | Health check |

### POST /query

```json
{ "question": "What is the most bought product on Fridays?" }
```

Response:

```json
{
  "sql": "SELECT product_name, SUM(quantity) AS total_sold FROM sales WHERE week_day = 'Friday' GROUP BY product_name ORDER BY total_sold DESC LIMIT 1;",
  "rows": [{ "product_name": "Alfajor 70 cacao x un", "total_sold": 312 }],
  "row_count": 1,
  "attempts": 1,
  "timing": { "sql_generation_ms": 4210, "execution_ms": 18 }
}
```

### POST /answer/stream

```json
{ "question": "...", "sql": "...", "rows": [...], "lang": "es" }
```

Returns a stream of Server-Sent Events with answer tokens. `lang` accepts `"es"` or `"en"`.

---

## Scaling

See [CONSIDERATIONS.md — Escalabilidad](CONSIDERATIONS.md#escalabilidad) for a detailed breakdown.

---

## Development (without Docker)

```bash
cd app
pip install -r requirements.txt

# Requires a running PostgreSQL and Ollama instance
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/sales_db
export OLLAMA_URL=http://localhost:11434
export MODEL_NAME=qwen2.5-coder:7b

uvicorn main:app --reload
```
