# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/plugins/images.py
──────────────────────────────────
Images plugin: PNG, JPG, WEBP, GIF, BMP, TIFF, SVG, ICO, HEIC.

Operations
  metadata / read ... dimensions, mode, EXIF summary
  convert ........... to another image format (PNG/JPG/WEBP/…) via Pillow
  compress .......... re-encode at a quality/target to shrink file size
  resize ............ scale to width/height (keeps aspect if one is given)
  ocr ............... extract text via pytesseract (needs the tesseract binary)

All Pillow / pytesseract imports are lazy — module loads without them.
"""

from __future__ import annotations

from pathlib import Path

from agent2.fileintel.base import Capability, PluginContext, require, require_binary
from agent2.fileintel.errors import BackendError

_IMG_FORMATS = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "svg", "heic", "ico"]

# Pillow save-format names keyed by our canonical format.
_PIL_FMT = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP",
            "gif": "GIF", "bmp": "BMP", "tiff": "TIFF", "ico": "ICO"}


def _open(path: str):
    Image = require("PIL.Image", "Pillow")
    try:
        return Image.open(path)
    except Exception as exc:
        raise BackendError(f"Could not open image: {exc}")


def op_metadata(path: str, options: dict, ctx: PluginContext) -> dict:
    ctx.progress("Reading image…")
    with _open(path) as im:
        info = {"width": im.width, "height": im.height,
                "dimensions": f"{im.width}x{im.height}",
                "mode": im.mode, "format": im.format}
        exif = {}
        try:
            raw = im.getexif()
            from PIL.ExifTags import TAGS  # type: ignore
            for tag_id, val in raw.items():
                exif[TAGS.get(tag_id, tag_id)] = str(val)[:120]
        except Exception:
            pass
        if exif:
            info["exif"] = exif
    return info


def op_convert(path: str, options: dict, ctx: PluginContext) -> dict:
    to = (options.get("to_format") or "png").lower().lstrip(".")
    if to in ("jpeg",):
        to = "jpg"
    if to == "pdf":
        return _image_to_pdf(path, options, ctx)
    pil_fmt = _PIL_FMT.get(to)
    if not pil_fmt:
        raise BackendError(f"Unsupported image target format '{to}'.")
    out = options.get("output_path") or str(Path(path).with_suffix(f".{to}"))
    ctx.progress(f"Converting → {to.upper()}…")
    with _open(path) as im:
        if pil_fmt == "JPEG" and im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        try:
            im.save(out, pil_fmt)
        except Exception as exc:
            raise BackendError(f"Image conversion failed: {exc}")
    return {"output_path": out, "to_format": to}


def _image_to_pdf(path: str, options: dict, ctx: PluginContext) -> dict:
    out = options.get("output_path") or str(Path(path).with_suffix(".pdf"))
    ctx.progress("Embedding image into PDF…")
    with _open(path) as im:
        if im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        try:
            im.save(out, "PDF")
        except Exception as exc:
            raise BackendError(f"Image→PDF failed: {exc}")
    return {"output_path": out, "to_format": "pdf"}


def op_compress(path: str, options: dict, ctx: PluginContext) -> dict:
    quality = int(options.get("quality", 70))
    out = options.get("output_path") or str(
        Path(path).with_name(Path(path).stem + "_compressed" + Path(path).suffix))
    ctx.progress(f"Compressing (quality={quality})…")
    with _open(path) as im:
        fmt = im.format or _PIL_FMT.get(Path(path).suffix.lstrip(".").lower(), "PNG")
        save_kwargs = {"optimize": True}
        if fmt in ("JPEG", "WEBP"):
            save_kwargs["quality"] = quality
        if fmt == "JPEG" and im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
        try:
            im.save(out, fmt, **save_kwargs)
        except Exception as exc:
            raise BackendError(f"Compression failed: {exc}")
    before = Path(path).stat().st_size
    after = Path(out).stat().st_size
    return {"output_path": out, "before": before, "after": after,
            "saved_pct": round(100 * (1 - after / before), 1) if before else 0}


def op_resize(path: str, options: dict, ctx: PluginContext) -> dict:
    w = options.get("width")
    h = options.get("height")
    if not w and not h:
        raise BackendError("resize needs options.width and/or options.height")
    out = options.get("output_path") or str(
        Path(path).with_name(Path(path).stem + "_resized" + Path(path).suffix))
    ctx.progress("Resizing…")
    with _open(path) as im:
        ow, oh = im.width, im.height
        if w and not h:
            h = int(oh * (int(w) / ow))
        elif h and not w:
            w = int(ow * (int(h) / oh))
        try:
            resized = im.resize((int(w), int(h)))
            resized.save(out)
        except Exception as exc:
            raise BackendError(f"Resize failed: {exc}")
    return {"output_path": out, "dimensions": f"{int(w)}x{int(h)}"}


def op_ocr(path: str, options: dict, ctx: PluginContext) -> dict:
    """OCR an image via pytesseract (requires the tesseract binary on PATH)."""
    pytesseract = require("pytesseract")
    require_binary("tesseract",
                   hint="Install Tesseract OCR (e.g. `winget install tesseract-ocr` / "
                        "`brew install tesseract` / `apt install tesseract-ocr`).")
    Image = require("PIL.Image", "Pillow")
    ctx.progress("Running OCR…")
    try:
        with Image.open(path) as im:
            text = pytesseract.image_to_string(im, lang=options.get("lang", "eng"))
    except Exception as exc:
        raise BackendError(f"OCR failed: {exc}")
    return {"text": text.strip(), "chars": len(text.strip())}


def ocr_pdf(path: str, options: dict, ctx: PluginContext) -> dict:
    """OCR every page of a (scanned) PDF. Used by the documents plugin.

    Needs pdf2image + poppler + tesseract; degrades to a clear MissingDependency.
    """
    convert_from_path = require("pdf2image").convert_from_path
    pytesseract = require("pytesseract")
    require_binary("tesseract", hint="Install Tesseract OCR and ensure it is on PATH.")
    ctx.progress("Rasterising PDF pages…")
    try:
        pages = convert_from_path(str(path))
    except Exception as exc:
        raise BackendError(f"Could not rasterise PDF (is poppler installed?): {exc}")
    out = []
    for i, page in enumerate(pages, 1):
        ctx.progress(f"OCR page {i}/{len(pages)}…")
        out.append(pytesseract.image_to_string(page, lang=options.get("lang", "eng")))
    text = "\n\n".join(out).strip()
    return {"text": text, "pages": len(pages), "chars": len(text)}


def register(reg) -> None:
    reg.register("images", _IMG_FORMATS, [
        Capability("metadata", op_metadata, backend="pillow"),
        Capability("read", op_metadata, backend="pillow"),
        Capability("convert", op_convert, backend="pillow"),
        Capability("compress", op_compress, backend="pillow"),
        Capability("resize", op_resize, backend="pillow"),
        Capability("ocr", op_ocr, backend="tesseract"),
    ])
