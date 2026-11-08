FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# application code (vendor/selfcheck = rules engine synced from flywheel, see README)
COPY main.py jobs.py mailer.py selfcheck_api.py ./
COPY vendor/ ./vendor/

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
