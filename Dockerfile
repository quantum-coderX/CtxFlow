FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
# vite.config.ts writes to ../src/researchos/web
RUN mkdir -p /build/src/researchos/web && npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RESEARCHOS_HOST=0.0.0.0 \
    RESEARCHOS_PORT=8000 \
    RESEARCHOS_DATA_DIR=/data

# Set INSTALL_LAYA=false for a much smaller image (the heuristic router is used instead).
ARG INSTALL_LAYA=true
# Set INSTALL_LOCAL_EMBEDDINGS=true to bundle sentence-transformers for BGE embeddings.
ARG INSTALL_LOCAL_EMBEDDINGS=false

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY migrations ./migrations
COPY scripts ./scripts
COPY --from=frontend /build/src/researchos/web ./src/researchos/web

# CPU-only torch keeps the Laya extra from pulling CUDA wheels.
RUN if [ "$INSTALL_LAYA" = "true" ]; then \
        pip install torch --index-url https://download.pytorch.org/whl/cpu ; \
    fi \
    && EXTRAS="researchos,orchestration,mcp,web" \
    && if [ "$INSTALL_LAYA" = "true" ]; then EXTRAS="$EXTRAS,laya"; fi \
    && if [ "$INSTALL_LOCAL_EMBEDDINGS" = "true" ]; then EXTRAS="$EXTRAS,local-embeddings"; fi \
    && pip install ".[${EXTRAS}]"

# The MCP arXiv server runs through uvx.
RUN pip install uv

RUN useradd --create-home --uid 10001 researchos \
    && mkdir -p /data \
    && chown -R researchos:researchos /data /app
USER researchos
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["researchos-api"]
