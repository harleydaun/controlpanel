FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ipmitool curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# Vendor Chart.js so the UI works with no internet access
RUN curl -fsSL https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.js \
        -o /app/static/chart.umd.js

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
    CMD curl -fsS http://localhost:8000/api/status >/dev/null || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
