# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/plugins/media.py
─────────────────────────────────
Audio + Video plugin: MP3, WAV, AAC, M4A, FLAC, OGG / MP4, MOV, AVI, MKV, WEBM.

Operations
  metadata / read ... duration, streams, codecs (via ffprobe)
  convert ........... transcode to another container/codec (via ffmpeg)
  extract_audio ..... pull the audio track out of a video (via ffmpeg)
  transcribe ........ speech-to-text (via the `whisper` CLI if installed)

All backends shell out to system binaries (ffmpeg / ffprobe / whisper) using
argv lists — never string interpolation. When a binary is absent the operation
returns a clean MissingDependency with an install hint; nothing is bundled.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent2.fileintel.base import Capability, PluginContext, require_binary
from agent2.fileintel.errors import BackendError

_AUDIO = ["mp3", "wav", "aac", "m4a", "flac", "ogg"]
_VIDEO = ["mp4", "mov", "avi", "mkv", "webm"]


def op_metadata(path: str, options: dict, ctx: PluginContext) -> dict:
    ff = require_binary("ffprobe",
                        hint="Install ffmpeg (ffprobe ships with it) and add it to PATH.")
    ctx.progress("Probing media…")
    try:
        out = subprocess.run(
            [ff, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
    except Exception as exc:
        raise BackendError(f"ffprobe failed: {exc}")
    fmt = data.get("format", {})
    streams = [{"type": s.get("codec_type"), "codec": s.get("codec_name"),
                "width": s.get("width"), "height": s.get("height"),
                "sample_rate": s.get("sample_rate")}
               for s in data.get("streams", [])]
    return {"duration_sec": round(float(fmt.get("duration", 0) or 0), 2),
            "bit_rate": fmt.get("bit_rate"),
            "format_name": fmt.get("format_name"), "streams": streams}


def op_convert(path: str, options: dict, ctx: PluginContext) -> dict:
    ff = require_binary("ffmpeg", hint="Install ffmpeg and add it to PATH.")
    to = (options.get("to_format") or "").lower().lstrip(".")
    if not to:
        raise BackendError("convert needs options.to_format (e.g. mp3, mp4, wav).")
    out = options.get("output_path") or str(Path(path).with_suffix(f".{to}"))
    ctx.progress(f"Transcoding → {to}…")
    try:
        r = subprocess.run([ff, "-y", "-i", str(path), out],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise BackendError(f"ffmpeg exit {r.returncode}: {r.stderr[-500:]}")
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"ffmpeg failed: {exc}")
    return {"output_path": out, "to_format": to}


def op_extract_audio(path: str, options: dict, ctx: PluginContext) -> dict:
    ff = require_binary("ffmpeg", hint="Install ffmpeg and add it to PATH.")
    to = (options.get("to_format") or "mp3").lower()
    out = options.get("output_path") or str(Path(path).with_suffix(f".{to}"))
    ctx.progress("Extracting audio track…")
    try:
        r = subprocess.run([ff, "-y", "-i", str(path), "-vn", out],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise BackendError(f"ffmpeg exit {r.returncode}: {r.stderr[-500:]}")
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"ffmpeg failed: {exc}")
    return {"output_path": out, "to_format": to}


def op_transcribe(path: str, options: dict, ctx: PluginContext) -> dict:
    whisper = require_binary(
        "whisper",
        hint="Install OpenAI Whisper CLI: `pip install -U openai-whisper` "
             "(needs ffmpeg), or transcribe with a cloud API.")
    out_dir = Path(options.get("output_dir") or ctx.output_dir or Path(path).parent)
    ctx.progress("Transcribing (this can take a while)…")
    try:
        r = subprocess.run(
            [whisper, str(path), "--model", options.get("model", "base"),
             "--output_dir", str(out_dir), "--output_format", "txt"],
            capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise BackendError(f"whisper exit {r.returncode}: {r.stderr[-500:]}")
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"whisper failed: {exc}")
    txt_path = out_dir / (Path(path).stem + ".txt")
    text = txt_path.read_text(encoding="utf-8", errors="replace") if txt_path.exists() else ""
    return {"output_path": str(txt_path), "text": text[:200_000], "chars": len(text)}


def register(reg) -> None:
    reg.register("audio", _AUDIO, [
        Capability("metadata", op_metadata, backend="ffprobe"),
        Capability("read", op_metadata, backend="ffprobe"),
        Capability("convert", op_convert, backend="ffmpeg"),
        Capability("transcribe", op_transcribe, backend="whisper"),
    ])
    reg.register("video", _VIDEO, [
        Capability("metadata", op_metadata, backend="ffprobe"),
        Capability("read", op_metadata, backend="ffprobe"),
        Capability("convert", op_convert, backend="ffmpeg"),
        Capability("extract_audio", op_extract_audio, backend="ffmpeg"),
        Capability("transcribe", op_transcribe, backend="whisper"),
    ])
