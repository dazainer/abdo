# Abdo: one image, two stages.
#   1. Node builds the React web app (web/ -> web/dist).
#   2. Python runs FastAPI, which serves the API, the Telegram webhook, and web/dist.
# Railway builds this automatically when it's present (replacing Nixpacks).

FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
# --ignore-scripts: the only install script is sharp's (icon rendering, a dev-time
# tool); the committed PNG icons mean the build never needs it.
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# ffmpeg: Telegram voice replies are transcoded MP3 -> OGG/Opus (see app/tts.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY scripts/ scripts/
COPY migrations/ migrations/
COPY schema.sql .
COPY --from=web /web/dist web/dist
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
