# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/fileintel/plugins/presentations.py
─────────────────────────────────────────
Presentations plugin: PPTX, PPT, ODP.

Operations
  read / extract_text ... slide text + titles
  speaker_notes ......... per-slide speaker notes
  translate / rewrite ... AI ops (plugin extracts text; agent transforms)
  convert ............... → PDF / TXT

PPTX uses python-pptx (lazy). PPT (legacy binary) and ODP degrade gracefully with
a clear "convert to .pptx first" hint. This plugin ships now so the format is
recognised and the capability surface is complete; heavier fidelity backends can
be added later without touching core.
"""

from __future__ import annotations

from pathlib import Path

from agent2.fileintel.base import Capability, PluginContext, require
from agent2.fileintel.errors import BackendError, UnsupportedFormat

_PRES_FORMATS = ["pptx", "ppt", "odp"]


def _require_pptx(path: str):
    from agent2.fileintel.detector import detect
    fmt = detect(path)["format"]
    if fmt != "pptx":
        raise UnsupportedFormat(
            f"'.{fmt}' presentations aren't supported directly.",
            hint="Convert to .pptx first (e.g. via LibreOffice) then retry.")
    pptx = require("pptx", "python-pptx")
    return pptx.Presentation(path)


def op_read(path: str, options: dict, ctx: PluginContext) -> dict:
    ctx.progress("Reading slides…")
    prs = _require_pptx(path)
    slides = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [
            shape.text_frame.text
            for shape in slide.shapes
            if shape.has_text_frame
        ]
        slides.append({"slide": i, "text": "\n".join(t for t in texts if t)})
    full = "\n\n".join(f"[Slide {s['slide']}]\n{s['text']}" for s in slides)
    return {"slide_count": len(slides), "slides": slides,
            "text": full[:200_000], "chars": len(full)}


def op_speaker_notes(path: str, options: dict, ctx: PluginContext) -> dict:
    ctx.progress("Extracting speaker notes…")
    prs = _require_pptx(path)
    notes = []
    for i, slide in enumerate(prs.slides, 1):
        if slide.has_notes_slide:
            tf = slide.notes_slide.notes_text_frame
            if tf and tf.text.strip():
                notes.append({"slide": i, "notes": tf.text.strip()})
    return {"slides_with_notes": len(notes), "notes": notes}


def _ai_op(task: str):
    def _fn(path: str, options: dict, ctx: PluginContext) -> dict:
        data = op_read(path, options, ctx)
        instr = ("Translate this presentation's text to "
                 f"{options.get('target_language', 'the requested language')}."
                 if task == "translate"
                 else "Rewrite/improve this presentation's text per the user's request.")
        return {"ai_task": task, "instruction": instr,
                "text": data["text"], "slide_count": data["slide_count"]}
    return _fn


def op_convert(path: str, options: dict, ctx: PluginContext) -> dict:
    to = (options.get("to_format") or "pdf").lower()
    data = op_read(path, options, ctx)
    if to == "txt":
        out = options.get("output_path") or str(Path(path).with_suffix(".txt"))
        Path(out).write_text(data["text"], encoding="utf-8")
        return {"output_path": out, "to_format": "txt"}
    if to == "pdf":
        rl = require("reportlab.platypus", "reportlab")
        styles_mod = require("reportlab.lib.styles", "reportlab")
        from reportlab.lib.pagesizes import LETTER  # type: ignore
        import html as _h
        ctx.progress("Rendering slides → PDF…")
        out = options.get("output_path") or str(Path(path).with_suffix(".pdf"))
        styles = styles_mod.getSampleStyleSheet()
        doc = rl.SimpleDocTemplate(out, pagesize=LETTER)
        flow = []
        for s in data["slides"]:
            flow.append(rl.Paragraph(f"<b>Slide {s['slide']}</b>", styles["Heading2"]))
            flow.extend(
                rl.Paragraph(_h.escape(line) or " ", styles["Normal"])
                for line in s["text"].split("\n")
            )
            flow.append(rl.Spacer(1, 12))
        doc.build(flow)
        return {"output_path": out, "to_format": "pdf"}
    raise BackendError(f"No presentation conversion path to '{to}'.")


def register(reg) -> None:
    reg.register("presentations", _PRES_FORMATS, [
        Capability("read", op_read, backend="python-pptx"),
        Capability("extract_text", op_read, backend="python-pptx"),
        Capability("speaker_notes", op_speaker_notes, backend="python-pptx"),
        Capability("translate", _ai_op("translate"), backend="ai"),
        Capability("rewrite", _ai_op("rewrite"), backend="ai"),
        Capability("convert", op_convert, backend="native"),
    ])
