#!/usr/bin/env python3
import gc
import json
import math
import mimetypes
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import runpod


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


def transcribe_recording(job):
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

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or ""
    if diarize and not token:
        raise RuntimeError("HF_TOKEN is required when diarize=true")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu" and compute_type == "float16":
        compute_type = "int8"

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
        download(input_url, audio_path)

        t0 = time.perf_counter()
        model = whisperx.load_model(
            model_name,
            device,
            compute_type=compute_type,
            language=language,
            download_root=str(model_dir),
        )
        t1 = time.perf_counter()
        audio = whisperx.load_audio(str(audio_path))
        result = model.transcribe(audio, batch_size=batch_size, language=language)
        t2 = time.perf_counter()
        del model
        gc.collect()

        model_a, metadata = whisperx.load_align_model(
            language_code=result["language"],
            device=device,
        )
        result = whisperx.align(
            result["segments"],
            model_a,
            metadata,
            audio,
            device,
            return_char_alignments=False,
        )
        t3 = time.perf_counter()
        del model_a
        gc.collect()

        diarization_path = out_dir / "recording.diarization.csv"
        if diarize:
            diarize_model = whisperx.diarize.DiarizationPipeline(
                token=token,
                device=device,
                cache_dir=str(pyannote_cache),
            )
            diarize_segments = diarize_model(
                str(audio_path),
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )
            diarize_segments.to_csv(diarization_path, index=False)
            result = whisperx.assign_word_speakers(diarize_segments, result)
        else:
            diarization_path.write_text("diarization disabled\n", encoding="utf-8")
        t4 = time.perf_counter()

        summary = {
            "model": model_name,
            "language": result.get("language"),
            "device": device,
            "compute_type": compute_type,
            "diarize": diarize,
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

        uploaded = {
            path.name: upload(output_base_url, path)
            for path in [markdown_path, docx_path, whisperx_path, diarization_path]
        }
        return {"summary": summary, "uploaded": uploaded}


def handler(event):
    job = event.get("input", event)
    return transcribe_recording(job)


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
