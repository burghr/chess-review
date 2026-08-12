FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Debian ships Stockfish at /usr/games/stockfish. curl is only here for the
# healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends stockfish curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY chessreview ./chessreview

RUN useradd --create-home --uid 1000 chess \
    && mkdir -p /data \
    && chown -R chess:chess /data /app
USER chess

ENV CHESS_REVIEW_DB=/data/chess.db \
    CHESS_REVIEW_ENGINE=/usr/games/stockfish \
    CHESS_REVIEW_DEPTH=14 \
    CHESS_REVIEW_THREADS=2 \
    CHESS_REVIEW_HASH=256 \
    CHESS_REVIEW_BATCH=200 \
    CHESS_REVIEW_INTERVAL=60 \
    CHESS_REVIEW_AUTO_SYNC=1 \
    CHESS_REVIEW_SYNC_ON_START=0 \
    CHESS_REVIEW_LLM_PROVIDER="" \
    CHESS_REVIEW_LLM_MODEL="" \
    CHESS_REVIEW_LLM_BASE_URL=""

EXPOSE 8765
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8765/api/health || exit 1

CMD ["uvicorn", "chessreview.web.app:app", "--host", "0.0.0.0", "--port", "8765"]
