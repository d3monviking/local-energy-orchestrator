# Build context is the repo root; see docker-compose.yml.
# gateway/ imports absolute (`from gateway.network_model import ...`), so
# the repo root stays on PYTHONPATH rather than cd'ing into gateway/.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt requirements.txt
COPY gateway/requirements.txt gateway/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r gateway/requirements.txt

COPY contracts/ contracts/
COPY gateway/ gateway/

ENV PYTHONPATH=/app
CMD ["uvicorn", "gateway.ingest:app", "--host", "0.0.0.0", "--port", "8020"]
