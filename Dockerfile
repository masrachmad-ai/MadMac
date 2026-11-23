FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY madmac ./madmac
COPY tests ./tests
COPY config.yaml .
ENV MADMAC_DATA_DIR=/app/data PYTHONUNBUFFERED=1 TZ=UTC
# Bot is "healthy" only if its main loop wrote a heartbeat in the last 2 minutes.
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import time,sys;sys.exit(0 if time.time()-int(open('/app/data/heartbeat').read())<120 else 1)"
CMD ["python", "-m", "madmac", "run"]
