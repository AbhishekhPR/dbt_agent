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

USER relium

EXPOSE 8000

CMD ["python", "-m", "agent.github_app.server"]
