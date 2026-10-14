FROM python:3.11-slim AS detector

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV MPLBACKEND=Agg
ENV OMP_NUM_THREADS=2
ENV OPENBLAS_NUM_THREADS=2
ENV TORCH_HOME=/models/torch

ARG SECURITY_REFRESH=manual
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-beat-this.txt ./

RUN pip install --upgrade pip setuptools wheel \
    && pip install -r requirements.txt \
    && pip install "git+https://github.com/imcmurray/madmom-modern.git@df718c704a3b460ac6c61b7e61663a0436b91bda"

ARG WITH_BEAT_THIS=0
RUN if [ "$WITH_BEAT_THIS" = "1" ]; then \
        pip install --index-url https://download.pytorch.org/whl/cpu torch==2.8.0 torchaudio==2.8.0 \
        && pip install -r requirements-beat-this.txt; \
    fi

COPY app ./app

ENTRYPOINT ["python", "-m", "app.detect_bars"]

FROM detector AS worker
COPY worker/requirements.txt /tmp/worker-requirements.txt
RUN pip install -r /tmp/worker-requirements.txt \
    && python -m pip check \
    && python -m pip uninstall -y pip \
    && useradd --uid 1000 --create-home worker \
    && mkdir -p /data /models/torch \
    && chown -R worker:worker /data /models
COPY worker ./worker
USER worker
ENTRYPOINT ["python", "-m", "worker.main"]

# Preserve the original CLI build and entrypoint for build.sh/start.sh.
FROM detector AS cli
RUN python -m pip check && python -m pip uninstall -y pip
