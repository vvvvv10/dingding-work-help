FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    KB_DB_PATH=/data/kb.sqlite3

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

VOLUME ["/data"]
EXPOSE 8000

CMD ["python", "-m", "app.main", "serve", "--host", "0.0.0.0", "--port", "8000"]
