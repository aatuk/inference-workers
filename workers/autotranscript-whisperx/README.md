# Autotranscript WhisperX Worker

RunPod serverless worker for retreat transcription.

## Job Input

Submit a RunPod job with:

```json
{
  "input": {
    "input_url": "https://meta.acausalcompassion.org/files/<token>/input.mp3",
    "output_base_url": "https://meta.acausalcompassion.org/files/<token>/outputs",
    "model": "large-v3",
    "language": "en",
    "diarize": true,
    "min_speakers": 1,
    "max_speakers": 12,
    "batch_size": 16,
    "compute_type": "float16"
  }
}
```

`language` may be omitted for automatic language detection. For multilingual
meetings, leaving it unset is usually better than forcing English.

## Outputs

The worker uploads:

```text
recording.md
recording.docx
recording.whisperx.json
recording.diarization.csv
```

## Build Locally

```sh
docker build -f workers/autotranscript-whisperx/Dockerfile \
  -t autotranscript-whisperx:local .
```

## RunPod Runtime

The image expects `HF_TOKEN` in the environment when diarization is enabled.
The token must have accepted access to the relevant pyannote models on
Hugging Face.
