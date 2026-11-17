FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    UPLOAD_DIR=/data/uploads \
    DEMO_DB=/app/demo/demo_sensors.db \
    PORT=8000 \
    QUERY_TIMEOUT=3 \
    MAX_CONCURRENT=2 \
    MAX_ROWS=1000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY webapp/ ./webapp/
COPY demo/make_demo_db.py demo/find_duplicates.sql ./demo/

# 构建期生成传感器演示库，并准备上传目录（非 root 运行）
RUN python demo/make_demo_db.py /app/demo/demo_sensors.db \
    && mkdir -p /data/uploads \
    && useradd --system --uid 10001 appuser \
    && chown -R appuser:appuser /data

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

# 单 worker 多线程：查询名额在进程内用信号量控制，必须保持单 worker
CMD ["gunicorn", "--pythonpath", "/app/webapp", "--bind", "0.0.0.0:8000", \
     "--workers", "1", "--threads", "16", "--timeout", "30", "app:app"]
