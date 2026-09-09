FROM python:3.12.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY limiter/pyproject.toml /app/limiter/pyproject.toml
COPY limiter/src /app/limiter/src
RUN pip install --no-cache-dir /app/limiter
COPY limiter/static /app/limiter/static
COPY limiter/templates /app/limiter/templates
COPY config /app/config
COPY sql /app/sql
COPY scripts /app/scripts
ENV LIMITER_WEB_ROOT=/app/limiter LIMITER_CONFIG=/app/config/limits.yaml
RUN useradd --uid 10001 --create-home demo
USER demo
EXPOSE 8080
CMD ["uvicorn", "ratelimit_demo.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
