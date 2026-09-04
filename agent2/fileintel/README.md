<!--
Author: Aarav Shah
Portfolio: aaravshah1311.is-great.net
github: github.com/aaravshah1311
-->

# File Intelligence System (`agent2/fileintel/`)

A plugin-based subsystem that gives the agent real file understanding. It detects
a file's type, tells you which operations that type supports, and dispatches to
whichever backend can actually do the work — reading, summarising, converting,
analysing, extracting, OCR-ing, and searching a whole workspace.

**Three design goals, and the failure each one prevents:**

- **Adding a format is adding one file to `plugins/`.** Nothing in the core
  modules names a format, so a new plugin cannot regress an existing one.
- **Every third-party library is optional and imported lazily.** A missing
  library surfaces as a structured *"pip install X"* hint at the moment you
  invoke the operation, not as an `ImportError` while the package loads. Import a
  library at module scope instead and one absent wheel takes the whole file
  subsystem — and both agent loops that import it — down with it.
- **The router owns backend fallback.** The agent never receives a stack trace;
  it receives a result dict or a structured error with a `code` it can act on.

## At a glance

With **zero optional libraries installed**, the registry still comes up complete:

| Counted | Figure | How to count it |
|---------|--------|-----------------|
| Plugin files | **7** | `*.py` in `plugins/`, excluding `__init__.py` |
| Registration names | **11** | `REGISTRY.summary()["plugins"]` — a plugin may call `reg.register()` more than once |
| Canonical formats | **69** | `len(REGISTRY.known_formats())` |
| Format × operation pairs | **331** | `REGISTRY.summary()["operations"]` |
| Distinct operation names | **26** | the union of `operations_for(fmt)` over every format |
| `Capability` entries | **358** | every `(format, operation, backend)` triple — 331 pairs plus the extra backends behind the multi-backend ones |

Registration happens at import time and never touches a library, which is why
these figures do not move with your environment. An operation that needs a
missing library fails only when you actually call it.

---

## Architecture

```
User references a file
  → agent calls detect_file / file_capabilities / run_file_op / convert_file / search_workspace
  → tools.py shim: _safe_path() → the workspace sandbox
  → agent2.fileintel public API   (__init__.py: run_op, detect_file, …)
      → ToolExecutor.run_operation()        executor.py
          1. security.preflight()           traversal + size + executable checks
          2. detector.detect()              extension + magic-byte sniff → format
          3. OperationRouter.run()          resolve backends, try each in priority order
                → Capability.fn(path, options, ctx)   the plugin operation
          4. oplog.log_op()                 record a row in the file_ops table
          5. structured result {…, progress: [...], duration_ms}   never raises
```

`search_workspace` is the one tool that does **not** run that pipeline: there is
no single file to preflight or detect, so `search.search()` performs its own
workspace confinement and returns `{"error": …, "code": "unsafe_path"}` rather
than raising. It writes no op-log row, because it writes no files.

### Modules

| File | Role |
|------|------|
| `errors.py` | Typed exceptions. `FileIntelError` base (`.message`, `.hint`, `.code`, `.as_dict()`), then `MissingDependency`, `BackendError` — both **router-recoverable**, so they trigger fallback — plus `UnsupportedOperation`, `UnsupportedFormat`, `FileTooLarge`, `UnsafePath`. Codes: `file_error`, `missing_dependency`, `backend_error`, `unsupported_operation`, `unsupported_format`, `file_too_large`, `unsafe_path`, and `unexpected_error` from the executor's last-resort guard. |
| `base.py` | The plugin contract: the `Capability` dataclass, `PluginContext` (the DI carrier), the `OpFn` signature, and the lazy-import helpers `require()` / `require_binary()`. |
| `detector.py` | `detect(path)` — extension (plus 6 aliases) + magic bytes + ZIP-container disambiguation. Returns `format`, `category`, `mime`, `confidence` and a `mismatch` flag. **Never raises**, because it runs before every operation and an undetectable file must read as "unknown format", not as a failed turn. |
| `metadata.py` | `extract_metadata(path)` — name, size, dates, SHA-256, plus type-specific fields (image dimensions, PDF page count, media duration, CSV shape). Best-effort; hashing is skipped above `_HASH_LIMIT` (200 MB) so a huge file cannot stall a `detect_file` call. |
| `security.py` | `resolve_safe()`, `validate_size()`, `check_extension()` and the `preflight()` that runs all three. |
| `registry.py` | `CapabilityRegistry` — `format → operation → [Capability]`, each list kept sorted by `priority` (lowest first). Queries: `resolve()`, `operations_for()`, `capabilities_for()`, `formats_in_category()`, `known_formats()`, `summary()`. |
| `router.py` | `OperationRouter` — resolves the backend list and tries each in order. `MissingDependency` and `BackendError` fall through to the next backend; every other `FileIntelError` propagates unchanged, because "this format has no such operation" is an answer and retrying cannot improve it. Annotates the winning result with `_backend`. |
| `executor.py` | `ToolExecutor` — the pipeline above. Converts any `FileIntelError` into `{error, code, hint?, progress}` and catches everything else as `unexpected_error`. |
| `oplog.py` | History logger → the `file_ops` table (`tool`, `operation`, `path`, `output_paths`, `duration_ms`, `ok`, `error`), plus `recent()`. Written on success **and** on failure, so a run that produced nothing still leaves a trace. |
| `search.py` | `search()` — kinds `content` / `filename` / `recent` / `secrets` / `duplicates`, 11 natural-language preset phrases ("invoices", "TODOs", "API keys", …), capped at `_MAX_HITS` (300) and skipping `_SKIP_DIRS`. An unrecognised `kind` falls back to a content search rather than erroring. |
| `plugins/` | Auto-loaded format plugins (below). |

