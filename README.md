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
git clone https://github.com/iurzaiz/nivii-tht.git

# 2. (Optional) customise the model
cp .env.example .env
# edit .env if needed

# 3. Build and run
docker compose up --build
```

**The first run takes several minutes** while the model is downloaded (~4 GB for the default 7B model). The model is cached in a Docker volume, so subsequent starts are instant.
Once you see `Application ready.` in the logs, open **http://localhost:8000**.

API docs: **http://localhost:8000/docs**

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

## Scaling & architecture decisions

See [CONSIDERATIONS.md](CONSIDERATIONS.md) for a detailed breakdown.

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
