FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:${PATH}" \
    UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir uv==0.13.7 \
    && uv sync --frozen --extra deployment --no-dev \
    && groupadd --gid 1000 taip \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin taip

USER 1000:1000
EXPOSE 8000
CMD ["uvicorn", "taip.deployment.gateway:create_gateway_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--no-proxy-headers"]
