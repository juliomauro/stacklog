FROM python:3.12-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static ./static

ENV DB_PATH=/data/stacklog.db
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/', timeout=2)" || exit 1

CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "server:app"]
