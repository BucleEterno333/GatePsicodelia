# ============================================================
# UNICEF AU Checker — Dockerfile (Python + Playwright)
# ============================================================
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Dependencias Python primero (mejor caché)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Código
COPY . .

# Vars por defecto
ENV PORT=8080 \
    HEADLESS=true \
    FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# Arranque con Gunicorn en modo gthread (necesario para que sobreviva
# el threading.Thread del batch y Playwright no muera entre workers)
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} \
  --workers 1 \
  --threads 8 \
  --worker-class gthread \
  --timeout 1800 \
  --graceful-timeout 120 \
  --access-logfile - \
  --error-logfile - \
  server:app"]