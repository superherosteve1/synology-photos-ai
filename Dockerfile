FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STATE_PATH=/data/processed.db

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

USER appuser

VOLUME ["/data"]

ENTRYPOINT ["synology-photos-ai"]
CMD ["watch"]
