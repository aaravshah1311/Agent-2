# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/detector.py
────────────────────────────
File-type detection: given a path, decide its category (documents, spreadsheets,
images, …) and canonical format ("pdf", "docx", "png", …).

Detection is layered for robustness:
  1. Extension  — fast, usually right.
  2. Magic bytes — a small signature sniff of the first bytes; catches
     mis-named files and confirms the extension (used for the security
     extension-vs-content cross-check).
  3. mimetypes  — stdlib fallback for the MIME string.

Nothing here needs a third-party library, so detection always works.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

# ── Canonical format → category ─────────────────────────────────────────────────
# The single source of truth for "what category is this format in". Plugins key
# their capabilities on the format string; the registry keys on the same strings.
CATEGORY_FORMATS: dict[str, list[str]] = {
    "documents":     ["pdf", "docx", "doc", "odt", "rtf", "txt", "md", "html", "epub"],
    "spreadsheets":  ["xlsx", "xls", "csv", "ods", "tsv"],
    "presentations": ["pptx", "ppt", "odp"],
    "images":        ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "svg", "heic", "ico"],
    "audio":         ["mp3", "wav", "aac", "m4a", "flac", "ogg"],
    "video":         ["mp4", "mov", "avi", "mkv", "webm"],
    "archives":      ["zip", "rar", "7z", "tar", "gz", "bz2"],
    "code":          ["py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "c", "cpp",
                      "h", "hpp", "cs", "php", "css", "sql", "json", "yaml", "yml", "xml",
                      "sh", "bat", "ps1", "rb", "toml", "ini", "cfg"],
}

# Reverse map: format → category (first category that lists it wins).
FORMAT_CATEGORY: dict[str, str] = {}
for _cat, _fmts in CATEGORY_FORMATS.items():
    for _f in _fmts:
        FORMAT_CATEGORY.setdefault(_f, _cat)

# Extension aliases → canonical format.
_EXT_ALIAS = {
    "jpeg": "jpg", "tif": "tiff", "yml": "yaml", "htm": "html",
    "tgz": "gz", "mpeg": "mp4",
}

# ── Magic-byte signatures ────────────────────────────────────────────────────────
# (byte-signature, canonical-format). Every signature sits at offset 0, so they are
# dispatched by FIRST BYTE: one dict lookup instead of a linear scan over the whole
# table. A few first bytes are shared by several formats ('I' → tiff/mp3, 'R' →
# riff/rar, 'B' → bmp/bz2, 'P' → the three zip variants), so each bucket is a small
# ordered list probed in turn — first match wins, exactly as before.
_MAGIC: dict[int, list[tuple[bytes, str]]] = {
    ord("%"):    [(b"%PDF", "pdf")],
    0x89:        [(b"\x89PNG\r\n\x1a\n", "png")],
    0xFF:        [(b"\xff\xd8\xff", "jpg")],
    ord("G"):    [(b"GIF87a", "gif"), (b"GIF89a", "gif")],
    ord("B"):    [(b"BM", "bmp"), (b"BZh", "bz2")],
    ord("I"):    [(b"II*\x00", "tiff"), (b"ID3", "mp3")],
    ord("M"):    [(b"MM\x00*", "tiff")],
    0x00:        [(b"\x00\x00\x01\x00", "ico")],
    ord("R"):    [(b"RIFF", "riff"),        # wav/webp/avi — refined by sub-tag below
                  (b"Rar!\x1a\x07", "rar")],
    ord("O"):    [(b"OggS", "ogg")],
    ord("f"):    [(b"fLaC", "flac")],
    ord("7"):    [(b"7z\xbc\xaf\x27\x1c", "7z")],
    0x1F:        [(b"\x1f\x8b", "gz")],
    ord("P"):    [(b"PK\x03\x04", "zip"),   # also docx/xlsx/pptx/odt/epub
                  (b"PK\x05\x06", "zip"),
                  (b"PK\x07\x08", "zip")],
}

