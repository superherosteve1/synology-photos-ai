FROM python:3.11-slim-bookworm

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STATE_PATH=/data/processed.db

COPY pyproject.toml README.md ./
COPY src ./src

# NAS Container Manager builds often stall or fail on PEP 517 build isolation
# ("Installing build dependencies…") when fetching hatchling from PyPI.
# Pre-install hatchling and runtime deps, then install the package without isolation.
RUN pip install --upgrade pip \
    && pip install hatchling \
        httpx \
        langchain-core \
        langchain-openai \
        Pillow \
        pydantic \
        pydantic-settings \
        typer \
        rich \
    && pip install --no-build-isolation --no-deps .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

USER appuser

VOLUME ["/data"]

ENTRYPOINT ["synology-photos-ai"]
CMD ["watch"]
