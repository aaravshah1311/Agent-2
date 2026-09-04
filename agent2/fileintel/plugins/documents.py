# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/plugins/documents.py
─────────────────────────────────────
Documents plugin: PDF, DOCX, DOC, ODT, RTF, TXT, MD, HTML, EPUB.

Operations
  read / extract_text ......... pull plain text out of any document
  metadata .................... document-specific metadata
  merge (PDF, batch) .......... combine several PDFs into one
  split (PDF) ................. split a PDF into per-page files
  extract_images (PDF) ........ dump embedded images
  convert ..................... to PDF / TXT (backend fallback: docx2pdf → reportlab)
  compare ..................... unified diff of two documents' text
  ocr ......................... route a scanned PDF through the OCR pipeline
  summarize / translate / rewrite / grammar (AI ops)
        The plugin extracts the text and hands it back tagged `ai_task`; the
        agent performs the language transformation and writes the result. This
        keeps "who does the LLM work" honest — plugins never call a model.

Every third-party import is lazy (via base.require) so this module loads with
zero optional libraries installed.
"""

from __future__ import annotations

import difflib
import html as _html
import re
from pathlib import Path

from agent2.fileintel.base import Capability, PluginContext, require
from agent2.fileintel.errors import BackendError, UnsupportedFormat

_DOC_FORMATS = ["pdf", "docx", "doc", "odt", "rtf", "txt", "md", "html", "epub"]
_MAX_TEXT = 200_000  # cap returned text so we never blow the context window


# ── Text extraction (per format) ─────────────────────────────────────────────────

def _read_txt(p: Path) -> str:
    try:
        from charset_normalizer import from_path  # type: ignore
        best = from_path(str(p)).best()
        if best:
            return str(best)
    except Exception:
        pass
    return p.read_text(encoding="utf-8", errors="replace")


def _read_md(p: Path) -> str:
    # Markdown is already readable text; return as-is.
    return _read_txt(p)


def _read_html(p: Path) -> str:
    raw = _read_txt(p)
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(raw, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    except Exception:
        # stdlib fallback: strip tags crudely
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw)
        text = re.sub(r"(?s)<[^>]+>", "", text)
        return _html.unescape(re.sub(r"\n{3,}", "\n\n", text)).strip()


def _read_pdf(p: Path) -> str:
    pypdf = require("pypdf")
    try:
        reader = pypdf.PdfReader(str(p))
        parts = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(parts).strip()
    except Exception as exc:
        raise BackendError(f"Could not read PDF: {exc}")


def _read_docx(p: Path) -> str:
    docx = require("docx", "python-docx")
    try:
        d = docx.Document(str(p))
        lines = [para.text for para in d.paragraphs]
        for table in d.tables:
            lines.extend("\t".join(c.text for c in row.cells) for row in table.rows)
        return "\n".join(lines).strip()
    except Exception as exc:
        raise BackendError(f"Could not read DOCX: {exc}")


def _read_rtf(p: Path) -> str:
    raw = _read_txt(p)
    # Minimal RTF → text: drop control words and groups.
    text = re.sub(r"\\'[0-9a-fA-F]{2}", "", raw)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_zipdoc(p: Path, kind: str) -> str:
    """ODT / EPUB: pull text out of the XML/HTML inside the zip container."""
    import zipfile
    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            if kind == "odt" and "content.xml" in names:
                raw = zf.read("content.xml").decode("utf-8", "replace")
                text = re.sub(r"(?s)<[^>]+>", " ", raw)
                return _html.unescape(re.sub(r"\s{2,}", " ", text)).strip()
            if kind == "epub":
                chunks = []
                for n in names:
                    if n.lower().endswith((".xhtml", ".html", ".htm")):
                        raw = zf.read(n).decode("utf-8", "replace")
                        raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw)
                        chunks.append(_html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw)))
                return re.sub(r"\s{2,}", " ", "\n\n".join(chunks)).strip()
    except Exception as exc:
        raise BackendError(f"Could not read {kind.upper()}: {exc}")
    raise BackendError(f"Unsupported {kind.upper()} layout")


_EXTRACTORS = {
    "txt": _read_txt, "md": _read_md, "html": _read_html,
    "pdf": _read_pdf, "docx": _read_docx, "rtf": _read_rtf,
    "odt": lambda p: _read_zipdoc(p, "odt"),
    "epub": lambda p: _read_zipdoc(p, "epub"),
}


def _extract_text(path: str) -> tuple[str, str]:
    """Return (text, format). Raises for formats without an extractor (doc)."""
    from agent2.fileintel.detector import detect
    fmt = detect(path)["format"]
    fn = _EXTRACTORS.get(fmt)
    if not fn:
        if fmt == "doc":
            raise UnsupportedFormat(
                "Legacy .doc is not supported directly.",
                hint="Convert to .docx first (e.g. via LibreOffice) then retry.")
        raise UnsupportedFormat(f"No text extractor for '.{fmt}'.")
    return fn(Path(path)), fmt


# ── Operations ───────────────────────────────────────────────────────────────────

def op_read(path: str, options: dict, ctx: PluginContext) -> dict:
    ctx.progress("Extracting text…")
    text, fmt = _extract_text(path)
    truncated = len(text) > _MAX_TEXT
    return {"format": fmt, "text": text[:_MAX_TEXT], "chars": len(text),
            "truncated": truncated}


def _ai_op(task: str):
    """Factory for AI ops: extract text, hand it to the agent to transform."""
    def _fn(path: str, options: dict, ctx: PluginContext) -> dict:
        ctx.progress("Extracting text for the language model…")
        text, fmt = _extract_text(path)
        instr = {
            "summarize": "Summarize the following document content for the user.",
            "translate": f"Translate the following content to "
                         f"{options.get('target_language', 'the requested language')}. "
                         "Preserve structure. Then save with write_file if asked.",
            "rewrite":   "Rewrite/improve the following content per the user's request, "
                         "preserving meaning and structure.",
            "grammar":   "Correct grammar and spelling in the following content, "
                         "preserving meaning and formatting.",
        }[task]
        return {"ai_task": task, "format": fmt, "instruction": instr,
                "text": text[:_MAX_TEXT], "chars": len(text)}
    return _fn


def op_pdf_merge(path: str, options: dict, ctx: PluginContext) -> dict:
    pypdf = require("pypdf")
    paths = options.get("paths") or [path]
    if len(paths) < 2:
        raise BackendError("merge needs at least 2 PDFs via options.paths")
    out = options.get("output_path") or str(Path(paths[0]).with_name("merged.pdf"))
    ctx.progress(f"Merging {len(paths)} PDFs…")
    try:
        writer = pypdf.PdfWriter()
        for pth in paths:
            reader = pypdf.PdfReader(str(pth))
            for pg in reader.pages:
                writer.add_page(pg)
        ctx.progress("Saving merged PDF…")
        with open(out, "wb") as fh:
            writer.write(fh)
    except Exception as exc:
        raise BackendError(f"PDF merge failed: {exc}")
    return {"output_path": out, "merged": len(paths)}


def op_pdf_split(path: str, options: dict, ctx: PluginContext) -> dict:
    pypdf = require("pypdf")
    ctx.progress("Splitting PDF pages…")
    out_dir = Path(options.get("output_dir") or ctx.output_dir or Path(path).parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(path).stem
    created: list[str] = []
    try:
        reader = pypdf.PdfReader(str(path))
        for i, page in enumerate(reader.pages, 1):
            writer = pypdf.PdfWriter()
            writer.add_page(page)
            dest = out_dir / f"{stem}_p{i}.pdf"
            with open(dest, "wb") as fh:
                writer.write(fh)
            created.append(str(dest))
    except Exception as exc:
        raise BackendError(f"PDF split failed: {exc}")
    return {"outputs": created, "pages": len(created)}


def op_pdf_extract_images(path: str, options: dict, ctx: PluginContext) -> dict:
    pypdf = require("pypdf")
    ctx.progress("Extracting embedded images…")
    out_dir = Path(options.get("output_dir") or ctx.output_dir or Path(path).parent)
    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    try:
        reader = pypdf.PdfReader(str(path))
        for pi, page in enumerate(reader.pages, 1):
            for img in getattr(page, "images", []):
                dest = out_dir / f"{Path(path).stem}_p{pi}_{img.name}"
                with open(dest, "wb") as fh:
                    fh.write(img.data)
                created.append(str(dest))
    except Exception as exc:
        raise BackendError(f"Image extraction failed: {exc}")
    return {"outputs": created, "images": len(created)}


def op_compare(path: str, options: dict, ctx: PluginContext) -> dict:
    other = options.get("other") or options.get("compare_to")
    if not other:
        raise BackendError("compare needs options.other = path to the second document")
    ctx.progress("Extracting both documents…")
    a, _ = _extract_text(path)
    b, _ = _extract_text(other)
    ctx.progress("Diffing…")
    diff = list(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile=Path(path).name, tofile=Path(other).name, lineterm=""))
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return {"similarity": round(ratio, 4),
            "diff": "\n".join(diff[:2000]),
            "changed_lines": sum(1 for d in diff if d[:1] in "+-" and d[:2] not in ("++", "--"))}


def op_ocr(path: str, options: dict, ctx: PluginContext) -> dict:
    """Route scanned-PDF OCR through the images plugin's OCR backend."""
    from agent2.fileintel.plugins.images import ocr_pdf
    return ocr_pdf(path, options, ctx)


