FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py db_migrate.py ./
COPY db-patches/ ./db-patches/
COPY scripts/deploy.py scripts/sqlite_db.py ./scripts/

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