### The dependency-injection seam

Nothing in core reaches for a global. `OperationRouter` and `ToolExecutor` take a
`CapabilityRegistry` in their constructors, and every operation receives a
`PluginContext` carrying the workspace root, a `progress()` callback, an op-log
`logger` and an `output_dir`. `__init__.py` builds **one** default wired triple
(`REGISTRY` / `ROUTER` / `EXECUTOR`) for the tool layer; tests build their own
with fake capabilities and inject them, so a router assertion does not depend on
which optional libraries happen to be installed on the machine running it.

---

## Plugins and the capability matrix

Plugins live in `plugins/` and are auto-discovered: `plugins/__init__.py` walks
the directory with `pkgutil.iter_modules`, imports each module and calls its
module-level `register(registry)`. A module whose name starts with `_` is skipped;
an import or register failure is reported to stderr and skipped, so one broken
plugin never takes down the other six.

A plugin may register more than once — that is why 7 files produce 11
registration names. `documents.py` splits into `documents` (text and AI ops),
`documents-convert` (the three-backend convert chain) and `documents-pdf`
(PDF-only ops); `spreadsheets.py` splits into `spreadsheets` and
`spreadsheets-xlsx`; `media.py` registers `audio` and `video` against different
format sets. Splitting is how a plugin gives one operation a different backend
chain, or an operation to only some of its formats, without a conditional inside
the operation.

| Plugin file | Registers as | Formats | Pairs | Operations |
|-------------|--------------|---------|-------|------------|
| `documents.py` | `documents`, `documents-convert`, `documents-pdf` | 9 — pdf, docx, doc, odt, rtf, txt, md, html, epub | 76 | read, extract_text, summarize\*, translate\*, rewrite\*, grammar\*, compare, convert, merge, split, extract_images, ocr |
| `code.py` | `code` | 26 — py, js, jsx, ts, tsx, java, go, rs, c, h, cpp, hpp, cs, php, rb, sh, bat, ps1, sql, json, yaml, toml, ini, cfg, css, xml | 78 | read, analyze, metadata |
| `images.py` | `images` | 9 — png, jpg, webp, gif, bmp, tiff, svg, heic, ico | 54 | read, metadata, convert, compress, resize, ocr |
| `media.py` | `audio`, `video` | 11 — mp3, wav, flac, aac, m4a, ogg + mp4, mov, mkv, avi, webm | 49 | read, metadata, convert, extract_audio, transcribe |
| `archives.py` | `archives` | 6 — zip, tar, gz, bz2, 7z, rar | 30 | list, read, inspect, extract (Zip-Slip-safe), create |
| `spreadsheets.py` | `spreadsheets`, `spreadsheets-xlsx` | 5 — xlsx, xls, csv, ods, tsv | 26 | read, analyze, clean, formula_audit, export_csv, convert |
| `presentations.py` | `presentations` | 3 — pptx, ppt, odp | 18 | read, extract_text, speaker_notes, translate\*, rewrite\*, convert |
| | | **69** | **331** | **26 distinct** |

`jpeg`, `tif`, `yml`, `htm`, `tgz` and `mpeg` are **aliases**, not formats:
`detector.normalize_format()` folds them onto `jpg`, `tiff`, `yaml`, `html`, `gz`
and `mp4` before the registry ever sees them, so `.jpeg` and `.jpg` cannot end up
with different operation sets.

