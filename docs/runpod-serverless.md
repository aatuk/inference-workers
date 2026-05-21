# RunPod Serverless Setup

This repository builds container images for RunPod serverless endpoints. The
goal is to avoid ad hoc pod setup and avoid installing heavy Python packages
during every worker cold start.

## What GitHub Provides

GitHub hosts the worker source and runs the image build in GitHub Actions. The
workflow logs in to GitHub Container Registry with the repository's
`GITHUB_TOKEN`, then publishes a public image to GHCR.

No audio data, Hugging Face token, Google credentials, or RunPod API tokens are
stored in GitHub.

## What RunPod Provides

RunPod runs the worker image on demand. The endpoint should be configured with:

```text
Image:        ghcr.io/aatuk/inference-workers-autotranscript-whisperx:latest
Endpoint:     nustdetgux6198 (autotranscript-whisperx-image)
Min workers: 0
Max workers: 1 initially
Idle timeout: 600-1800 seconds
Env:
  HF_TOKEN=<Hugging Face token with pyannote access>
```

With min workers set to zero, there is no steady idle worker charge. A cold
start is still billed while the worker initializes. Baking dependencies into the
image removes the expensive `pip install` cold-start step, but the worker still
needs time to pull the image and load models.

The first smoke tests on 2026-05-21 showed the endpoint accepting jobs while
RunPod left the worker `throttled`; those test jobs were cancelled. This appears
to be RunPod scheduling/capacity rather than a GitHub or image publication
problem.

## What The VPS Provides

The VPS owns orchestration and staging:

```text
/var/lib/autotranscript/runpod-api-token
/var/lib/autotranscript/hf-token
/var/lib/autotranscript/staging
https://meta.acausalcompassion.org/files/<token>/...
```

The staging service stores temporary inputs and outputs below
`/var/lib/autotranscript/staging`. These files are excluded from restic backups.

The eventual serverless coordinator should:

1. Stage an input with `autotranscript-staging stage <audio-file>`.
2. Submit a RunPod job to the configured endpoint.
3. Poll job status.
4. Copy uploaded outputs to the Syncthing or Google Drive output directory.
5. Clean up staging files after completion or expiry.

## Updating The Worker

1. Edit files under `workers/autotranscript-whisperx`.
2. Commit and push to GitHub.
3. Wait for the GitHub Actions build to publish a new image tag.
4. Either leave the RunPod endpoint on `:latest`, or update it to a specific
   SHA tag for reproducibility.
5. Submit a small smoke job before processing real recordings.

## Secrets

Keep secrets out of the image and out of Git:

- `HF_TOKEN`: RunPod endpoint environment variable.
- RunPod API token: VPS secret file.
- Google service account key: VPS/local secret file.

The high-entropy staging URLs are bearer URLs: anyone who has one can fetch or
upload the corresponding file while it exists. They should be treated as
temporary secrets.
