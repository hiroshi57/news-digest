FROM python:3.12-slim

# システムパッケージ（dnspython の C 拡張は不要だが念のため）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依存インストール（コードより先にキャッシュを効かせる）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリコードをコピー
COPY app/ ./app/

# Python パスを通す
ENV PYTHONPATH=/app

# Cloud Run は PORT 環境変数で port を渡す（デフォルト 8080）
ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
