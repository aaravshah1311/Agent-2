# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/metadata.py
────────────────────────────
Universal file metadata extraction.

`extract_metadata(path)` always returns the cheap, dependency-free facts
(name, ext, mime, size, timestamps, sha256). It then layers on type-specific
metadata (page_count, dimensions, duration) *best-effort* — if the optional lib
for that type is missing, those fields are simply omitted (never an error).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, UTC
from pathlib import Path

from agent2.fileintel.detector import detect

_HASH_LIMIT = 200 * 1024 * 1024  # don't checksum files larger than 200 MB


def _sha256(p: Path) -> str | None:
    try:
        if p.stat().st_size > _HASH_LIMIT:
            return None
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=UTC).isoformat()
    except Exception:
        return ""


def extract_metadata(path: str, deep: bool = True) -> dict:
    """Return a metadata dict for a file. Never raises.

    Set deep=False to skip the checksum and type-specific probing (faster for
    directory listings).
    """
    p = Path(path).expanduser()
    det = detect(str(p))
    meta: dict = {
        "filename": p.name,
        "path": str(p),
        "extension": p.suffix.lstrip(".").lower(),
        "format": det["format"],
        "category": det["category"],
        "mime": det["mime"],
        "exists": p.exists(),
    }
    if not p.exists() or not p.is_file():
        return meta

    try:
        st = p.stat()
        meta.update({
            "size": st.st_size,
            "size_human": _human(st.st_size),
            "created": _iso(getattr(st, "st_ctime", st.st_mtime)),
            "modified": _iso(st.st_mtime),
        })
    except Exception:
        pass

    if not deep:
        return meta

    meta["sha256"] = _sha256(p)

    # ── Type-specific, best-effort (all failures swallowed) ──────────────────
    cat = det["category"]
    fmt = det["format"]
    try:
        if cat == "images":
            meta.update(_image_meta(p, fmt))
        elif fmt == "pdf":
            meta.update(_pdf_meta(p))
        elif cat in ("audio", "video"):
            meta.update(_media_meta(p))
        elif fmt in ("csv", "tsv"):
            meta.update(_csv_meta(p, fmt))
    except Exception:
        pass  # metadata is a bonus, never a failure

    return meta


def _human(n: int) -> str:
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} PB"


def _image_meta(p: Path, fmt: str) -> dict:
    if fmt == "svg":
        return {"vector": True}
    try:
        from PIL import Image  # type: ignore
        with Image.open(p) as im:
            return {"width": im.width, "height": im.height,
                    "dimensions": f"{im.width}x{im.height}",
                    "image_mode": im.mode,
                    "frames": getattr(im, "n_frames", 1)}
    except Exception:
        return {}


def _pdf_meta(p: Path) -> dict:
    try:
        import pypdf  # type: ignore
        r = pypdf.PdfReader(str(p))
        out = {"page_count": len(r.pages), "encrypted": r.is_encrypted}
        if r.metadata:
            for k in ("title", "author", "creator", "producer"):
                v = getattr(r.metadata, k, None)
                if v:
                    out[k] = str(v)
        return out
    except Exception:
        return {}


def _media_meta(p: Path) -> dict:
    """Duration via ffprobe if present; otherwise nothing."""
    import shutil
    import subprocess
    import json as _json
    ff = shutil.which("ffprobe")
    if not ff:
        return {}
    try:
        out = subprocess.run(
            [ff, "-v", "quiet", "-print_format", "json", "-show_format", str(p)],
            capture_output=True, text=True, timeout=15,
        )
        data = _json.loads(out.stdout or "{}")
        dur = data.get("format", {}).get("duration")
        return {"duration_sec": round(float(dur), 2)} if dur else {}
    except Exception:
        return {}


def _csv_meta(p: Path, fmt: str) -> dict:
    import csv
    delim = "\t" if fmt == "tsv" else ","
    try:
        with open(p, encoding="utf-8", errors="replace", newline="") as fh:
            reader = csv.reader(fh, delimiter=delim)
            rows = 0
            cols = 0
            for i, row in enumerate(reader):
                rows += 1
                if i == 0:
                    cols = len(row)
                if rows > 100_000:
                    break
        return {"rows": rows, "columns": cols}
    except Exception:
        return {}
