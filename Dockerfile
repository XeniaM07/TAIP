FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev

EXPOSE 8000
CMD ["uvicorn", "taip.application.api:app", "--host", "0.0.0.0", "--port", "8000"]