\* **AI operations** (summarize / translate / rewrite / grammar) are honest about
the division of labour: the plugin extracts the source text and hands it back
tagged `ai_task` with an `instruction`. The **agent** performs the language
transformation and writes the result. A plugin never calls a model, so there is
no second place where a model gets called, no second key to rotate, and no
"conversion" that quietly costs tokens.

### Optional libraries: `require()`, and why `run.py` carries a manifest

Every third-party library is resolved inside the operation, from a **string**,
through `importlib`:

```python
lib = require("pypdf")                       # → MissingDependency("pypdf") if absent
lib = require("docx", pip_name="python-docx")  # import name ≠ pip name
```

⚠️ **That string is invisible to any "grep for imports" audit**, and the bug it
caused is exactly why `run.py` keeps `FILEINTEL_PACKAGES` as a hand-maintained
list: five libraries — `xlrd`, `odfpy`, `rarfile`, `docx2pdf`, `pdf2image` — were
required by operations and absent from the manifest, so they were never
installed. The affected features reported *"pip install X"* forever, on a machine
whose setup had just announced it had installed everything, and nothing anywhere
was in an error state. `test_run_py_installs_every_fileintel_library` now walks
every `require(...)` call site under `agent2/fileintel/` and fails if the
manifest does not cover it, so adding a `require()` for a new library and
forgetting the manifest entry is a red test rather than a silent dead feature.

**Backends that need a system binary** are declared with `require_binary()`,
which resolves through `shutil.which` and raises `MissingDependency(kind="binary")`
with an OS-appropriate hint. There are exactly four:

| Binary | Used by |
|--------|---------|
| `tesseract` | `images.ocr`, and the OCR path a scanned PDF routes through |
| `ffprobe` | `media.metadata` |
| `ffmpeg` | `media.convert`, `media.extract_audio` |
| `whisper` | `media.transcribe` |

Everything else is a pip library, `docx2pdf` included — it drives Word through
COM, so `run.py` installs it on Windows only, and the DOCX→PDF path falls back
through reportlab elsewhere. `rarfile` is likewise a pip library; whether it can
open a given archive depends on an extraction helper it finds for itself, and
fileintel never asks for one by name.

---

## How to add a plugin

Adding a format is a single new file in `plugins/` — no edits to any core module.

1. **Create `plugins/myformat.py`.** Write each operation as a function with the
   signature `op(path, options, ctx) -> dict`. Return a JSON-serialisable dict on
   success and raise a typed error from `errors.py` on failure. Never let a raw
   third-party exception escape — wrap it in `BackendError`, or the router cannot
   tell "try the next backend" from "stop".

2. **Import third-party libraries lazily** inside the operation, via `require()`,
   and add the library to `run.py`'s `FILEINTEL_PACKAGES`:

   ```python
   from agent2.fileintel.base import Capability, PluginContext, require
   from agent2.fileintel.errors import BackendError

   def op_read(path: str, options: dict, ctx: PluginContext) -> dict:
       ctx.progress("Reading…")                 # streamed + collected into result["progress"]
       lib = require("some_lib", pip_name="some-lib")   # → MissingDependency if absent
       try:
           data = lib.load(path)
       except Exception as exc:
           raise BackendError(f"Could not read: {exc}")
       return {"text": data.text}
   ```

3. **Export `register(reg)`.** Declare the formats you handle and one
   `Capability` per operation. Use `backend` and `priority` when more than one
   backend can perform the same operation — the router tries the lowest priority
   first and falls through on `MissingDependency` / `BackendError`:

   ```python
   def register(reg) -> None:
       reg.register("myformat", ["mf", "myf"], [
           Capability("read", op_read, backend="some-lib"),
           Capability("convert", op_convert_fast, backend="fast", priority=10),
           Capability("convert", op_convert_fallback, backend="pure-python", priority=50),
       ])
   ```

   The shipped example is `documents-convert`: `docx2pdf` at priority 10 for
   fidelity, `reportlab` at 50 as the universal renderer, `native-text` at 60 for
   text targets. On a machine with no Word, the first raises `MissingDependency`,
   the router moves on, and the user gets a PDF instead of an error.

That is the whole change. The auto-loader picks the module up on next import,
`detect_file` lists the new operations, and both agent loops can call them
through the existing `run_file_op` tool. No edits to `tools.py`, `agent.py`,
`llm/provider_agent.py` or the router.

### Testing a new plugin

