# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.11
ARG DENO_VERSION=2.9.2
ARG DENO_SHA256=934d1bd5cb09eaed7f2e4a4fc58208d04a3c5c0fcde9f319d93d735265c67a4a
ARG SONIC_ANNOTATOR_VERSION=1.7
ARG SONIC_ANNOTATOR_SHA256=ec7838368aa6b20a039d04ebd5d91f4efd26e9c79f713b70843e35880151b919
ARG CHORDINO_COMMIT=59f683ebb479c510b6b1a819ead3483778d72d4b
ARG WHISPER_MODEL_REPOSITORY=Systran/faster-whisper-medium
ARG WHISPER_MODEL_REVISION=08e178d48790749d25932bbc082711ddcfdfbc4f

FROM debian:bookworm-slim AS native-tools
ARG DENO_VERSION
ARG DENO_SHA256
ARG SONIC_ANNOTATOR_VERSION
ARG SONIC_ANNOTATOR_SHA256
ARG CHORDINO_COMMIT
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl git gfortran libboost-iostreams-dev \
      libvamp-sdk2v5 unzip vamp-plugin-sdk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN curl -fsSLO "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-x86_64-unknown-linux-gnu.zip" \
    && echo "${DENO_SHA256}  deno-x86_64-unknown-linux-gnu.zip" | sha256sum -c - \
    && unzip -q deno-x86_64-unknown-linux-gnu.zip -d /opt/deno \
    && chmod 0755 /opt/deno/deno

RUN curl -fsSL -o sonic-annotator.tar.gz \
      "https://github.com/sonic-visualiser/sonic-annotator/releases/download/sonic-annotator-${SONIC_ANNOTATOR_VERSION}/sonic-annotator-${SONIC_ANNOTATOR_VERSION}.0-linux64-static.tar.gz" \
    && echo "${SONIC_ANNOTATOR_SHA256}  sonic-annotator.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/sonic-annotator \
    && tar -xzf sonic-annotator.tar.gz -C /opt/sonic-annotator --strip-components=1 \
    && cd /opt/sonic-annotator \
    && ./sonic-annotator --appimage-extract >/dev/null \
    && rm sonic-annotator \
    && mv squashfs-root /opt/sonic-annotator-appdir \
    && cp /opt/sonic-annotator-appdir/AppRun /opt/sonic-annotator-appdir/sonic-annotator \
    && chmod -R a+rX /opt/sonic-annotator-appdir \
    && chmod 0755 /opt/sonic-annotator-appdir/sonic-annotator

RUN git clone https://github.com/c4dm/nnls-chroma.git /build/nnls-chroma \
    && cd /build/nnls-chroma \
    && git checkout "${CHORDINO_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${CHORDINO_COMMIT}" \
    && make -f Makefile.linux VAMP_SDK_DIR=/usr/lib/x86_64-linux-gnu \
    && mkdir -p /opt/vamp \
    && cp nnls-chroma.so nnls-chroma.cat nnls-chroma.n3 /opt/vamp/

FROM python:${PYTHON_VERSION}-slim-bookworm AS app-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_HOME=/app
WORKDIR /app
COPY pyproject.toml README.md THIRD_PARTY_NOTICES.md ./
COPY src ./src

FROM app-base AS web
RUN pip install --no-cache-dir ".[web]"
COPY streamlit_app.py ./streamlit_app.py
COPY .streamlit ./.streamlit
RUN mkdir -p /app/data && chown -R nobody:nogroup /app
USER nobody
EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]

FROM app-base AS worker-base
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates ffmpeg libgfortran5 libgomp1 libsndfile1 libvamp-sdk2v5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=native-tools /opt/deno/deno /usr/local/bin/deno
COPY --from=native-tools /opt/sonic-annotator-appdir /opt/sonic-annotator
COPY --from=native-tools /opt/vamp /opt/vamp
ENV PATH=/opt/sonic-annotator:${PATH} \
    VAMP_PATH=/opt/vamp \
    TORCH_HOME=/opt/demucs-models \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    NUMBA_CACHE_DIR=/tmp/numba-cache \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    OPENBLAS_NUM_THREADS=4
ENV ASR_MODEL_PATH=/opt/whisper-models/faster-whisper-medium \
    ASR_MODEL_NAME=Systran/faster-whisper-medium
RUN sonic-annotator -l | grep -q "vamp:nnls-chroma:chordino:simplechord"

FROM worker-base AS worker-ci
RUN pip install --no-cache-dir "." \
    && mkdir -p /app/data /app/data/work \
    && chown -R nobody:nogroup /app
USER nobody
RUN sonic-annotator -l | grep -q "vamp:nnls-chroma:chordino:simplechord"
CMD ["python", "-m", "ogniskowy_grajek.worker"]

FROM worker-base AS whisper-model
ARG WHISPER_MODEL_REPOSITORY
ARG WHISPER_MODEL_REVISION
RUN pip install --no-cache-dir faster-whisper==1.2.1 \
    && python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='${WHISPER_MODEL_REPOSITORY}', revision='${WHISPER_MODEL_REVISION}', local_dir='/opt/whisper-models/faster-whisper-medium')"

FROM worker-base AS worker-cpu
RUN pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 \
      --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir ".[worker]" \
    && python -c "from demucs.pretrained import get_model; get_model('htdemucs')"
COPY --from=whisper-model /opt/whisper-models /opt/whisper-models
RUN mkdir -p /app/data /app/data/work && chown -R nobody:nogroup /app /opt/demucs-models
USER nobody
RUN sonic-annotator -l | grep -q "vamp:nnls-chroma:chordino:simplechord"
RUN python -m ogniskowy_grajek.worker --audio-smoke
CMD ["python", "-m", "ogniskowy_grajek.worker"]

FROM worker-base AS worker-gpu
RUN pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 \
      --index-url https://download.pytorch.org/whl/cu124 \
    && pip install --no-cache-dir ".[worker]" \
    && python -c "from demucs.pretrained import get_model; get_model('htdemucs')"
COPY --from=whisper-model /opt/whisper-models /opt/whisper-models
RUN mkdir -p /app/data /app/data/work && chown -R nobody:nogroup /app /opt/demucs-models
USER nobody
RUN sonic-annotator -l | grep -q "vamp:nnls-chroma:chordino:simplechord"
RUN python -m ogniskowy_grajek.worker --audio-smoke
CMD ["python", "-m", "ogniskowy_grajek.worker"]
