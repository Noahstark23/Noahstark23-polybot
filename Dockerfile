# ---- build stage ----
FROM python:3.12-slim AS builder
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# ---- runtime stage ----
FROM python:3.12-slim AS runtime
RUN groupadd -r botuser && useradd -r -g botuser botuser \
    && apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chown=botuser:botuser src/ ./src/
COPY --chown=botuser:botuser scripts/ ./scripts/
RUN mkdir -p /app/data /app/logs /app/secrets && chown -R botuser:botuser /app
USER botuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1
CMD ["python", "-m", "src.runner"]

# Nota: a diferencia del bot Kalshi, este Dockerfile NO hace COPY config/ —
# las llaves van solo por secret volume (corrige el hallazgo del .pem commiteado).
