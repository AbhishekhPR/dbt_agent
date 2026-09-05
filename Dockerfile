FROM python:3.10-slim-bookworm AS collector-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /collector-source

# Build the customer artifact once. The checksum and ZIP are made from the
# wheel produced by this stage; the runtime stage only copies the final bytes.
COPY pyproject.toml README.md ./
COPY agent ./agent
RUN python -m pip install "build==1.2.2.post1" "setuptools==80.9.0" "wheel==0.45.1" \
    && python -m build --wheel --no-isolation --outdir /collector-dist \
    && test "$(find /collector-dist -maxdepth 1 -name '*.whl' | wc -l)" -eq 1 \
    && test -f /collector-dist/relium-0.1.0-py3-none-any.whl \
    && cd /collector-dist \
    && sha256sum relium-0.1.0-py3-none-any.whl > SHA256SUMS \
    && sha256sum --check SHA256SUMS \
    && mkdir -p /collector-package \
    && python -m zipfile -c /collector-package/relium-collector-0.1.0.zip relium-0.1.0-py3-none-any.whl SHA256SUMS

FROM python:3.10-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Use the same hash-locked dependency contract as the existing production
# deployment. The application runs from /app, so no editable install or
# unpinned build dependency is needed in the image.
COPY requirements.lock ./
RUN python -m pip install --require-hashes -r requirements.lock

RUN addgroup --system relium \
    && adduser --system --ingroup relium --home /home/relium relium \
    && mkdir -p /data/relium \
    && chown -R relium:relium /data/relium /home/relium

COPY --chown=relium:relium agent ./agent
COPY --from=collector-builder --chown=relium:relium \
    /collector-package/relium-collector-0.1.0.zip \
    /app/artifacts/relium-collector-0.1.0.zip

USER relium

EXPOSE 8000

CMD ["python", "-m", "agent.github_app.server"]
