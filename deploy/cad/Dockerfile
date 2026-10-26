# Qualified smoke environment; immutable upstream base, locked Python wheel hashes.
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# The no-VTK wheel still links OCCT visualization libraries at import time.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=1 -o Acquire::https::Timeout=30 install -y --no-install-recommends libgl1 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY dist/deep3d_cad-0.2.0-py3-none-any.whl /tmp/wheels/
RUN python -m pip install --no-cache-dir --no-deps /tmp/wheels/*.whl
COPY pyproject.toml /app/pyproject.toml
COPY tests /app/tests
COPY specs /app/specs
COPY fixtures /app/fixtures
USER 10001:10001
CMD ["python", "-m", "deep3d_cad", "--help"]
