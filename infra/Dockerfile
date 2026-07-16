FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY apps/api/src ./apps/api/src
RUN python -m pip install "uv==0.11.21" \
    && uv export --locked --no-dev --no-emit-project --output-file /tmp/requirements.lock \
    && python -m pip wheel --wheel-dir /wheels --requirement /tmp/requirements.lock \
    && python -m pip wheel --wheel-dir /wheels --no-deps .

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN addgroup --system contentai && adduser --system --ingroup contentai contentai
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels contentai \
    && rm -rf /wheels
USER contentai
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000"]
