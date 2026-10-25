FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tokyo

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 alerts \
    && mkdir -p /data /logs \
    && chown -R alerts:alerts /data /logs

USER alerts

ENV PYTHONPATH=/app/src
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
    CMD ["python", "scripts/healthcheck.py"]

CMD ["python", "-m", "radio_alerts_backend"]