# ── Conversion (backend fallback lives in the registry) ──────────────────────────

def op_convert_to_pdf_reportlab(path: str, options: dict, ctx: PluginContext) -> dict:
    """Universal text→PDF via reportlab (works for txt/md/html/docx/rtf/odt/epub)."""
    to = (options.get("to_format") or "pdf").lower()
    if to != "pdf":
        # Non-PDF targets handled by op_convert_text below via router ordering.
        raise BackendError(f"reportlab backend only targets PDF, not '{to}'.")
    reportlab_platypus = require("reportlab.platypus", "reportlab")
    styles_mod = require("reportlab.lib.styles", "reportlab")
    ctx.progress("Extracting source text…")
    text, _ = _extract_text(path)
    out = options.get("output_path") or str(Path(path).with_suffix(".pdf"))
    ctx.progress("Rendering PDF…")
    try:
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        SimpleDocTemplate = reportlab_platypus.SimpleDocTemplate
        Paragraph = reportlab_platypus.Paragraph
        Spacer = reportlab_platypus.Spacer
        styles = styles_mod.getSampleStyleSheet()
        doc = SimpleDocTemplate(out, pagesize=LETTER)
        flow = []
        for para in text.split("\n"):
            safe = _html.escape(para) or " "
            flow.append(Paragraph(safe, styles["Normal"]))
            flow.append(Spacer(1, 4))
        doc.build(flow)
    except Exception as exc:
        raise BackendError(f"reportlab PDF render failed: {exc}")
    return {"output_path": out, "to_format": "pdf"}


