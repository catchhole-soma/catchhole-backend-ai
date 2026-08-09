# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache

WORKDIR /app

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system catchhole \
    && useradd --system --gid catchhole --uid 10001 --create-home catchhole

COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install . \
    && mkdir -p "${TIKTOKEN_CACHE_DIR}" \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
    && chmod -R a+rX "${TIKTOKEN_CACHE_DIR}" \
    && cd /tmp \
    && python -c "from pathlib import Path; import app; prompt_dir = Path(app.__file__).parent / 'llm' / 'prompts'; required = ('character_setting_extraction.md', 'character_subject_resolution.md', 'world_setting_extraction.md', 'world_setting_subject_resolution.md', 'world_setting_comparison.md'); missing = [name for name in required if not (prompt_dir / name).is_file()]; assert not missing, f'missing packaged prompts: {missing}'"

USER catchhole:catchhole

CMD ["python", "scripts/run_analysis_worker.py"]
