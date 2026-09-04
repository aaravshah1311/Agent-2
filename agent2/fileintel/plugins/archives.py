# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/plugins/archives.py
────────────────────────────────────
Archives plugin: ZIP, TAR, GZ, BZ2, 7Z, RAR.

Operations
  list / read ... list entries (name, size, compressed size)
  extract ....... unpack into a directory
  inspect ....... totals: entry count, uncompressed size, ratio
  create ........ build a zip/tar from options.paths (batch)

ZIP/TAR/GZ/BZ2 use the stdlib (always available). 7Z uses py7zr, RAR uses
rarfile (+ the `unrar` binary) — both degrade gracefully.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

from agent2.fileintel.base import Capability, PluginContext, require
from agent2.fileintel.errors import BackendError

_ARCHIVE_FORMATS = ["zip", "tar", "gz", "bz2", "7z", "rar"]


def _entries(path: str, fmt: str) -> list[dict]:
    if fmt == "zip":
        with zipfile.ZipFile(path) as zf:
            return [{"name": i.filename, "size": i.file_size,
                     "compressed": i.compress_size} for i in zf.infolist()]
    if fmt in ("tar", "gz", "bz2"):
        # gz/bz2 here mean tar.gz / tar.bz2 or a single compressed file.
        try:
            with tarfile.open(path) as tf:
                return [{"name": m.name, "size": m.size} for m in tf.getmembers()]
        except tarfile.ReadError:
            # A bare .gz/.bz2 (single file, not a tar) — describe it as one member.
            return [{"name": Path(path).stem, "size": Path(path).stat().st_size,
                     "note": "single compressed file (not a tar archive)"}]
    if fmt == "7z":
        py7zr = require("py7zr")
        with py7zr.SevenZipFile(path, "r") as z:
            return [{"name": n} for n in z.getnames()]
    if fmt == "rar":
        rarfile = require("rarfile")
        with rarfile.RarFile(path) as rf:
            return [{"name": i.filename, "size": i.file_size} for i in rf.infolist()]
    raise BackendError(f"Cannot list '.{fmt}' archives.")


def op_list(path: str, options: dict, ctx: PluginContext) -> dict:
    from agent2.fileintel.detector import detect
    fmt = detect(path)["format"]
    ctx.progress("Reading archive index…")
    entries = _entries(path, fmt)
    return {"format": fmt, "count": len(entries), "entries": entries[:1000],
            "truncated": len(entries) > 1000}


def op_inspect(path: str, options: dict, ctx: PluginContext) -> dict:
    from agent2.fileintel.detector import detect
    fmt = detect(path)["format"]
    entries = _entries(path, fmt)
    total = sum(e.get("size", 0) for e in entries)
    comp = Path(path).stat().st_size
    return {"format": fmt, "count": len(entries), "uncompressed": total,
            "archive_size": comp,
            "ratio": round(comp / total, 3) if total else None}


def op_extract(path: str, options: dict, ctx: PluginContext) -> dict:
    from agent2.fileintel.detector import detect
    fmt = detect(path)["format"]
    out_dir = Path(options.get("output_dir") or ctx.output_dir
                   or Path(path).with_suffix("").as_posix() + "_extracted")
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx.progress(f"Extracting → {out_dir}…")
    try:
        if fmt == "zip":
            with zipfile.ZipFile(path) as zf:
                _safe_extract_zip(zf, out_dir)
        elif fmt in ("tar", "gz", "bz2"):
            with tarfile.open(path) as tf:
                _safe_extract_tar(tf, out_dir)
        elif fmt == "7z":
            py7zr = require("py7zr")
            with py7zr.SevenZipFile(path, "r") as z:
                z.extractall(path=str(out_dir))
        elif fmt == "rar":
            rarfile = require("rarfile")
            with rarfile.RarFile(path) as rf:
                rf.extractall(path=str(out_dir))
        else:
            raise BackendError(f"Cannot extract '.{fmt}'.")
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"Extraction failed: {exc}")
    files = [str(p) for p in out_dir.rglob("*") if p.is_file()]
    return {"output_dir": str(out_dir), "extracted": len(files),
            "outputs": files[:500]}


def _within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def _safe_extract_zip(zf: zipfile.ZipFile, out_dir: Path) -> None:
    """Extract a zip, skipping any member that would escape out_dir (Zip Slip)."""
    for member in zf.infolist():
        dest = out_dir / member.filename
        if not _within(out_dir, dest):
            continue
        zf.extract(member, out_dir)


def _safe_extract_tar(tf: tarfile.TarFile, out_dir: Path) -> None:
    for member in tf.getmembers():
        dest = out_dir / member.name
        if not _within(out_dir, dest):
            continue
        tf.extract(member, out_dir)


def op_create(path: str, options: dict, ctx: PluginContext) -> dict:
    paths = options.get("paths") or [path]
    to = (options.get("to_format") or "zip").lower()
    out = options.get("output_path") or str(Path(paths[0]).with_suffix(f".{to}"))
    ctx.progress(f"Creating {to} archive with {len(paths)} item(s)…")
    try:
        if to == "zip":
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for pth in paths:
                    p = Path(pth)
                    if p.is_dir():
                        for f in p.rglob("*"):
                            if f.is_file():
                                zf.write(f, f.relative_to(p.parent))
                    else:
                        zf.write(p, p.name)
        elif to in ("tar", "gz", "bz2"):
            mode = {"tar": "w", "gz": "w:gz", "bz2": "w:bz2"}[to]
            with tarfile.open(out, mode) as tf:
                for pth in paths:
                    tf.add(pth, arcname=Path(pth).name)
        else:
            raise BackendError(f"Cannot create '.{to}' archives (try zip/tar/gz/bz2).")
    except BackendError:
        raise
    except Exception as exc:
        raise BackendError(f"Archive creation failed: {exc}")
    return {"output_path": out, "items": len(paths), "to_format": to}


def register(reg) -> None:
    reg.register("archives", _ARCHIVE_FORMATS, [
        Capability("list", op_list, backend="native"),
        Capability("read", op_list, backend="native"),
        Capability("inspect", op_inspect, backend="native"),
        Capability("extract", op_extract, backend="native"),
        Capability("create", op_create, backend="native", batch=True),
    ])