def op_convert_docx_to_pdf_native(path: str, options: dict, ctx: PluginContext) -> dict:
    """High-fidelity DOCX→PDF via docx2pdf (needs Word/LibreOffice)."""
    if (options.get("to_format") or "pdf").lower() != "pdf":
        raise BackendError("docx2pdf backend only targets PDF.")
    d2p = require("docx2pdf")
    out = options.get("output_path") or str(Path(path).with_suffix(".pdf"))
    ctx.progress("Converting DOCX→PDF (native)…")
    try:
        d2p.convert(str(path), out)
    except Exception as exc:
        raise BackendError(f"docx2pdf failed: {exc}")
    return {"output_path": out, "to_format": "pdf"}


def op_convert_text(path: str, options: dict, ctx: PluginContext) -> dict:
    """Text-target conversions: <any doc> → txt, and md → html."""
    to = (options.get("to_format") or "txt").lower()
    text, fmt = _extract_text(path)
    if to == "txt":
        out = options.get("output_path") or str(Path(path).with_suffix(".txt"))
        ctx.progress("Writing text…")
        Path(out).write_text(text, encoding="utf-8")
        return {"output_path": out, "to_format": "txt"}
    if to == "html" and fmt == "md":
        md = require("markdown")
        out = options.get("output_path") or str(Path(path).with_suffix(".html"))
        Path(out).write_text(md.markdown(text), encoding="utf-8")
        return {"output_path": out, "to_format": "html"}
    raise BackendError(f"No text-conversion path from '.{fmt}' to '{to}'.")


# ── Registration ─────────────────────────────────────────────────────────────────

def register(reg) -> None:
    text_ops = [
        Capability("read", op_read, backend="native", description="Extract plain text"),
        Capability("extract_text", op_read, backend="native"),
        Capability("summarize", _ai_op("summarize"), backend="ai"),
        Capability("translate", _ai_op("translate"), backend="ai"),
        Capability("rewrite", _ai_op("rewrite"), backend="ai"),
        Capability("grammar", _ai_op("grammar"), backend="ai"),
        Capability("compare", op_compare, backend="difflib"),
    ]
    reg.register("documents", _DOC_FORMATS, text_ops)

    # Conversion: docx2pdf first (fidelity), reportlab as universal fallback,
    # text conversions last. Router tries them in priority order.
    reg.register("documents-convert", _DOC_FORMATS, [
        Capability("convert", op_convert_docx_to_pdf_native, backend="docx2pdf", priority=10),
        Capability("convert", op_convert_to_pdf_reportlab, backend="reportlab", priority=50),
        Capability("convert", op_convert_text, backend="native-text", priority=60),
    ])

    # PDF-only operations.
    reg.register("documents-pdf", ["pdf"], [
        Capability("merge", op_pdf_merge, backend="pypdf", batch=True),
        Capability("split", op_pdf_split, backend="pypdf"),
        Capability("extract_images", op_pdf_extract_images, backend="pypdf"),
        Capability("ocr", op_ocr, backend="tesseract"),
    ])
