FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data ./data
ENV DB_PATH=/app/var/sop_index.sqlite3
EXPOSE 8000
# Index the mounted/bundled library on start (idempotent: a no-op if unchanged), then serve.
CMD ["sh", "-c", "python -m app.cli ingest ${SNAPSHOT_DIR:-data/sops}; exec uvicorn app.api.main:app --host 0.0.0.0 --port 8000"]
