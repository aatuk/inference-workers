FROM pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    AUTOTRANSCRIPT_MODEL_CACHE=/models/whisperx \
    AUTOTRANSCRIPT_PYANNOTE_CACHE=/models/pyannote

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg \
      git \
    && rm -rf /var/lib/apt/lists/*

COPY workers/autotranscript-whisperx/requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /app/requirements.txt \
    && mkdir -p /models/huggingface /models/torch /models/whisperx /models/pyannote

COPY workers/autotranscript-whisperx/handler.py /app/handler.py

CMD ["python", "-u", "/app/handler.py"]
