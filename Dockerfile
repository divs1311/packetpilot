FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN pip install --no-deps -e .

EXPOSE 8000
# API by default; docker-compose overrides the command for the worker and UI.
CMD ["sh", "-c", "uvicorn packetpilot.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
