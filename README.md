# Inference Workers

Reusable GPU worker images for services that are easier to run on rented
inference infrastructure than on the VPS.

The first worker is `autotranscript-whisperx`, a RunPod serverless worker that
downloads staged audio, runs WhisperX with optional diarization, and uploads
transcript artifacts back to the staging service.

## Architecture

The VPS remains the coordinator:

1. A local service notices work, such as an audio file in Syncthing or Google
   Drive.
2. The VPS stages the input at a temporary high-entropy URL under
   `https://meta.acausalcompassion.org/files/<token>/...`.
3. The VPS submits a RunPod serverless job containing only the input URL,
   output URL, and model settings.
4. The worker downloads the audio, transcribes it, and uploads output files.
5. The VPS or dashboard can inspect status and move completed outputs.

The container image contains no secrets. Runtime secrets such as `HF_TOKEN` and
RunPod API tokens live in RunPod endpoint configuration or on the VPS.

## Images

GitHub Actions publishes:

```text
ghcr.io/aatuk/inference-workers-autotranscript-whisperx:latest
ghcr.io/aatuk/inference-workers-autotranscript-whisperx:<git-sha>
```

## Documentation

- `docs/runpod-serverless.md` explains how the image, RunPod endpoint, and VPS
  staging service fit together.
- `workers/autotranscript-whisperx/README.md` documents the worker payload and
  local build commands.
