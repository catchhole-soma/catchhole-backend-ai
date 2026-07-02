# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system catchhole \
    && useradd --system --gid catchhole --uid 10001 --create-home catchhole

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install .

USER catchhole:catchhole

CMD ["python", "scripts/run_analysis_worker.py"]
