# StockFlow container for Google Cloud Run
FROM python:3.12-slim

# Don't write .pyc files; print logs immediately (Cloud Run reads stdout)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production

WORKDIR /app

# Install dependencies first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY . .

# Run as a non-root user for safety
RUN useradd --create-home appuser
USER appuser

# Cloud Run tells the container which port to listen on via $PORT (default 8080).
# SECRET_KEY and DATABASE_URL are provided as Cloud Run environment variables/secrets.
CMD exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 2 --threads 4 --timeout 60 "app:create_app()"
