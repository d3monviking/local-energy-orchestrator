# Build context is the repo root (leo/); see docker-compose.yml.
FROM python:3.12-slim

WORKDIR /app
COPY sim/requirements.txt sim/requirements.txt
RUN pip install --no-cache-dir -r sim/requirements.txt

COPY sim/ sim/

ENV PYTHONPATH=/app:/app/sim
WORKDIR /app/sim

CMD ["uvicorn", "clock:app", "--host", "0.0.0.0", "--port", "8010"]