Add pure-Python tests to `.github/tests/test_fileintel.py`. Prefer stdlib-only
operation paths — no network, no heavy binaries. For router and fallback
behaviour, build a `CapabilityRegistry` with fake capabilities so the assertion
is independent of which optional libraries are installed:

```bash
python -m pytest .github/tests/test_fileintel.py -v
```

---

## Agent-facing tools

Five tools are wired into both agent loops (Gemini and custom providers). Each is
a thin router-dispatched shim: the caller names an **operation**, never a
library, so there is no tool-per-format explosion and no tool that goes stale
when a backend is swapped underneath it.

| Tool | Purpose | Capability |
|------|---------|-----------|
| `detect_file(path)` | Type + rich metadata + the operations available for this file. Call it first. | `read` |
| `file_capabilities(path_or_category)` | Operations for a file path **or** one of the 8 category names (documents, spreadsheets, presentations, images, audio, video, archives, code). | `read` |
| `run_file_op(path, operation, options?)` | The universal dispatcher — the workhorse. | `fs.write` |
| `convert_file(path, to_format, output_path?)` | Convenience wrapper onto the `convert` operation. | `fs.write` |
| `search_workspace(query, path?, kind?)` | Content / filename / recent / secrets / duplicates search. | `read` |

`run_file_op` and `convert_file` map to `fs.write` in `core/permissions.py`
because either one can create a file; the other three are reads. The gate lives
in `tools.dispatch_tool` and returns a tool `error`, never an exception — the
model reads errors and adapts.

**Progress streaming.** `ToolExecutor` accepts an `on_progress` callback, and
every operation emits human-readable steps ("Reading… / Converting… /
Completed."). They are collected into the result's `progress` list, which
`agent.py`'s `_tool_result_summary` renders. True per-step Socket.IO streaming is
a documented extension point: the callback is the seam, and wiring it through
`dispatch_tool` would break Gemini/provider parity, so it is deliberately out of
scope.

### One blind spot worth knowing

⚠️ **A file written by a fileintel operation produces no diff.** The diff engine
snapshots a file before a *file-writing tool* runs (`core/diffs.capture_for()`,
which names this case), and `run_file_op` / `convert_file` reach disk through a
plugin backend instead. A conversion, an OCR pass or an archive extraction is
therefore absent from the Ctrl+B viewer and from the web diff panel, and the
op-log `file_ops` row is the only record that it happened. Route a change through
`write_file` or `multi_edit_files` when the user needs to see it.

## Security

- **Path traversal, twice.** `tools._safe_path()` puts every shim through
  `workspace.validate_path` first, and `security.preflight()` independently
  resolves the real path and rejects anything that escapes the supplied root
  (`UnsafePath`). The second check is not redundant: the public API is callable
  without the tool layer, and a check that only exists in a caller is a check a
  new caller forgets.
- **Size limit** — `config.MAX_FILE_SIZE` is enforced *before* any read
  (`FileTooLarge`), so a pathological file cannot be loaded into memory first and
  rejected second.
- **Executable block** — `config.BLOCKED_EXTENSIONS` (exe, dll, so, msi, …) are
  refused unless the caller passes `allow_executable=True`. Script *source*
  (js, sh, bat, ps1) is deliberately **not** blocked, because the agent's job
  includes reading it as code.
- **Content versus extension** — `detect()` cross-checks magic bytes against the
  extension and flags a `mismatch` (a PNG named `.txt`), and disambiguates the
  seven ZIP-container formats — docx, xlsx, pptx, odt, ods, odp, epub — by
  looking inside. It reports the mismatch rather than acting on it, so the caller
  decides.
- **No shell injection** — every subprocess backend (ffmpeg, ffprobe, tesseract,
  whisper) is invoked with an argv list, never a shell string. Archive
  extraction is Zip-Slip-safe.

## Configuration

| Constant (`agent2/config.py`) | Env override | Default |
|-------------------------------|--------------|---------|
| `MAX_FILE_SIZE` | `AGENT2_MAX_FILE_SIZE` | `104857600` (100 MB) |
| `FILEINTEL_OUTPUT_DIR` | `AGENT2_FILEINTEL_OUTPUT` | `""` — output lands beside the source |
| `BLOCKED_EXTENSIONS` | — | exe, dll, so, msi, … |

Python libraries are listed in `requirements.txt` and installed by `run.py` from
`FILEINTEL_PACKAGES`. Every one is in `OPTIONAL_IMPORTS`, so a wheel that fails
to build warns and setup continues — each operation degrades to an install hint
at runtime, and aborting a whole install over an optional OCR bridge would be a
worse outcome than a feature that says what it needs.
