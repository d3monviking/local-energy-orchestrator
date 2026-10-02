# Build context is the repo root (leo/); see docker-compose.yml.
FROM python:3.12-slim

WORKDIR /app
COPY mocks/requirements.txt mocks/requirements.txt
RUN pip install --no-cache-dir -r mocks/requirements.txt

COPY mocks/ mocks/
COPY sim/__init__.py sim/__init__.py
COPY sim/clock_client.py sim/clock_client.py
COPY contracts/ contracts/

ENV PYTHONPATH=/app:/app/mocks
WORKDIR /app/mocks

# Actual entry point (ies_server / modbus_battery / modbus_meters / sms_service)
# is chosen per-service by docker-compose.yml's `command:`.
CMD ["uvicorn", "ies_server:app", "--host", "0.0.0.0", "--port", "8011"]
