# Сборка идёт в два этапа: фронтенд мини-аппа собирается node-ом и попадает в
# питоновский образ уже статикой. Node в рантайме не нужен и в образ не едет.
FROM node:22-alpine AS miniapp

WORKDIR /build
# Сначала манифесты — слой с npm ci переиспользуется, пока зависимости не менялись.
COPY web/miniapp/package.json web/miniapp/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/miniapp/ ./
# build = tsc --noEmit && vite build: несобирающийся TypeScript валит образ здесь,
# а не всплывает белым экраном внутри Telegram.
RUN npm run build


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY src ./src
COPY --from=miniapp /build/dist ./web/miniapp

ENV PYTHONPATH=/app/src

RUN useradd -m -u 10001 app && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "b24bot.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
