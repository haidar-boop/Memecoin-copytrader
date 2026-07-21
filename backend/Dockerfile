FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/backend

COPY pyproject.toml ./
# Install deps first for layer caching; source is copied afterwards.
RUN pip install --no-cache-dir hatchling && \
    python - <<'EOF'
import tomllib, subprocess
with open("pyproject.toml", "rb") as f:
    deps = tomllib.load(f)["project"]["dependencies"]
subprocess.check_call(["pip", "install", "--no-cache-dir", *deps])
EOF

COPY app ./app
COPY workers ./workers
COPY telegram_bot ./telegram_bot
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8000 9100

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
