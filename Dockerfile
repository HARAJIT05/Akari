# ── Base ──────────────────────────────────────────────
FROM python:3.12-slim AS base

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create runtime directories
RUN mkdir -p data/downloads data/logs

# ── Bot ───────────────────────────────────────────────
FROM base AS bot

ENV CONFIG_PATH=/app/config.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

CMD ["python", "src/main.py"]

# ── Dashboard ─────────────────────────────────────────
FROM base AS dashboard

ENV CONFIG_PATH=/app/config.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

EXPOSE 5000

CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "5000"]
