#!/usr/bin/env python3
import gc
import json
import math
import mimetypes
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import runpod


def log_event(level, message, **fields):
    print(
        json.dumps(
            {
                "level": level,
                "message": message,
                **fields,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def progress(event, stage, **fields):
    payload = {"stage": stage, **fields}
    log_event("INFO", "progress", **payload)
    if event is None:
        return
    try:
        runpod.serverless.progress_update(event, payload)
    except AttributeError:
        try:
            from runpod.serverless import progress_update

            progress_update(event, payload)
        except Exception as exc:
            log_event("WARNING", "progress_update failed", error=str(exc))
    except Exception as exc:
        log_event("WARNING", "progress_update failed", error=str(exc))


class Timeout:
    def __init__(self, seconds, label):
        self.seconds = int(seconds)
        self.label = label
        self.previous = None

    def __enter__(self):
        if self.seconds <= 0:
            return
        self.previous = signal.signal(signal.SIGALRM, self._handle_timeout)
        signal.alarm(self.seconds)

    def __exit__(self, exc_type, exc, tb):
        if self.seconds > 0:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, self.previous)

    def _handle_timeout(self, signum, frame):
        raise TimeoutError(f"{self.label} exceeded {self.seconds}s")


def jsonable(value):
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def format_ts(seconds):
    minutes, sec = divmod(float(seconds), 60.0)
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours:02d}:{minutes:02d}:{sec:05.2f}"


def download(url, path):
    with urllib.request.urlopen(url, timeout=600) as response, path.open("wb") as out:
        shutil.copyfileobj(response, out)


def upload(base_url, path):
    data = path.read_bytes()
    url = base_url.rstrip("/") + "/" + urllib.parse.quote(path.name)
    request = urllib.request.Request(url, data=data, method="PUT")
    request.add_header(
        "Content-Type",
        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )
    request.add_header("Content-Length", str(len(data)))
    with urllib.request.urlopen(request, timeout=600) as response:
        response.read()
    return url


def docx_paragraph(text="", *, bold=False, monospace=False):
    text = escape(text)
    run_props = []
    if bold:
        run_props.append("<w:b/>")
    if monospace:
        run_props.append('<w:rFonts w:ascii="Courier New" w:hAnsi="Courier New"/>')
    props = f"<w:rPr>{''.join(run_props)}</w:rPr>" if run_props else ""
    return "<w:p><w:r>" f'{props}<w:t xml:space="preserve">{text}</w:t>' "</w:r></w:p>"


def write_docx_from_markdown(markdown_path, docx_path):
    paragraphs = []
    in_code = False
    for raw_line in markdown_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            paragraphs.append(docx_paragraph(line, monospace=True))
        elif line.startswith("# "):
            paragraphs.append(docx_paragraph(line[2:].strip(), bold=True))
        elif line.startswith("## "):
            paragraphs.append(docx_paragraph(line[3:].strip(), bold=True))
        elif line.startswith("### "):
            paragraphs.append(docx_paragraph(line[4:].strip(), bold=True))
        elif not line:
            paragraphs.append("<w:p/>")
        else:
            paragraphs.append(docx_paragraph(line))

    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    %s
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>
    </w:sectPr>
  </w:body>
</w:document>
""" % "\n    ".join(paragraphs)
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
"""
    relationships = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
"""
    with zipfile.ZipFile(docx_path, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types)
        docx.writestr("_rels/.rels", relationships)
        docx.writestr("word/document.xml", document_xml)


def truthy(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def tail_text(path, limit=12000):
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    return text[-limit:]


def pyannote_from_pretrained_diagnostic(job, event=None):
    model_name = job.get(
        "diarization_model",
        "pyannote/speaker-diarization-community-1",
    )
    timeout = int(job.get("timeout_seconds", 120))
    stack_after = int(job.get("stack_after_seconds", 30))
    move_to_device = job.get("move_to_device") or ""
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or ""
    if not token:
        raise RuntimeError("HF_TOKEN is required for pyannote diagnostics")

    progress(
        event,
        "pyannote_diagnostic_start",
        model=model_name,
        timeout_seconds=timeout,
        move_to_device=move_to_device or None,
    )
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        result_path = work / "result.json"
        stack_path = work / "stack.txt"
        stdout_path = work / "stdout.txt"
        stderr_path = work / "stderr.txt"
        cache_dir = Path(os.environ.get("AUTOTRANSCRIPT_PYANNOTE_CACHE", "/models/pyannote"))
        cache_dir.mkdir(parents=True, exist_ok=True)

        code = r"""
import faulthandler
import json
import os
import time
from pathlib import Path

result_path = Path(os.environ["DIAG_RESULT"])
stack_path = Path(os.environ["DIAG_STACK"])
with stack_path.open("w", encoding="utf-8") as stack:
    faulthandler.enable(stack)
    faulthandler.dump_traceback_later(
        int(os.environ["DIAG_STACK_AFTER"]),
        repeat=True,
        file=stack,
    )
    t0 = time.perf_counter()
    from pyannote.audio import Pipeline
    t1 = time.perf_counter()
    pipeline = Pipeline.from_pretrained(
        os.environ["DIAG_MODEL"],
        token=os.environ["HF_TOKEN"],
        cache_dir=os.environ["DIAG_CACHE"],
    )
    t2 = time.perf_counter()
    out = {
        "ok": True,
        "import_seconds": round(t1 - t0, 3),
        "from_pretrained_seconds": round(t2 - t1, 3),
        "pipeline_type": type(pipeline).__name__,
    }
    device = os.environ.get("DIAG_MOVE_TO_DEVICE")
    if device:
        import torch

        pipeline.to(torch.device(device))
        out["to_device_seconds"] = round(time.perf_counter() - t2, 3)
    faulthandler.cancel_dump_traceback_later()
    result_path.write_text(json.dumps(out, sort_keys=True), encoding="utf-8")
"""
        env = os.environ.copy()
        env.update(
            {
                "DIAG_RESULT": str(result_path),
                "DIAG_STACK": str(stack_path),
                "DIAG_STACK_AFTER": str(stack_after),
                "DIAG_MODEL": model_name,
                "DIAG_CACHE": str(cache_dir),
                "HF_TOKEN": token,
                "DIAG_MOVE_TO_DEVICE": str(move_to_device),
            }
        )
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", code],
                stdout=stdout,
                stderr=stderr,
                env=env,
            )
            try:
                return_code = process.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=30)
                timed_out = True

        result = {
            "ok": not timed_out and return_code == 0,
            "timed_out": timed_out,
            "return_code": return_code,
            "model": model_name,
            "stdout_tail": tail_text(stdout_path),
            "stderr_tail": tail_text(stderr_path),
            "stack_tail": tail_text(stack_path),
        }
        if result_path.exists():
            result["child_result"] = json.loads(result_path.read_text(encoding="utf-8"))
        progress(event, "pyannote_diagnostic_done", ok=result["ok"], timed_out=timed_out)
        return result


def run_diarization_subprocess(
    *,
    audio_path,
    output_csv,
    model_name,
    token,
    device,
    cache_dir,
    min_speakers,
    max_speakers,
    timeout,
    event=None,
):
    progress(
        event,
        "diarization_subprocess_start",
        model=model_name,
        timeout_seconds=timeout,
        device=device,
    )
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        result_path = work / "result.json"
        progress_path = work / "progress.jsonl"
        stack_path = work / "stack.txt"
        stdout_path = work / "stdout.txt"
        stderr_path = work / "stderr.txt"

        code = r"""
import faulthandler
import json
import os
import time
from pathlib import Path

result_path = Path(os.environ["DIAR_RESULT"])
progress_path = Path(os.environ["DIAR_PROGRESS"])
stack_path = Path(os.environ["DIAR_STACK"])
output_csv = Path(os.environ["DIAR_OUTPUT_CSV"])


def emit(**payload):
    with progress_path.open("a", encoding="utf-8") as progress_file:
        progress_file.write(json.dumps(payload, sort_keys=True) + "\n")


def optional_int(name):
    value = os.environ.get(name, "")
    return int(value) if value else None


with stack_path.open("w", encoding="utf-8") as stack:
    faulthandler.enable(stack)
    faulthandler.dump_traceback_later(60, repeat=True, file=stack)
    t0 = time.perf_counter()
    emit(stage="diarization_child_import_start")
    import pandas as pd
    import torch
    from pyannote.audio import Pipeline
    from whisperx.audio import SAMPLE_RATE, load_audio

    t1 = time.perf_counter()
    emit(stage="diarization_child_import_done", seconds=round(t1 - t0, 3))
    emit(stage="diarization_pipeline_from_pretrained_start", model=os.environ["DIAR_MODEL"])
    pipeline = Pipeline.from_pretrained(
        os.environ["DIAR_MODEL"],
        token=os.environ["HF_TOKEN"],
        cache_dir=os.environ["DIAR_CACHE"],
    )
    t2 = time.perf_counter()
    emit(stage="diarization_pipeline_from_pretrained_done", seconds=round(t2 - t1, 3))
    emit(stage="diarization_pipeline_to_device_start", device=os.environ["DIAR_DEVICE"])
    pipeline.to(torch.device(os.environ["DIAR_DEVICE"]))
    t3 = time.perf_counter()
    emit(stage="diarization_pipeline_to_device_done", seconds=round(t3 - t2, 3))
    emit(stage="diarization_audio_load_start")
    audio = load_audio(os.environ["DIAR_AUDIO_PATH"])
    emit(stage="diarization_start")

    ranges = {
        "segmentation": (0.0, 50.0),
        "embeddings": (50.0, 99.0),
    }
    last_percent = [0.0]

    def hook(step_name, step_artifact, file=None, total=None, completed=None):
        if total is None or completed is None or total <= 0:
            return
        offset, end = ranges.get(step_name, (0.0, 99.0))
        percent = offset + min(completed / total, 1.0) * (end - offset)
        if percent > last_percent[0]:
            last_percent[0] = percent
            emit(stage="diarization_progress", percent=round(float(percent), 2))

    output = pipeline(
        {"waveform": torch.from_numpy(audio[None, :]), "sample_rate": SAMPLE_RATE},
        min_speakers=optional_int("DIAR_MIN_SPEAKERS"),
        max_speakers=optional_int("DIAR_MAX_SPEAKERS"),
        hook=hook,
    )
    emit(stage="diarization_progress", percent=100.0)
    diarization = getattr(output, "speaker_diarization", output)
    rows = pd.DataFrame(
        diarization.itertracks(yield_label=True),
        columns=["segment", "label", "speaker"],
    )
    if len(rows) == 0:
        rows = pd.DataFrame(columns=["segment", "label", "speaker", "start", "end"])
    else:
        rows["start"] = rows["segment"].apply(lambda segment: segment.start)
        rows["end"] = rows["segment"].apply(lambda segment: segment.end)
    rows.to_csv(output_csv, index=False)
    t4 = time.perf_counter()
    emit(stage="diarization_done", segments=len(rows), seconds=round(t4 - t3, 3))
    faulthandler.cancel_dump_traceback_later()
    result_path.write_text(
        json.dumps(
            {
                "ok": True,
                "segments": int(len(rows)),
                "import_seconds": round(t1 - t0, 3),
                "from_pretrained_seconds": round(t2 - t1, 3),
                "to_device_seconds": round(t3 - t2, 3),
                "diarization_seconds": round(t4 - t3, 3),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
"""
        env = os.environ.copy()
        env.update(
            {
                "DIAR_RESULT": str(result_path),
                "DIAR_PROGRESS": str(progress_path),
                "DIAR_STACK": str(stack_path),
                "DIAR_OUTPUT_CSV": str(output_csv),
                "DIAR_MODEL": model_name,
                "DIAR_CACHE": str(cache_dir),
                "DIAR_DEVICE": str(device),
                "DIAR_AUDIO_PATH": str(audio_path),
                "DIAR_MIN_SPEAKERS": "" if min_speakers is None else str(min_speakers),
                "DIAR_MAX_SPEAKERS": "" if max_speakers is None else str(max_speakers),
                "HF_TOKEN": token,
            }
        )

        seen_progress_lines = 0
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", code],
                stdout=stdout,
                stderr=stderr,
                env=env,
            )
            deadline = time.monotonic() + timeout
            timed_out = False
            while True:
                if progress_path.exists():
                    lines = progress_path.read_text(encoding="utf-8").splitlines()
                    for line in lines[seen_progress_lines:]:
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        stage = payload.pop("stage", "diarization_subprocess_progress")
                        progress(event, stage, **payload)
                    seen_progress_lines = len(lines)

                return_code = process.poll()
                if return_code is not None:
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    process.kill()
                    return_code = process.wait(timeout=30)
                    break
                time.sleep(1)

        if progress_path.exists():
            lines = progress_path.read_text(encoding="utf-8").splitlines()
            for line in lines[seen_progress_lines:]:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stage = payload.pop("stage", "diarization_subprocess_progress")
                progress(event, stage, **payload)

        if timed_out or return_code != 0:
            raise RuntimeError(
                json.dumps(
                    {
                        "message": "diarization subprocess failed",
                        "timed_out": timed_out,
                        "return_code": return_code,
                        "stdout_tail": tail_text(stdout_path),
                        "stderr_tail": tail_text(stderr_path),
                        "stack_tail": tail_text(stack_path),
                    },
                    sort_keys=True,
                )
            )

        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
        else:
            result = {"ok": True}
        progress(event, "diarization_subprocess_done", **result)
        return result


def transcribe_recording(job, event=None):
    import torch
    import whisperx

    input_url = job["input_url"]
    output_base_url = job["output_base_url"]
    model_name = job.get("model", "large-v3")
    language = job.get("language") or None
    diarize = truthy(job.get("diarize", True))
    min_speakers = job.get("min_speakers")
    max_speakers = job.get("max_speakers")
    batch_size = int(job.get("batch_size", 16))
    compute_type = job.get("compute_type", "float16")
    diarize_timeout = int(job.get("diarize_timeout_seconds", 30 * 60))
    diarize_pipeline_load_timeout = int(
        job.get("diarize_pipeline_load_timeout_seconds", diarize_timeout)
    )
    diarize_subprocess_timeout = int(
        job.get(
            "diarize_subprocess_timeout_seconds",
            diarize_pipeline_load_timeout + diarize_timeout + 60,
        )
    )
    diarization_model = job.get(
        "diarization_model",
        "pyannote/speaker-diarization-community-1",
    )

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or ""
    if diarize and not token:
        raise RuntimeError("HF_TOKEN is required when diarize=true")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu" and compute_type == "float16":
        compute_type = "int8"
    progress(
        event,
        "received",
        model=model_name,
        language=language,
        diarize=diarize,
        device=device,
        compute_type=compute_type,
        diarization_model=diarization_model if diarize else None,
    )

    suffix = Path(urllib.parse.urlsplit(input_url).path).suffix or ".audio"
    model_dir = Path(os.environ.get("AUTOTRANSCRIPT_MODEL_CACHE", "/models/whisperx"))
    pyannote_cache = Path(os.environ.get("AUTOTRANSCRIPT_PYANNOTE_CACHE", "/models/pyannote"))
    model_dir.mkdir(parents=True, exist_ok=True)
    pyannote_cache.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        audio_path = work / f"recording{suffix}"
        out_dir = work / "out"
        out_dir.mkdir()
        progress(event, "download_start")
        download(input_url, audio_path)
        progress(event, "download_done", bytes=audio_path.stat().st_size)

        t0 = time.perf_counter()
        progress(event, "whisper_model_load_start")
        model = whisperx.load_model(
            model_name,
            device,
            compute_type=compute_type,
            language=language,
            download_root=str(model_dir),
        )
        t1 = time.perf_counter()
        progress(event, "whisper_model_load_done", seconds=round(t1 - t0, 3))
        audio = whisperx.load_audio(str(audio_path))
        progress(event, "transcribe_start", batch_size=batch_size)
        result = model.transcribe(audio, batch_size=batch_size, language=language)
        t2 = time.perf_counter()
        progress(
            event,
            "transcribe_done",
            seconds=round(t2 - t1, 3),
            segments=len(result.get("segments", [])),
            detected_language=result.get("language"),
        )
        del model
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()

        progress(event, "align_model_load_start", language=result["language"])
        model_a, metadata = whisperx.load_align_model(
            language_code=result["language"],
            device=device,
        )
        progress(event, "align_start")
        result = whisperx.align(
            result["segments"],
            model_a,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )
        t3 = time.perf_counter()
        progress(event, "align_done", seconds=round(t3 - t2, 3))
        del model_a
        gc.collect()
        if device == "cuda":
            torch.cuda.empty_cache()

        diarization_path = out_dir / "recording.diarization.csv"
        if diarize:
            import pandas as pd

            run_diarization_subprocess(
                audio_path=audio_path,
                output_csv=diarization_path,
                model_name=diarization_model,
                token=token,
                device=device,
                cache_dir=pyannote_cache,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
                timeout=diarize_subprocess_timeout,
                event=event,
            )
            diarize_segments = pd.read_csv(diarization_path)
            progress(event, "diarization_done", segments=len(diarize_segments))
            progress(event, "speaker_assignment_start")
            result = whisperx.assign_word_speakers(diarize_segments, result)
            progress(event, "speaker_assignment_done")
        else:
            diarization_path.write_text("diarization disabled\n", encoding="utf-8")
        t4 = time.perf_counter()

        summary = {
            "model": model_name,
            "language": result.get("language"),
            "device": device,
            "compute_type": compute_type,
            "diarize": diarize,
            "diarization_model": diarization_model if diarize else None,
            "diarization_backend": "subprocess" if diarize else None,
            "load_seconds": round(t1 - t0, 3),
            "transcribe_seconds": round(t2 - t1, 3),
            "align_seconds": round(t3 - t2, 3),
            "diarize_seconds": round(t4 - t3, 3),
            "total_seconds": round(t4 - t0, 3),
            "segments": len(result.get("segments", [])),
        }

        whisperx_path = out_dir / "recording.whisperx.json"
        markdown_path = out_dir / "recording.md"
        docx_path = out_dir / "recording.docx"

        whisperx_path.write_text(
            json.dumps(jsonable({"summary": summary, "result": result}), indent=2),
            encoding="utf-8",
        )

        lines = [
            "# Transcript",
            "",
            "## Summary",
            "",
            f"- Model: `{model_name}`",
            f"- Language: `{result.get('language', 'unknown')}`",
            f"- Device: `{device}`",
            f"- Diarization: `{diarize}`",
            f"- Total processing seconds: `{summary['total_seconds']}`",
            "",
            "## Text",
            "",
        ]
        for segment in result.get("segments", []):
            speaker = segment.get("speaker", "SPEAKER_UNKNOWN")
            start = format_ts(segment.get("start", 0.0))
            end = format_ts(segment.get("end", 0.0))
            text = " ".join(str(segment.get("text", "")).split())
            if text:
                lines.append(f"**[{start} - {end}] {speaker}:** {text}")
                lines.append("")
        markdown_path.write_text("\n".join(lines), encoding="utf-8")
        write_docx_from_markdown(markdown_path, docx_path)

        progress(event, "upload_start")
        uploaded = {
            path.name: upload(output_base_url, path)
            for path in [markdown_path, docx_path, whisperx_path, diarization_path]
        }
        progress(event, "upload_done", files=list(uploaded))
        return {"summary": summary, "uploaded": uploaded}


def handler(event):
    job = event.get("input", event)
    if job.get("diagnostic") == "pyannote_from_pretrained":
        return pyannote_from_pretrained_diagnostic(job, event)
    return transcribe_recording(job, event)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