# Zip-container formats disambiguated by their internal layout.
_ZIP_INNER = {
    "word/": "docx", "xl/": "xlsx", "ppt/": "pptx",
    "mimetypeapplication/vnd.oasis.opendocument.text": "odt",
    "mimetypeapplication/vnd.oasis.opendocument.spreadsheet": "ods",
    "mimetypeapplication/vnd.oasis.opendocument.presentation": "odp",
    "mimetypeapplication/epub+zip": "epub",
}


def _sniff_magic(path: Path) -> str | None:
    """Return a format from magic bytes, or None. Never raises."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(512)
    except Exception:
        return None
    if not head:
        return None
    for sig, fmt in _MAGIC.get(head[0], ()):
        if head[:len(sig)] == sig:
            if fmt == "riff":
                tag = head[8:12]
                if tag == b"WEBP":
                    return "webp"
                if tag == b"WAVE":
                    return "wav"
                if tag == b"AVI ":
                    return "avi"
                return None
            if fmt == "zip":
                return _refine_zip(path) or "zip"
            return fmt
    return None


def _refine_zip(path: Path) -> str | None:
    """Peek inside a zip container to tell docx/xlsx/pptx/odt/epub apart."""
    try:
        import zipfile
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            joined = "".join(names)
            # ODF/EPUB store a 'mimetype' member; read it for an exact answer.
            if "mimetype" in names:
                try:
                    mt = zf.read("mimetype").decode("ascii", "replace")
                    for key, fmt in _ZIP_INNER.items():
                        if key.startswith("mimetype") and mt in key:
                            return fmt
                except Exception:
                    pass
            for key, fmt in _ZIP_INNER.items():
                if key.startswith("mimetype"):
                    continue
                if any(n.startswith(key) for n in names) or key in joined:
                    return fmt
    except Exception:
        return None
    return None


def normalize_format(ext_or_fmt: str) -> str:
    f = (ext_or_fmt or "").lower().lstrip(".")
    return _EXT_ALIAS.get(f, f)


def detect(path: str) -> dict:
    """Detect a file's type.

    Returns:
        {
          "path": str, "exists": bool, "format": str|None, "category": str|None,
          "mime": str, "ext_format": str|None, "magic_format": str|None,
          "mismatch": bool, "confidence": "high"|"medium"|"low"
        }
    Never raises — a bad path just yields exists=False.
    """
    p = Path(path).expanduser()
    ext_fmt = normalize_format(p.suffix) or None
    magic_fmt = _sniff_magic(p) if p.exists() and p.is_file() else None
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"

    # Resolve the winning format: magic bytes beat the extension when both exist
    # and disagree on a *binary* type; text-ish formats (code, csv, md) have no
    # magic, so the extension is authoritative for them.
    fmt = magic_fmt or ext_fmt
    if magic_fmt and ext_fmt and magic_fmt != ext_fmt:
        # zip-container formats: magic already refined; trust it.
        fmt = magic_fmt

    category = FORMAT_CATEGORY.get(fmt) if fmt else None
    mismatch = bool(ext_fmt and magic_fmt and ext_fmt != magic_fmt
                    and not _compatible(ext_fmt, magic_fmt))

    if magic_fmt and ext_fmt and magic_fmt == ext_fmt:
        conf = "high"
    elif magic_fmt or (ext_fmt and category):
        conf = "medium" if mismatch else "high" if magic_fmt else "medium"
    else:
        conf = "low"

    return {
        "path": str(p),
        "exists": p.exists(),
        "format": fmt,
        "category": category,
        "mime": mime,
        "ext_format": ext_fmt,
        "magic_format": magic_fmt,
        "mismatch": mismatch,
        "confidence": conf,
    }


def _compatible(a: str, b: str) -> bool:
    """True when two formats are effectively the same container family."""
    zipish = {"zip", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub"}
    if a in zipish and b in zipish:
        return True
    return normalize_format(a) == normalize_format(b)
