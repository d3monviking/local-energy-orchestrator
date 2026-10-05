# Build context is the repo root; see docker-compose.yml.
# cloud/training.py imports gateway.forecast.*, so the gateway package
# (and its dependencies, hence the shared root requirements.txt) is
# needed here too.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt requirements.txt
COPY cloud/requirements.txt cloud/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt -r cloud/requirements.txt

COPY contracts/ contracts/
COPY gateway/ gateway/
COPY cloud/ cloud/

ENV PYTHONPATH=/app
CMD ["uvicorn", "cloud.api:app", "--host", "0.0.0.0", "--port", "8030"]
