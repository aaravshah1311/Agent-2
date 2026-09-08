# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/projectscan.py
──────────────────────────
THE project analyser — one declaration of "what IS this project". Read by `/init`
(Task 29), by `GET /api/project`, and by Tasks 30/31 when they write
`.agent2/agent2.md` out of the same answer a human just read on their terminal.

Agent2 already had *file* intelligence (`fileintel` — what format is this ONE
file), *repository* intelligence (`core/gitstate.py` — what does `git` say about
this directory) and a *content dumper* (`tools.scan_project` — paste the tree and
the file bodies into the prompt). None of them answers the question `/init` asks:
what language is this project written in, what installs it, what runs its tests,
where does it start, and what conventions does it already follow.

⚠️ IT OWNS THE ANALYSIS, NOT THE FACTS IT ANALYSES.
Every input comes from the module that already declares it, so nothing here can
drift out of step with the rest of the app:

  * git            → `core.gitstate.snapshot()` / `describe()`. ⚠️ NEVER a second
                     `subprocess.run(["git", …])` — `gitstate._run` is the only
                     one in the package, it is the only one with a timeout, and it
                     is the only one that caches. A `git` fork here would be three
                     more per `/init`, uncached and unbounded.
  * the ignore set → `core.workspace.SKIP_DIRS` ("kept here so tools and search
                     share one definition"). ⚠️ There are already four copies of
                     that set in this build; this module adds no fifth, and it
                     fixes none of them either, because Task 29 says *do not
                     modify unrelated files*.
  * project docs   → `core.broker.PROJECT_DOCS` / `PRIMARY_DOC`. The broker
                     decides which documents a project HAS; `/init` only reports
                     what it found there.
  * the root       → `core.workspace.root()`. There is deliberately **no path
                     argument** on `/init` or on `GET /api/project`: the workspace
                     root is the security boundary, so a scan that cannot be
                     pointed anywhere else has no containment question to get
                     wrong. `root=` exists for tests and for Tasks 30/31.

What this module DOES own, because nothing else declares it: `LANGUAGES` (the
extension→language census), `PACKAGE_MANAGERS`, `FRAMEWORKS`, `ENTRY_HINTS`,
`CONFIG_FILES`, `COMMENT_SYNTAX` (what a comment looks like in each language) and
the shape of the result.

⚠️ IT READS THE PROJECT'S OWN COMMENTS, AND COMMENTS ARE ALL THAT COME BACK.
A census cannot tell you what `agent2/core/broker/__init__.py` is *for*; its own
docstring can, in the author's words, in one line. So `_notes` opens a few
important files — entry points first, then module roots, then root-level source,
then the largest, then tests — and keeps the leading docstring or comment block of
each, with the reason it was chosen (`why`) and the cap that engaged
(`truncated`) attached to every item.

⚠️ `_leading_comment()` IS THE PRIVACY BOUNDARY, AND IT IS A SYNTAX GATE, NOT A
HEURISTIC. Every branch returns bytes that sat inside a comment or a docstring,
and a file whose extension is not in `COMMENT_SYNTAX` returns `""` rather than the
head of the file. That is what makes it safe for `projectdoc._evidence()` to put
these notes in the one string `/init` sends to a model: its rule is *nothing but
prose a human wrote for a reader*, a comment is exactly that, and a statement is
not. Sampling the first N lines of an unknown format and calling it a comment
would have broken that rule silently, in the direction that leaks code.
⚠️ `config.INIT_NOTE_FILES=0` (`AGENT2_INIT_NOTE_FILES`) skips the step entirely —
the one `/init` ceiling whose zero is a feature, because it is the only part of
the scan that is prose rather than a count.

⚠️ THE LANGUAGE TABLE IS NOT `fileintel.plugins.code._LANG`, AND THAT IS NOT A
DUPLICATE. `_LANG` answers "what format is this one file" for the file-operations
router — it is keyed off the detector's format string and it deliberately counts
`json`, `yaml`, `toml`, `ini` and `cfg` as first-class formats, because reading one
of those IS a file operation. This table answers "what is this project WRITTEN
in", where the same rows would be actively wrong: a repository with 300 fixture
JSONs and 40 Python files is not a JSON project. So data, docs and source are
three separate buckets here (`DATA_EXTS`, `DOC_EXTS`, `LANGUAGES`) and deriving
either table from the other would force one of the two questions to be answered
wrongly. Both spellings are stated once, each in the module that asks.

⚠️ ONE BOUNDED WALK, AND THE TRUNCATION IS A FIELD.
`config.INIT_MAX_FILES`, `INIT_MAX_DEPTH` and `INIT_SCAN_BUDGET_SEC` cap the walk,
and when a cap engages the result says so (`truncated` + `truncated_by`). That is
the one thing `tools.scan_project` does not do — it silently cuts at 300 000
characters and reports a tree that looks complete — and a *description of a
project* that quietly stopped early is worse than one that says "I only saw part
of this", because Task 31 is going to write the incomplete answer into a file and
every later turn will read it as fact.

⚠️ THIS MODULE IS READ-ONLY, LIKE `gitstate` — BUT `/init` IS NOT.
`/init` is Claude-Code's `/init`: it analyses the workspace and then creates
`.agent2/` with `agent2.md` in it. The write half lives in `core/projectdoc.py`
and the split is deliberate, not a phase artefact: the analysis is what
`GET /api/project`, a dry run and every test need, and it must be callable
without a filesystem consequence. So nothing here writes, creates or deletes
anything, and the `agent2` block in the result (`present`, `doc`, `doc_chars`,
`skills`, `workflows`) is the seam `projectdoc` reads — never a place it stores
state. ⚠️ For the same reason this module never calls `gitstate.invalidate()`:
the reader does not invalidate, the writer does (see `projectdoc.apply()`).

⚠️ NOTHING HERE MAY RAISE. Every step runs inside its own guard (`_step`), a
failure lands in `errors` and the remaining steps still run — the same "one guard
per collector, never one around the loop" rule `core/broker/__init__.py` carries,
for the same reason: an unreadable `package.json` must not cost the user the
language census, the git state and the test layout. Hence the written BLE001/S110
exemption in `pyproject.toml`.

⚠️ GIT PARTIAL SUCCESS IS REPORTED AS *UNKNOWN*, NEVER AS *CLEAN*.
`gitstate._read` returns as soon as one of its three subprocesses succeeds, so
`repo=True` with an empty branch and no commits is indistinguishable from a clean
repository — and "clean" is the reading that would let `/init` write "no
uncommitted changes" into `agent2.md` while a tree full of them sat on disk. The
`git.unknown` flag exists so both surfaces say "unknown" instead. For the same
reason `git.changed` is `snapshot()["dirty"]` — the count of changed *paths* —
and never `staged + unstaged + untracked`, which double-counts a path that is
both staged and modified.

Layer: config / workspace / gitstate / broker → projectscan → cli.render · routes.
"""

import json
import os
import re
import time
from pathlib import Path

from agent2 import config


# ── What counts as a language ─────────────────────────────────────────────────
# Extension (lowercased, no dot) → language name. SOURCE and markup only; see the
# docstring for why data formats are a separate bucket rather than more rows here.
LANGUAGES: dict[str, str] = {
    "py": "Python", "pyi": "Python", "pyw": "Python", "pyx": "Cython",
    "js": "JavaScript", "mjs": "JavaScript", "cjs": "JavaScript", "jsx": "JavaScript",
    "ts": "TypeScript", "tsx": "TypeScript", "mts": "TypeScript", "cts": "TypeScript",
    "java": "Java", "kt": "Kotlin", "kts": "Kotlin", "scala": "Scala", "groovy": "Groovy",
    "go": "Go", "rs": "Rust", "zig": "Zig", "nim": "Nim", "cr": "Crystal",
    "c": "C", "h": "C/C++ header", "hpp": "C++ header", "hh": "C++ header",
    "hxx": "C++ header", "cpp": "C++", "cc": "C++", "cxx": "C++", "ino": "C++",
    "cs": "C#", "fs": "F#", "vb": "Visual Basic",
    "php": "PHP", "rb": "Ruby", "pl": "Perl", "pm": "Perl", "lua": "Lua", "tcl": "Tcl",
    "swift": "Swift", "m": "Objective-C", "mm": "Objective-C++", "dart": "Dart",
    "ex": "Elixir", "exs": "Elixir", "erl": "Erlang", "hrl": "Erlang",
    "hs": "Haskell", "ml": "OCaml", "mli": "OCaml",
    "clj": "Clojure", "cljs": "ClojureScript", "scm": "Scheme", "rkt": "Racket",
    "lisp": "Lisp", "el": "Emacs Lisp",
    "r": "R", "jl": "Julia", "f90": "Fortran", "f95": "Fortran",
    "pas": "Pascal", "asm": "Assembly",
    "sh": "Shell", "bash": "Shell", "zsh": "Shell", "fish": "Shell",
    "ps1": "PowerShell", "psm1": "PowerShell", "bat": "Batch", "cmd": "Batch",
    "sql": "SQL", "graphql": "GraphQL", "gql": "GraphQL", "proto": "Protocol Buffers",
    "html": "HTML", "htm": "HTML", "css": "CSS", "scss": "SCSS", "sass": "Sass",
    "less": "Less", "vue": "Vue", "svelte": "Svelte", "astro": "Astro",
    "tf": "Terraform", "hcl": "HCL", "nix": "Nix", "sol": "Solidity",
    "elm": "Elm", "ipynb": "Jupyter Notebook",
}

# Files whose *name* is the signal — there is no extension to key off.
LANGUAGE_FILENAMES: dict[str, str] = {
    "dockerfile": "Dockerfile", "containerfile": "Dockerfile",
    "makefile": "Make", "gnumakefile": "Make", "cmakelists.txt": "CMake",
    "jenkinsfile": "Groovy", "rakefile": "Ruby", "gemfile": "Ruby",
    "vagrantfile": "Ruby", "brewfile": "Ruby",
}

# Counted, reported, and never called a language.
DATA_EXTS = frozenset({
    "json", "yaml", "yml", "toml", "ini", "cfg", "conf", "env", "properties",
    "csv", "tsv", "xml", "plist", "lock", "db", "sqlite", "sqlite3",
})
DOC_EXTS = frozenset({"md", "markdown", "rst", "txt", "adoc", "asciidoc", "org"})

# ⚠️ Dot-directories are tooling state by default, WITH A DECLARED ALLOWLIST.
# A blanket "skip everything starting with a dot" is what a scanner reaches for,
# and in this very repository it would hide `.github/tests/` — the entire test
# suite — so `/init` would report a project with no tests. Everything on this list
# is project content that conventionally lives behind a dot.
#
# ⚠️ `.agent2` IS DELIBERATELY NOT ON IT, and that is the one entry worth a
# paragraph: it is not the project, it is what `/init` itself wrote. Walking it
# makes the scan measure its own output — the doc counted its own `agent2.md` and
# `skills/README.md`, so a five-file project was reported as seven files in five
# directories, `.agent2/` appeared in the generated `## Architecture` as if a user
# had designed it, and the *second* `/init` therefore reported four sections
# "refreshed" for a project nobody had touched. The facts a human actually wants
# from that folder (is it there, is there a doc, how many skills and workflows) are
# reported by the `agent2` seam in `_docs`, which reads the directory directly —
# so this costs nothing and keeps a re-scan idempotent. Phase 11's skill discovery
# reads `.agent2/skills/` itself and never asks this walk.
KEEP_DOT_DIRS = frozenset({
    ".github", ".gitlab", ".circleci", ".devcontainer", ".config",
})

# ── Presentation caps ─────────────────────────────────────────────────────────
# The walk is bounded by `config`; these bound the *report*, so one enormous
# project cannot produce a payload no terminal can print and no prompt can hold.
MAX_LANGUAGES = 10
MAX_DIRS = 14
MAX_ENTRY_POINTS = 10
MAX_CONFIG_FILES = 16
MAX_CONVENTIONS = 10
MAX_COMMANDS_PER_KIND = 6
MAX_FRAMEWORKS = 14
MAX_TEST_DIRS = 6
MAX_ROOT_PROBE = 40     # root source files opened to look for an entry guard
MAX_INDENT_SAMPLE = 12  # source files sampled for the indentation convention


# ── What a file says about ITSELF ─────────────────────────────────────────────
# Extension → comment family. ⚠️ AN EXTENSION THAT IS NOT HERE IS SKIPPED, NEVER
# GUESSED AT. Reading the first lines of an unknown format and calling whatever
# comes back "a comment" is how a note ends up being a line of code, or a chunk of
# a minified bundle, presented to the model as the author's own description. There
# are three families and each one is a *syntax*, not a language:
#
#   py    — the module docstring if there is one, otherwise a leading `#` block.
#           ⚠️ Docstring FIRST: this project's own files open with three `#` credit
#           lines and then the real prose, so a leading-comment-only reader would
#           report `Author: …` as what every module is about.
#   c     — a leading `/* … */` (or `/** … */`) block, otherwise leading `//`.
#   hash  — a leading `#` block only (shebang and coding cookie skipped).
#   dash  — a leading `--` block only.
COMMENT_SYNTAX: dict[str, str] = {
    "py": "py", "pyi": "py", "pyw": "py", "pyx": "py",
    "js": "c", "mjs": "c", "cjs": "c", "jsx": "c", "ts": "c", "tsx": "c",
    "mts": "c", "cts": "c", "java": "c", "kt": "c", "kts": "c", "scala": "c",
    "groovy": "c", "go": "c", "rs": "c", "zig": "c", "c": "c", "h": "c",
    "hpp": "c", "hh": "c", "hxx": "c", "cpp": "c", "cc": "c", "cxx": "c",
    "cs": "c", "swift": "c", "php": "c", "dart": "c", "sol": "c",
    "css": "c", "scss": "c", "less": "c", "m": "c", "mm": "c",
    "sh": "hash", "bash": "hash", "zsh": "hash", "fish": "hash", "rb": "hash",
    "pl": "hash", "pm": "hash", "r": "hash", "jl": "hash", "nim": "hash",
    "ex": "hash", "exs": "hash", "tf": "hash", "hcl": "hash", "nix": "hash",
    "ps1": "hash", "psm1": "hash",
    "sql": "dash", "lua": "dash", "hs": "dash", "elm": "dash",
}

# Basenames (extension removed) that mean "this file is the root of something".
# A package initialiser, an index, an app or a server module is what a reader
# opens first, so its own description is worth more than a larger file's.
NOTE_ROOT_STEMS = frozenset({
    "__init__", "__main__", "index", "main", "app", "application", "lib", "core",
    "server", "client", "cli", "api", "engine", "router", "routes", "handler",
    "models", "schema", "types", "config", "settings", "setup", "mod",
})

# Files that introduce themselves by their *directory* rather than by their own
# name — `agent2/cli/__init__.py`'s docstring opens `agent2.cli`, and `index.ts`'s
# opens with the folder it indexes. Read only by `_strip_self_reference`.
PACKAGE_FILES = frozenset({
    "__init__.py", "index.js", "index.ts", "index.jsx", "index.tsx", "mod.rs",
    "lib.rs", "package.scala",
})

# Why a file was read, keyed by the rank the walk gave it. Reported per note, so a
# human (and Task 31's `## Important Files`) can see the selection was not random.
NOTE_WHY: dict[int, str] = {
    1: "module root", 2: "root source file", 3: "large source file",
    4: "test module",
}

NOTE_HEAD_BYTES = 16384   # bytes read from one file — the header, never the body
NOTE_MIN_CHARS = 16       # below this a "note" is `TODO` or a copyright line
NOTE_READ_FACTOR = 4      # opens allowed per kept note (cf. MAX_ROOT_PROBE)
NOTE_POOL = 256           # candidates retained by the walk, so memory is bounded

# Characters in a note's one-line `summary`. ⚠️ THE SUMMARY IS SHIPPED, NOT
# RE-DERIVED: `projectdoc`'s `## Important Files`, `cli/render._scan_rows()` and the
# browser's Project panel all print this exact string, so "the first sentence of a
# note" is one declaration rather than one in Python and a second in JavaScript —
# the same reason `core/highlight.py` ships `spans` instead of letting the browser
# tokenize. The doc's copy is the one that matters: it is written into a committed
# file that every later turn reads as fact, and a browser that trimmed one sentence
# differently would describe a different file than the one on disk.
NOTE_SUMMARY_CHARS = 180

# The command categories, in the order a human reads them and the order Task 31's
# `## Build Commands` / `## Test Commands` / `## Development Commands` want.
COMMAND_KINDS: tuple[str, ...] = ("install", "dev", "build", "test", "lint", "other")


# ── Package managers ──────────────────────────────────────────────────────────
# (key, label, manifests, lockfiles, marker). `marker` is a substring that must
# appear in one of the manifests — that is how five different Python managers are
# told apart when they all live in `pyproject.toml`. An empty marker means the
# manifest alone is the proof.
PACKAGE_MANAGERS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...], str], ...] = (
    ("pip",        "pip",         ("requirements.txt", "requirements/base.txt",
                                   "constraints.txt"), (), ""),
    ("poetry",     "Poetry",      ("pyproject.toml",), ("poetry.lock",), "[tool.poetry"),
    ("pdm",        "PDM",         ("pyproject.toml",), ("pdm.lock",), "[tool.pdm"),
    ("hatch",      "Hatch",       ("pyproject.toml",), (), "[tool.hatch"),
    # ⚠️ `[tool.uv` IS REQUIRED, like every other pyproject claimant. With an empty
    # marker this row matched EVERY `pyproject.toml`, so a plain PEP-621 project
    # was reported as installed with a tool it has never used — and, because one
    # claimant is not an ambiguity, the disambiguation below never noticed.
    ("uv",         "uv",          ("pyproject.toml",), ("uv.lock",), "[tool.uv"),
    ("pipenv",     "Pipenv",      ("Pipfile",), ("Pipfile.lock",), ""),
    ("conda",      "conda",       ("environment.yml", "environment.yaml"), (), ""),
    ("setuptools", "setuptools",  ("setup.py", "setup.cfg"), (), ""),
    ("npm",        "npm",         ("package.json",), ("package-lock.json",), ""),
    ("yarn",       "Yarn",        ("package.json",), ("yarn.lock",), ""),
    ("pnpm",       "pnpm",        ("package.json",), ("pnpm-lock.yaml",), ""),
    ("bun",        "Bun",         ("package.json",), ("bun.lockb", "bun.lock"), ""),
    ("cargo",      "Cargo",       ("Cargo.toml",), ("Cargo.lock",), ""),
    ("go",         "Go modules",  ("go.mod",), ("go.sum",), ""),
    ("composer",   "Composer",    ("composer.json",), ("composer.lock",), ""),
    ("bundler",    "Bundler",     ("Gemfile",), ("Gemfile.lock",), ""),
    ("maven",      "Maven",       ("pom.xml",), (), ""),
    ("gradle",     "Gradle",      ("build.gradle", "build.gradle.kts"),
                                  ("gradle.lockfile",), ""),
    ("nuget",      "NuGet",       ("packages.config", "Directory.Packages.props"), (), ""),
    ("swiftpm",    "SwiftPM",     ("Package.swift",), ("Package.resolved",), ""),
    ("pub",        "pub",         ("pubspec.yaml",), ("pubspec.lock",), ""),
    ("mix",        "Mix",         ("mix.exs",), ("mix.lock",), ""),
)

# `npm`/`yarn`/`pnpm`/`bun` all claim `package.json`; only one of them installed
# this tree, and the lockfile is the evidence. When none is present the manifest
# still proves *a* Node toolchain, so `npm` is reported as the fallback rather
# than reporting all four (which reads as four package managers in one project).
NODE_MANAGERS = ("yarn", "pnpm", "bun", "npm")
# Same shape for Python's five: an explicit marker or lockfile wins, and `pip`
# stands in when only `requirements.txt` is there.
PY_PYPROJECT_MANAGERS = ("poetry", "pdm", "hatch", "uv")


# ── Frameworks ────────────────────────────────────────────────────────────────
# (label, dependency aliases). Matched against dependency NAMES gathered from the
# manifests — never against the source tree, which would mean reading every file.
FRAMEWORKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Django",          ("django",)),
    ("Flask",           ("flask",)),
    ("FastAPI",         ("fastapi",)),
    ("Starlette",       ("starlette",)),
    ("Socket.IO",       ("flask-socketio", "python-socketio", "socket.io")),
    ("SQLAlchemy",      ("sqlalchemy",)),
    ("pytest",          ("pytest",)),
    ("Celery",          ("celery",)),
    ("Pydantic",        ("pydantic",)),
    ("NumPy / pandas",  ("numpy", "pandas")),
    ("PyTorch",         ("torch",)),
    ("TensorFlow",      ("tensorflow",)),
    ("Streamlit",       ("streamlit",)),
    ("Rich",            ("rich",)),
    ("prompt_toolkit",  ("prompt_toolkit", "prompt-toolkit")),
    ("React",           ("react",)),
    ("Next.js",         ("next",)),
    ("Vue",             ("vue",)),
    ("Nuxt",            ("nuxt",)),
    ("Svelte",          ("svelte",)),
    ("Angular",         ("@angular/core",)),
    ("Express",         ("express",)),
    ("NestJS",          ("@nestjs/core",)),
    ("Vite",            ("vite",)),
    ("webpack",         ("webpack",)),
    ("Tailwind CSS",    ("tailwindcss",)),
    ("Jest",            ("jest",)),
    ("Vitest",          ("vitest",)),
    ("Playwright",      ("playwright", "@playwright/test")),
    ("Rails",           ("rails",)),
    ("Laravel",         ("laravel/framework",)),
    ("Spring",          ("spring-boot", "spring-core")),
    ("Actix",           ("actix-web",)),
    ("Axum",            ("axum",)),
    ("Tokio",           ("tokio",)),
    ("Gin",             ("github.com/gin-gonic/gin",)),
    ("Google Gemini",   ("google-genai", "google-generativeai")),
    ("OpenAI SDK",      ("openai",)),
    ("Anthropic SDK",   ("anthropic",)),
    ("MCP",             ("mcp", "modelcontextprotocol")),
)

# (label, marker path relative to the root). A file whose mere presence names a
# framework or a piece of infrastructure no dependency list would mention.
FRAMEWORK_FILES: tuple[tuple[str, str], ...] = (
    ("Django",          "manage.py"),
    ("Next.js",         "next.config.js"),
    ("Next.js",         "next.config.mjs"),
    ("Next.js",         "next.config.ts"),
    ("Nuxt",            "nuxt.config.ts"),
    ("Angular",         "angular.json"),
    ("Vite",            "vite.config.js"),
    ("Vite",            "vite.config.ts"),
    ("Svelte",          "svelte.config.js"),
    ("Tailwind CSS",    "tailwind.config.js"),
    ("Tailwind CSS",    "tailwind.config.ts"),
    ("Docker",          "Dockerfile"),
    ("Docker Compose",  "docker-compose.yml"),
    ("Docker Compose",  "docker-compose.yaml"),
    ("Terraform",       "main.tf"),
    ("Kubernetes",      "kustomization.yaml"),
)


# ── Entry points ──────────────────────────────────────────────────────────────
# Conventional entry files, checked at the root and inside a source dir — one or
# two levels down, so `src/<package>/main.py` and `cmd/<binary>/main.go` count.
# `why` is what the report prints, because "src/main.rs" alone does not say why
# it is listed.
ENTRY_HINTS: tuple[tuple[str, str], ...] = (
    ("main.py", "conventional Python entry point"),
    ("app.py", "conventional application module"),
    ("manage.py", "Django management entry point"),
    ("wsgi.py", "WSGI application"),
    ("asgi.py", "ASGI application"),
    ("__main__.py", "package executed with -m"),
    ("cli.py", "command-line entry point"),
    ("server.py", "server entry point"),
    ("index.js", "Node entry point"),
    ("index.ts", "Node entry point"),
    ("main.js", "Node entry point"),
    ("main.ts", "Node entry point"),
    ("server.js", "server entry point"),
    ("app.js", "application entry point"),
    ("main.go", "Go entry point"),
    ("main.rs", "Rust entry point"),
    ("lib.rs", "Rust library root"),
    ("Program.cs", "C# entry point"),
    ("Main.java", "Java entry point"),
)

# Where a project's own source conventionally lives. Used for the `src/` layout
# convention and to root the entry-point probe: a hint basename counts under one
# of these at a depth of one or two segments (`src/main.py`, `src/pkg/main.py`).
SOURCE_DIRS: tuple[str, ...] = ("src", "lib", "app", "cmd", "internal", "pkg")

# Directory names that mean "tests live here", and the basename patterns that
# mean "this file is a test". Both are needed: a project can have `tests/` with
# no matching names, or `test_*.py` scattered beside the code with no test dir.
#
# ⚠️ The second element is the runner the pattern PROVES, and `""` means the file
# name does not prove one. `foo.test.ts` is Jest, Vitest, Mocha or a bare `node
# --test` and the name cannot say which — so it stays `""` and the answer comes
# from `TEST_RUNNERS` (a dependency or a config file) or not at all. Guessing
# "Jest" here would put a command in `## Test Commands` that runs nothing.
TEST_DIR_NAMES = frozenset({"test", "tests", "spec", "specs", "__tests__", "testing"})
TEST_FILE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^test_.+\.py$"), "pytest"),
    (re.compile(r"^.+_test\.py$"), "pytest"),
    (re.compile(r"^.+_test\.go$"), "go test"),
    (re.compile(r"^.+\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$"), ""),
    (re.compile(r"^.+Test(s)?\.(java|kt|cs)$"), ""),
    (re.compile(r"^.+_spec\.rb$"), "RSpec"),
    (re.compile(r"^test_.+\.rb$"), ""),
    (re.compile(r"^.+_test\.rs$"), "cargo test"),
)

# Test runners, inferred from a dependency or a config file rather than guessed.
TEST_RUNNERS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("pytest", ("pytest",), ("pytest.ini", "conftest.py", "tox.ini")),
    ("Jest", ("jest",), ("jest.config.js", "jest.config.ts")),
    ("Vitest", ("vitest",), ("vitest.config.js", "vitest.config.ts")),
    ("Mocha", ("mocha",), (".mocharc.json", ".mocharc.yml")),
    ("RSpec", ("rspec",), (".rspec",)),
    ("JUnit", ("junit", "junit-jupiter"), ()),
)


# ── Build systems + configuration ─────────────────────────────────────────────
BUILD_FILES: tuple[tuple[str, str], ...] = (
    ("Make", "Makefile"), ("Make", "GNUmakefile"), ("CMake", "CMakeLists.txt"),
    ("Meson", "meson.build"), ("Bazel", "BUILD.bazel"), ("Bazel", "WORKSPACE"),
    ("Gradle", "build.gradle"), ("Gradle", "build.gradle.kts"), ("Maven", "pom.xml"),
    ("setuptools", "setup.py"), ("Cargo", "Cargo.toml"), ("Docker", "Dockerfile"),
    ("MSBuild", "Directory.Build.props"), ("Just", "justfile"), ("Task", "Taskfile.yml"),
)

# Configuration a reader should know about. Reported by name only — never read for
# content beyond the bounded manifest reads below, because a `.env` is exactly the
# kind of file whose *contents* must not travel into a report or a prompt.
CONFIG_FILES: tuple[str, ...] = (
    "pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "mypy.ini",
    ".ruff.toml", "ruff.toml", ".flake8", ".pylintrc", ".editorconfig",
    ".pre-commit-config.yaml", "tsconfig.json", "jsconfig.json",
    ".eslintrc", ".eslintrc.js", ".eslintrc.json", "eslint.config.js",
    ".prettierrc", ".prettierrc.json", "babel.config.js", "webpack.config.js",
    "vite.config.ts", "jest.config.js", "docker-compose.yml", "docker-compose.yaml",
    "Dockerfile", ".dockerignore", ".gitignore", ".gitattributes", "Makefile",
    ".env.example", ".env.sample", "renovate.json", "netlify.toml", "vercel.json",
    "Procfile", "sonar-project.properties",
)

# Manifests whose text is read (bounded) to derive dependencies and commands.
# ⚠️ THIS IS THE ONLY LIST OF FILES THIS MODULE OPENS, plus the indentation sample
# and the root entry-guard probe. A scanner that reads whatever it finds is one
# `.env` away from putting a live credential into `agent2.md`.
MANIFEST_FILES: tuple[str, ...] = (
    "pyproject.toml", "requirements.txt", "setup.cfg", "setup.py", "Pipfile",
    "environment.yml", "package.json", "Cargo.toml", "go.mod", "composer.json",
    "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts", "pubspec.yaml",
    "mix.exs", "Makefile", "GNUmakefile", "tox.ini", ".editorconfig",
)

# How a script/target name maps onto a command category.
SCRIPT_KINDS: dict[str, tuple[str, ...]] = {
    "install": ("install", "setup", "bootstrap", "deps", "init"),
    "dev": ("dev", "start", "serve", "watch", "run"),
    "build": ("build", "compile", "bundle", "dist", "package"),
    "test": ("test", "tests", "spec", "check", "coverage", "test:unit"),
    "lint": ("lint", "format", "fmt", "typecheck", "tsc", "mypy", "ruff"),
}

_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*:(?!=)", re.M)
_ENTRY_GUARD = re.compile(r"^if\s+__name__\s*==", re.M)

# Lines a `py`-family reader steps over before the docstring: blanks, `#` comments
# (the credit header this project's own files carry) and `from __future__`.
_PY_SKIP = re.compile(r"\A(?:[ \t]*(?:#[^\n]*)?\n|[ \t]*from\s+__future__\s+import[^\n]*\n)*")
_PY_DOC = re.compile(r"\A[ \t]*[rRuUbBfF]{0,2}(?P<q>\"\"\"|''')(?P<body>.*?)(?P=q)", re.S)
_PY_DOC_OPEN = re.compile(r"\A[ \t]*[rRuUbBfF]{0,2}(\"\"\"|''')")
_CODING = re.compile(r"^-\*-\s*coding[:=]|^coding[:=]", re.I)
# A rule, a banner or a box-drawing divider carries no information; every family
# produces them, so they are dropped once rather than in four readers.
_DECOR = re.compile(r"^[\s\-=_*#/~`.:|+<>!─-╿]+$")
# ⚠️ ATTRIBUTION IS NOT A DESCRIPTION, AND DROPPING IT IS WHAT MAKES A NOTE WORTH
# READING. This project's own files open with three credit lines, and a vendored
# one opens with a licence banner — both are the *author's* metadata, not the
# file's subject, so a reader that kept them reported "Author: … Portfolio: …" as
# what four of this repository's modules are about, identically, and a `LICENSE`
# preamble as what a dependency does. A note left empty by this filter is skipped
# entirely and the read budget moves on to the next candidate, which is why the
# filter improves the answer rather than merely shortening it.
_ATTRIB = re.compile(
    r"^(?:@?(?:authors?|portfolio|github|gitlab|website|homepage|e-?mail|contact|"
    r"maintainers?|created(?:\s+by)?|modified|date|version|since|licen[cs]e|"
    r"copyright|file|brief\s*:)\s*[:=@]|"
    r"copyright\b|\(c\)\s|©|spdx-license-identifier|all rights reserved)",
    re.I)


# ── The result shape ──────────────────────────────────────────────────────────
def empty() -> dict:
    """A scan that found nothing. ⚠️ Every key a caller may read is present here.

    Same contract as `gitstate.empty()`: a total function's failure mode is a
    complete, empty answer, so a renderer or Task 31 never has to ask whether a
    key exists before reading it.
    """
    return {
        "root": "", "name": "", "at": 0.0, "elapsed_ms": 0.0,
        "truncated": False, "truncated_by": "",
        "files": 0, "dirs": 0, "bytes": 0, "source_files": 0,
        "languages": [], "primary_language": "",
        "structure": [], "docs": [], "readme": "",
        "package_managers": [], "frameworks": [], "entry_points": [],
        "tests": {"dirs": [], "files": 0, "runners": []},
        "build": {"systems": [], "files": []},
        "config_files": [],
        "commands": {kind: [] for kind in COMMAND_KINDS},
        "conventions": [],
        "file_notes": [],
        "git": {"repo": False, "unknown": False, "branch": "", "detached": False,
                "changed": 0, "staged": 0, "unstaged": 0, "untracked": 0,
                "ahead": 0, "behind": 0, "upstream": "", "describe": "",
                "commits": []},
        "agent2": {"dir": "", "present": False, "doc": False, "doc_chars": 0,
                   "skills": 0, "workflows": 0},
        "errors": [],
    }


def _step(out: dict, where: str, fn, *a, **kw):
    """Run one analysis step inside its own guard.

    ⚠️ ONE GUARD PER STEP, NEVER ONE AROUND THE SEQUENCE — the rule
    `core/broker/__init__.py` states for its collectors, here for the same reason.
    A malformed `package.json` costs the caller the JavaScript half of the answer;
    it may not cost them the language census, the git state and the test layout.
    """
    try:
        return fn(*a, **kw)
    except Exception as exc:
        try:
            out["errors"].append({"where": where, "error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        return None


# ── Inputs, each read from the module that owns it ────────────────────────────
def _skip_dirs() -> set[str]:
    """`core.workspace.SKIP_DIRS` — read, never copied. See the docstring."""
    try:
        from agent2.core import workspace as _ws
        return set(_ws.SKIP_DIRS)
    except Exception:
        # The walk must still be bounded if the import fails, so this fallback is
        # deliberately the two directories whose absence would make the scan
        # meaningless (a `.git` object store and a `node_modules`), NOT a second
        # copy of the table.
        return {".git", "node_modules"}


def _workspace_root() -> Path | None:
    try:
        from agent2.core import workspace as _ws
        return Path(str(_ws.root()))
    except Exception:
        return None


def _project_docs() -> tuple[tuple[str, ...], str]:
    """`(PROJECT_DOCS, PRIMARY_DOC)` from the broker — the one doc declaration."""
    try:
        from agent2.core import broker as _broker
        return tuple(_broker.PROJECT_DOCS), str(_broker.PRIMARY_DOC)
    except Exception:
        return (), ".agent2/agent2.md"


def _read_text(path: Path, limit: int | None = None) -> str:
    """Bounded text read. Returns "" for anything unreadable."""
    cap = int(limit or config.INIT_MANIFEST_BYTES)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(cap)
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


# ── The walk ──────────────────────────────────────────────────────────────────
class _Walk:
    """Everything one pass over the tree collects.

    Deliberately not a list of every path: a 200 000-file monorepo would put
    200 000 strings in memory to answer questions about twenty of them. Only
    paths whose basename is *interesting* (a manifest, a marker, a config, an
    entry hint) are retained, plus every root-level name.
    """

    def __init__(self):
        self.files = 0
        self.dirs = 0
        self.bytes = 0
        self.ext_files: dict[str, int] = {}
        self.ext_bytes: dict[str, int] = {}
        self.name_langs: dict[str, int] = {}
        self.top: dict[str, int] = {}
        self.root_names: set[str] = set()
        self.interesting: set[str] = set()      # rel paths, forward slashes
        self.test_dirs: set[str] = set()
        self.test_files = 0
        self.proved_runners: set[str] = set()
        self.has_ci = False
        self.source_samples: list[Path] = []
        # (rank, -size, rel) for files whose own comments are worth reading. Ranked
        # and size-ordered here rather than in `_notes`, because the walk is the one
        # pass that sees every file — and capped at `NOTE_POOL`, because a monorepo
        # must not put 200 000 tuples in memory to pick twelve of them.
        self.note_cands: list[tuple[int, int, str]] = []
        self.truncated_by = ""


def _interesting_names() -> set[str]:
    names: set[str] = set()
    names.update(n.lower() for n in CONFIG_FILES)
    names.update(n.lower() for n in MANIFEST_FILES)
    names.update(n.lower() for _label, n in BUILD_FILES)
    names.update(n.lower() for _label, n in FRAMEWORK_FILES)
    names.update(n.lower() for n, _why in ENTRY_HINTS)
    for _key, _label, manifests, locks, _marker in PACKAGE_MANAGERS:
        names.update(m.lower().rsplit("/", 1)[-1] for m in manifests)
        names.update(m.lower() for m in locks)
    for _label, _deps, files in TEST_RUNNERS:
        names.update(f.lower() for f in files)
    return names


def _walk(base: Path, out: dict) -> _Walk:
    """One bounded pass. ⚠️ Every cap that engages is recorded, never silent."""
    w = _Walk()
    skip = _skip_dirs()
    interesting = _interesting_names()
    max_files = int(config.INIT_MAX_FILES)
    max_depth = int(config.INIT_MAX_DEPTH)
    deadline = time.time() + float(config.INIT_SCAN_BUDGET_SEC)
    base_str = str(base)

    for dirpath, dirnames, filenames in os.walk(base_str, followlinks=False):
        if time.time() > deadline:
            w.truncated_by = w.truncated_by or "budget"
            dirnames[:] = []
            break
        try:
            rel_dir = os.path.relpath(dirpath, base_str).replace(os.sep, "/")
        except Exception:
            rel_dir = ""
        if rel_dir == ".":
            rel_dir = ""
        depth = 0 if not rel_dir else rel_dir.count("/") + 1

        # Prune before descending: a skipped directory is never counted either,
        # which is why `files` matches what a developer would call "the project".
        kept = []
        for d in dirnames:
            low = d.lower()
            if low in skip or d in skip:
                continue
            if d.startswith(".") and low not in KEEP_DOT_DIRS:
                continue
            kept.append(d)
        if depth >= max_depth:
            if kept:
                w.truncated_by = w.truncated_by or "depth"
            kept = []
        dirnames[:] = kept
        if rel_dir:
            w.dirs += 1
            if os.path.basename(rel_dir).lower() in TEST_DIR_NAMES:
                w.test_dirs.add(rel_dir)
            # CI is a convention worth reporting, and it is proved by a workflow
            # FILE, not by the folder: an empty `.github/workflows/` runs nothing.
            if rel_dir.endswith("/workflows") and rel_dir.startswith((".github", ".gitlab")):
                w.has_ci = w.has_ci or bool(filenames)

        top = rel_dir.split("/", 1)[0] if rel_dir else ""
        # Computed once per directory, not once per file: a note from a test module
        # describes the tests, not the project, so it ranks last rather than being
        # excluded — a project whose only prose lives in its tests still gets an
        # answer, and it is still labelled `test module`.
        in_tests = bool(rel_dir) and any(p.lower() in TEST_DIR_NAMES
                                         for p in rel_dir.split("/"))

        for fn in filenames:
            if w.files >= max_files:
                w.truncated_by = w.truncated_by or "files"
                dirnames[:] = []
                break
            w.files += 1
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            low = fn.lower()
            if not rel_dir:
                w.root_names.add(fn)
            if low in interesting and depth <= 2:
                w.interesting.add(rel)
            if top:
                w.top[top] = w.top.get(top, 0) + 1

            try:
                size = os.path.getsize(os.path.join(dirpath, fn))
            except Exception:
                size = 0
            w.bytes += size

            ext = low.rsplit(".", 1)[1] if "." in low[1:] else ""
            if ext in LANGUAGES:
                w.ext_files[ext] = w.ext_files.get(ext, 0) + 1
                w.ext_bytes[ext] = w.ext_bytes.get(ext, 0) + size
                if len(w.source_samples) < MAX_INDENT_SAMPLE and ext in ("py", "js",
                                                                        "ts", "tsx",
                                                                        "go", "rs",
                                                                        "java"):
                    w.source_samples.append(Path(dirpath) / fn)
            elif low in LANGUAGE_FILENAMES:
                lang = LANGUAGE_FILENAMES[low]
                w.name_langs[lang] = w.name_langs.get(lang, 0) + 1
            elif ext in DATA_EXTS or ext in DOC_EXTS:
                w.ext_files[ext] = w.ext_files.get(ext, 0) + 1
                w.ext_bytes[ext] = w.ext_bytes.get(ext, 0) + size

            is_test = in_tests
            for pat, runner in TEST_FILE_PATTERNS:
                if pat.match(fn):
                    is_test = True
                    w.test_files += 1
                    if rel_dir:
                        w.test_dirs.add(rel_dir)
                    if runner:
                        w.proved_runners.add(runner)
                    break

            # A candidate for `_notes`. `.min.` is excluded because a bundle's
            # leading comment is a licence banner, which describes the vendor's
            # library rather than this project.
            if ext in COMMENT_SYNTAX and ".min." not in low:
                stem = low.rsplit(".", 1)[0]
                if is_test:
                    rank = 4
                elif stem in NOTE_ROOT_STEMS and depth <= 2:
                    rank = 1
                elif not rel_dir:
                    rank = 2
                else:
                    rank = 3
                w.note_cands.append((rank, -size, rel))
                if len(w.note_cands) > NOTE_POOL * 2:
                    w.note_cands.sort()
                    del w.note_cands[NOTE_POOL:]
        if w.files >= max_files:
            w.truncated_by = w.truncated_by or "files"
            break

    if w.truncated_by:
        out["truncated"] = True
        out["truncated_by"] = w.truncated_by
    return w


# ── Analysis steps ────────────────────────────────────────────────────────────
def _languages(out: dict, w: _Walk) -> None:
    counts: dict[str, dict] = {}
    for ext, n in w.ext_files.items():
        lang = LANGUAGES.get(ext)
        if not lang:
            continue
        rec = counts.setdefault(lang, {"name": lang, "files": 0, "bytes": 0})
        rec["files"] += n
        rec["bytes"] += w.ext_bytes.get(ext, 0)
    for lang, n in w.name_langs.items():
        rec = counts.setdefault(lang, {"name": lang, "files": 0, "bytes": 0})
        rec["files"] += n

    total = sum(r["files"] for r in counts.values()) or 0
    out["source_files"] = total
    # Ranked by BYTES, then files: a project with 400 one-line `__init__.py`
    # stubs and 30 large TypeScript modules is a TypeScript project, and a
    # file count alone says the opposite.
    rows = sorted(counts.values(), key=lambda r: (-r["bytes"], -r["files"], r["name"]))
    for r in rows:
        r["share"] = round(100.0 * r["files"] / total, 1) if total else 0.0
    out["languages"] = rows[:MAX_LANGUAGES]
    out["primary_language"] = rows[0]["name"] if rows else ""


def _structure(out: dict, w: _Walk) -> None:
    rows = [{"path": name, "files": n}
            for name, n in sorted(w.top.items(), key=lambda kv: (-kv[1], kv[0]))]
    out["structure"] = rows[:MAX_DIRS]


def _docs(out: dict, base: Path, w: _Walk) -> None:
    docs, primary = _project_docs()
    found: list[str] = []
    seen: set[str] = set()
    for rel in docs:
        # ⚠️ `/init`'s OWN OUTPUT IS NOT ONE OF THE PROJECT'S DOCUMENTS. `PRIMARY_DOC`
        # is `.agent2/agent2.md`, and this list is read straight from the filesystem
        # rather than from the walk, so pruning `.agent2` from `KEEP_DOT_DIRS` does
        # not reach it. Left in, the generated `## Important Files` named the very
        # file it was being written into — "`.agent2/agent2.md` — project
        # documentation" — and the second `/init` reported a change nobody made.
        # Whether that doc exists is the `agent2` seam's fact, three lines below.
        if rel == primary:
            continue
        try:
            p = base / rel
            if not p.is_file():
                continue
            # ⚠️ Case-insensitively deduped, exactly as `broker._collect_project`
            # does and for the same reason: `PROJECT_DOCS` lists both `README.md`
            # and `readme.md` for case-sensitive filesystems, and on Windows and
            # macOS both names hit the same file.
            real = os.path.normcase(os.path.abspath(str(p)))
            if real in seen:
                continue
            seen.add(real)
            found.append(rel)
        except Exception:
            continue
    out["docs"] = found
    out["readme"] = next((d for d in found if d.lower().startswith("readme")), "")

    # The `.agent2` seam `core/projectdoc.py` reads. Reported, never created here.
    adir = base / ".agent2"
    doc = base / primary
    info = out["agent2"]
    info["dir"] = ".agent2"
    try:
        info["present"] = adir.is_dir()
    except Exception:
        info["present"] = False
    try:
        info["doc"] = doc.is_file()
        info["doc_chars"] = doc.stat().st_size if info["doc"] else 0
    except Exception:
        info["doc"], info["doc_chars"] = False, 0
    for key, sub in (("skills", "skills"), ("workflows", "workflows")):
        try:
            d = adir / sub
            info[key] = sum(1 for _ in d.iterdir()) if d.is_dir() else 0
        except Exception:
            info[key] = 0


def _manifests(base: Path, w: _Walk) -> dict[str, str]:
    """Read the declared manifests once, bounded by `INIT_MANIFEST_BYTES`.

    ⚠️ ONE OF FOUR READERS, AND THE ONLY ONE THAT KEEPS A FILE'S CONTENTS.
    `_entry_points` opens root `.py` files to look for a `__main__` guard,
    `_conventions` samples source files for their indentation, and `_notes` reads
    the leading comment of a few important ones — but each of those three keeps a
    *derived* fact (a boolean, a tab-or-spaces verdict, a comment) and lets the
    body go. This one holds the manifest text, so it is the reader whose cap
    matters and the reader whose text `_dependencies` and `_commands` parse.
    """
    texts: dict[str, str] = {}
    for name in MANIFEST_FILES:
        if name not in w.root_names:
            continue
        text = _read_text(base / name)
        if text:
            texts[name] = text
    return texts


def _dependencies(texts: dict[str, str]) -> set[str]:
    """Dependency names, lowercased. Best effort by construction.

    A manifest is parsed properly when it is JSON, and token-scanned otherwise —
    because the alternative is a TOML/YAML/Gemfile/Gradle parser per ecosystem,
    and the only question asked of the result is "is `django` in here". A token
    scan can over-report (a framework named in a comment), which costs one wrong
    line in a report; a parser that raises on an unusual manifest would cost the
    whole answer.
    """
    deps: set[str] = set()

    pkg = texts.get("package.json", "")
    if pkg:
        try:
            data = json.loads(pkg)
            for key in ("dependencies", "devDependencies", "peerDependencies",
                        "optionalDependencies"):
                block = data.get(key) or {}
                if isinstance(block, dict):
                    deps.update(str(k).lower() for k in block)
        except Exception:
            deps.update(m.lower() for m in re.findall(r'"([@A-Za-z0-9_./\-]+)"\s*:\s*"', pkg))

    req = texts.get("requirements.txt", "")
    if req:
        for line in req.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[<>=!~;\[\s]", line, maxsplit=1)[0].strip()
            if name:
                deps.add(name.lower())

    for name in ("pyproject.toml", "Cargo.toml", "go.mod", "composer.json",
                 "Gemfile", "pom.xml", "build.gradle", "build.gradle.kts",
                 "Pipfile", "environment.yml", "setup.py", "setup.cfg",
                 "pubspec.yaml", "mix.exs"):
        text = texts.get(name, "")
        if not text:
            continue
        deps.update(m.lower() for m in re.findall(r'["\']([@A-Za-z0-9_./\-]{2,60})["\']', text))
        deps.update(m.lower() for m in re.findall(r"^\s*([A-Za-z][A-Za-z0-9_.\-]{1,59})\s*=",
                                                  text, re.M))
        deps.update(m.lower() for m in re.findall(r"^\s*(?:require|gem|implementation)\s+"
                                                  r"[\"']?([@A-Za-z0-9_./\-]{2,80})",
                                                  text, re.M))
    return deps


def _package_managers(out: dict, w: _Walk, texts: dict[str, str]) -> None:
    hits: list[dict] = []
    by_key: dict[str, dict] = {}
    for key, label, manifests, locks, marker in PACKAGE_MANAGERS:
        manifest = next((m for m in manifests if m.rsplit("/", 1)[-1] in w.root_names
                         or m in w.interesting), "")
        if not manifest:
            continue
        lock = next((lk for lk in locks if lk in w.root_names), "")
        if marker:
            text = texts.get(manifest.rsplit("/", 1)[-1], "")
            if marker not in text and not lock:
                continue
        rec = {"key": key, "label": label, "manifest": manifest, "lockfile": lock}
        by_key[key] = rec
        hits.append(rec)

    # ⚠️ Four managers claim `package.json` and five claim `pyproject.toml`; only
    # one of each installed this tree, and the lockfile (or an explicit `[tool.x]`
    # marker) is the evidence. Reporting all of them would tell the reader this
    # project uses four package managers — which is not an over-report, it is a
    # different and false fact.
    node = [k for k in NODE_MANAGERS if k in by_key]
    if len(node) > 1:
        winner = next((k for k in node if by_key[k]["lockfile"]), "npm")
        hits = [r for r in hits if r["key"] not in node or r["key"] == winner]
    py = [k for k in PY_PYPROJECT_MANAGERS if k in by_key]
    if len(py) > 1:
        winner = next((k for k in py if by_key[k]["lockfile"]), py[0])
        hits = [r for r in hits if r["key"] not in py or r["key"] == winner]

    # ⚠️ A PEP-621 `pyproject.toml` that names no tool is still evidence of an
    # installable Python project — `pip install -e .` installs it. Every pyproject
    # claimant now needs its marker or its lockfile, so without this a very ordinary
    # project would report NO package manager at all, and Task 31 would write a
    # `## Build Commands` section saying the project declares no install step.
    # `pip` is Python's `npm` here: the one thing the manifest alone does prove.
    if "pyproject.toml" in w.root_names and "pip" not in by_key and not py:
        hits.insert(0, {"key": "pip", "label": "pip",
                        "manifest": "pyproject.toml", "lockfile": ""})
    out["package_managers"] = hits


def _frameworks(out: dict, w: _Walk, deps: set[str]) -> None:
    found: list[dict] = []
    seen: set[str] = set()
    for label, aliases in FRAMEWORKS:
        hit = next((a for a in aliases if a in deps), "")
        if hit and label not in seen:
            seen.add(label)
            found.append({"name": label, "evidence": f"dependency: {hit}"})
    for label, marker in FRAMEWORK_FILES:
        if label in seen:
            continue
        if marker in w.root_names or marker in w.interesting:
            seen.add(label)
            found.append({"name": label, "evidence": f"file: {marker}"})
    out["frameworks"] = found[:MAX_FRAMEWORKS]


def _entry_points(out: dict, base: Path, w: _Walk, texts: dict[str, str]) -> None:
    found: list[dict] = []
    seen: set[str] = set()

    def add(rel: str, why: str):
        if rel and rel not in seen:
            seen.add(rel)
            found.append({"path": rel, "why": why})

    for name, why in ENTRY_HINTS:
        if name in w.root_names:
            add(name, why)

    # ⚠️ ONE PACKAGE LEVEL DEEP, NOT JUST `<source dir>/<hint>`. The canonical
    # Python src-layout puts the entry point at `src/<package>/main.py` (or
    # `__main__.py`), and Go's at `cmd/<binary>/main.go` — so matching only
    # `src/main.py` found a flat project's entry point and missed the one nearly
    # every packaged project has. `## Entry Points` then read "none found" for a
    # project whose entry point sits exactly where the convention puts it.
    hint_why = {n.lower(): why for n, why in ENTRY_HINTS}
    for rel in sorted(w.interesting):
        parts = rel.split("/")
        if not (2 <= len(parts) <= 3) or parts[0] not in SOURCE_DIRS:
            continue
        if why := hint_why.get(parts[-1].lower(), ""):
            add(rel, why)

    # Declared entry points beat inferred ones — a `[project.scripts]` table or a
    # `bin` field is the author SAYING where this starts, which no heuristic
    # outranks. They are added after the conventional names but carry the stronger
    # `why`, and the ordering below puts declared ones first.
    declared: list[dict] = []
    pkg = texts.get("package.json", "")
    if pkg:
        try:
            data = json.loads(pkg)
            for key in ("main", "module", "browser"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    declared.append({"path": val.lstrip("./"),
                                     "why": f"package.json {key}"})
            binv = data.get("bin")
            if isinstance(binv, str):
                declared.append({"path": binv.lstrip("./"), "why": "package.json bin"})
            elif isinstance(binv, dict):
                for cmd, target in list(binv.items())[:4]:
                    declared.append({"path": str(target).lstrip("./"),
                                     "why": f"package.json bin: {cmd}"})
        except Exception:
            pass
    pyproj = texts.get("pyproject.toml", "")
    if pyproj:
        block = re.search(r"^\[project\.scripts\](.*?)(?=^\[|\Z)", pyproj, re.M | re.S)
        for name, target in re.findall(r"^\s*([A-Za-z0-9_\-]+)\s*=\s*[\"']([^\"']+)",
                                       (block.group(1) if block else ""), re.M):
            declared.append({"path": target, "why": f"pyproject script: {name}"})

    # A root-level Python file with an `if __name__ == "__main__"` guard IS an
    # entry point, and it is the only one this project's own launchers (`run.py`,
    # `agent2cli.py`, …) would ever be found by — none of them is called `main.py`.
    probed = 0
    for name in sorted(w.root_names):
        if probed >= MAX_ROOT_PROBE:
            break
        if not name.lower().endswith(".py"):
            continue
        probed += 1
        if _ENTRY_GUARD.search(_read_text(base / name, 60_000)):
            add(name, "__main__ guard")

    ordered = declared + found
    final: list[dict] = []
    seen.clear()
    for rec in ordered:
        rel = str(rec.get("path") or "")
        if rel and rel not in seen:
            seen.add(rel)
            final.append(rec)
    out["entry_points"] = final[:MAX_ENTRY_POINTS]


# ── What the important files say about themselves ─────────────────────────────
def _note_family(rel: str) -> str:
    """The comment family for a path, or `""` when this reader does not know one."""
    low = rel.rsplit("/", 1)[-1].lower()
    ext = low.rsplit(".", 1)[1] if "." in low[1:] else ""
    return COMMENT_SYNTAX.get(ext, "")


def _prefix_comment(text: str, prefix: str) -> str:
    """The leading `prefix`-comment block. Shebang and coding cookie stepped over."""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        t = raw.strip()
        if not t:
            if lines:
                break          # a blank line ends the header block
            continue
        if not lines and t.startswith("#!"):
            continue
        if not t.startswith(prefix):
            break              # the first line of actual code
        body = t[len(prefix):].strip()
        if _CODING.match(body):
            continue
        lines.append(body)
    return "\n".join(lines)


def _c_comment(text: str) -> str:
    """A leading `/* … */` block, else a leading `//` block."""
    stripped = (text or "").lstrip()
    if stripped.startswith("/*"):
        end = stripped.find("*/", 2)
        body = stripped[2:end] if end > 0 else stripped[2:]
        return "\n".join(raw.strip().lstrip("*").strip() for raw in body.splitlines())
    return _prefix_comment(text, "//")


def _py_comment(text: str) -> str:
    """The module docstring if there is one, else the leading `#` block.

    ⚠️ DOCSTRING FIRST — see `COMMENT_SYNTAX`. An unterminated docstring (the head
    cap cut it) still yields what was read: the alternative is reporting *nothing*
    for the very files whose description is longest.
    """
    head = text or ""
    skip = _PY_SKIP.match(head)
    rest = head[skip.end():] if skip else head
    if m := _PY_DOC.match(rest):
        return m.group("body")
    if m := _PY_DOC_OPEN.match(rest):
        return rest[m.end():]
    return _prefix_comment(head, "#")


def _leading_comment(text: str, family: str) -> str:
    """⚠️ THE PRIVACY BOUNDARY: comment syntax in, comment text out, never code.

    Every branch returns only bytes that sat inside a comment or a docstring, and
    an unrecognised family returns `""` rather than the head of the file. That is
    what lets `projectdoc._evidence()` put these notes in a prompt that leaves the
    machine while its own rule — *nothing but prose a human wrote for a reader* —
    stays true: a comment is exactly that, and a statement is not.
    """
    if family == "py":
        return _py_comment(text)
    if family == "c":
        return _c_comment(text)
    if family == "hash":
        return _prefix_comment(text, "#")
    if family == "dash":
        return _prefix_comment(text, "--")
    return ""


def _clean_note(raw: str) -> str:
    """One line of readable prose: decoration and attribution dropped.

    ⚠️ See `_ATTRIB` — a credit header or a licence banner is about the author, not
    about the file, and a note that is nothing else is dropped by the caller.
    """
    kept: list[str] = []
    for line in (raw or "").splitlines():
        t = line.strip()
        if not t or _DECOR.match(t) or _ATTRIB.match(t):
            continue
        kept.append(t)
    return " ".join(" ".join(kept).split()).strip()


def _strip_self_reference(text: str, rel: str) -> str:
    """Drop a leading restatement of the file's own name.

    ⚠️ THE FIRST WORDS OF THE ANSWER MUST NOT BE THE QUESTION. The convention this
    repository follows — and most others that write module docstrings at all — opens
    with the module's own path, and every consumer of a note already prints the path
    beside it: `- ``agent2/cli/models.py`` — agent2/cli/models.py Models, modes and
    shell detection` spends its first third saying nothing.

    Only a **leading** token is removed, and only when it names *this* file: its path
    (slashed or dotted), its basename, or its stem — plus the parent for a package
    file, because `agent2/cli/__init__.py` is introduced as `agent2.cli`. Anything
    else the author wrote survives, including a path that names a *different* file
    (`agent2/server/routes.py` opens with a stale `agent2/routes.py`, and reporting
    that verbatim is the honest answer — the docstring really does say so).
    """
    head = (text or "").split(" ", 1)
    if len(head) != 2:
        return text
    first = head[0].strip().strip("`\"'*:—-").lower()
    if not first:
        return text
    low = rel.lower()
    base = low.rsplit("/", 1)[-1]
    names = {low, low.replace("/", "."), base, base.rsplit(".", 1)[0]}
    if base in PACKAGE_FILES and "/" in low:
        parent = low.rsplit("/", 1)[0]
        names |= {parent, parent.replace("/", "."), parent.rsplit("/", 1)[-1]}
    if first in names or first.rstrip(":") in names:
        return head[1].lstrip(" —-:")
    return text


def _note_summary(text: str, limit: int = NOTE_SUMMARY_CHARS) -> str:
    """The first sentence of a note, or a clean cut at `limit`. Total.

    ⚠️ A CUT IS MARKED. A sentence that stops without an ellipsis reads as the whole
    thought, and Task 31 writes this string into `agent2.md`, which every later turn
    reads as fact — so a truncated description must be visibly truncated.
    """
    flat = " ".join((text or "").split())
    if not flat:
        return ""
    if len(flat) <= limit:
        cut = re.search(r"(?<=[.!?])\s", flat)
        return flat[:cut.start()] if cut else flat
    cut = re.search(r"(?<=[.!?])\s", flat[:limit + 1])
    if cut:
        return flat[:cut.start()]
    head = flat[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return f"{head or flat[:limit]}…"


def _notes(out: dict, base: Path, w: _Walk) -> None:
    """Read a few important files and keep what their authors said about them.

    ⚠️ THE SELECTION IS DETERMINISTIC, AND THE REASON IS REPORTED. Entry points
    first (the project told us where it starts), then module roots, then root-level
    source, then the largest files, then tests — `why` on every item, because a
    list of files with no stated reason is indistinguishable from a random sample,
    and Task 31 writes this list into `agent2.md` where a later turn reads it as
    fact.

    ⚠️ ONLY COMMENTS LEAVE THIS FUNCTION. `_leading_comment` is the boundary; a
    format whose comment syntax is not declared is skipped rather than sampled.

    ⚠️ BOUNDED BY OPENS, NOT BY A DEADLINE, and deliberately so: at most
    `INIT_NOTE_FILES × NOTE_READ_FACTOR` files are opened and at most
    `NOTE_HEAD_BYTES` is read from each — the same shape as `_entry_points`'
    `MAX_ROOT_PROBE` probe, which is the precedent this follows rather than adding
    a second clock to a module that already has `INIT_SCAN_BUDGET_SEC`. The read
    budget, not the result count, is what bounds a directory of *uncommented*
    modules: those cost an open each and yield nothing.

    Each note carries `text` (what the author wrote, capped) **and** `summary` (the
    first sentence) — see `NOTE_SUMMARY_CHARS` for why the one-liner is shipped
    rather than left to each of the three printers.
    """
    limit = int(config.INIT_NOTE_FILES)
    if limit <= 0:
        return                      # `AGENT2_INIT_NOTE_FILES=0` — see config.py

    picks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rec in (out.get("entry_points") or []):
        rel = str(rec.get("path") or "").replace("\\", "/").lstrip("./")
        if rel and rel not in seen and _note_family(rel):
            seen.add(rel)
            picks.append((rel, "entry point"))
    w.note_cands.sort()
    for rank, _neg_size, rel in w.note_cands:
        if rel not in seen:
            seen.add(rel)
            picks.append((rel, NOTE_WHY.get(rank, "source file")))

    cap = int(config.INIT_NOTE_CHARS)
    notes: list[dict] = []
    reads = 0
    for rel, why in picks:
        if len(notes) >= limit or reads >= limit * NOTE_READ_FACTOR:
            break
        family = _note_family(rel)
        if not family:
            continue
        reads += 1
        text = _strip_self_reference(
            _clean_note(_leading_comment(_read_text(base / rel, NOTE_HEAD_BYTES),
                                         family)), rel)
        if len(text) < NOTE_MIN_CHARS:
            continue
        ext = rel.rsplit(".", 1)[-1].lower()
        # ⚠️ THE SUMMARY IS DERIVED FROM THE FULL TEXT, NOT FROM `kept`. Summarising
        # the capped copy hides the cap: with `INIT_NOTE_CHARS` below
        # `NOTE_SUMMARY_CHARS` the trimmed text is short enough to look complete, so
        # a note that WAS cut would be written into `agent2.md` with no ellipsis and
        # read as the author's whole thought. The tighter of the two limits applies.
        notes.append({
            "path": rel,
            "why": why,
            "language": LANGUAGES.get(ext, ""),
            "text": text[:cap].rstrip(),
            "summary": _note_summary(text, min(NOTE_SUMMARY_CHARS, cap)),
            "chars": min(len(text), cap),
            "truncated": len(text) > cap,
        })
    out["file_notes"] = notes


def _tests(out: dict, w: _Walk, deps: set[str]) -> None:
    """Where the tests are, how many, and what runs them.

    ⚠️ A RUNNER IS PROVED, NEVER GUESSED. Three things can prove one: a dependency
    (`pytest` in a manifest), a config file (`pytest.ini`, `jest.config.js`) or a
    file-name pattern that admits only one runner (`test_x.py`). `foo.test.ts`
    proves none of them, so a project with only those and no manifest reports its
    test files and an **empty** runner list — which is the honest answer, and the
    one that stops `_commands` writing a `## Test Commands` line that runs nothing.
    """
    runners: list[str] = []
    for label, aliases, files in TEST_RUNNERS:
        if any(a in deps for a in aliases) or any(f in w.root_names or f in w.interesting
                                                  for f in files):
            runners.append(label)
    runners.extend(sorted(w.proved_runners))
    dirs = sorted(w.test_dirs, key=lambda d: (d.count("/"), d))[:MAX_TEST_DIRS]
    out["tests"] = {"dirs": dirs, "files": w.test_files,
                    "runners": sorted(set(runners))}


def _build(out: dict, w: _Walk) -> None:
    systems: list[str] = []
    files: list[str] = []
    for label, name in BUILD_FILES:
        if name in w.root_names:
            files.append(name)
            if label not in systems:
                systems.append(label)
    out["build"] = {"systems": systems, "files": files}


def _config_files(out: dict, w: _Walk) -> None:
    # Root level only, and by declared name only. ⚠️ A pattern sweep here (`*.ini`,
    # `*.env*`) is what puts `.env` on a list that Task 31 writes into a committed
    # file — the name alone tells a reader the project has secrets and where.
    out["config_files"] = [n for n in CONFIG_FILES if n in w.root_names][:MAX_CONFIG_FILES]


def _commands(out: dict, w: _Walk, texts: dict[str, str], deps: set[str]) -> None:
    """Runnable commands, derived from what the project itself declares."""
    cmds: dict[str, list[dict]] = {k: [] for k in COMMAND_KINDS}
    seen: set[str] = set()

    def add(kind: str, cmd: str, src: str):
        if not cmd or cmd in seen or len(cmds[kind]) >= MAX_COMMANDS_PER_KIND:
            return
        seen.add(cmd)
        cmds[kind].append({"cmd": cmd, "from": src})

    def kind_of(name: str) -> str:
        low = name.lower()
        for kind, aliases in SCRIPT_KINDS.items():
            if low in aliases:
                return kind
        for kind, aliases in SCRIPT_KINDS.items():
            if any(low.startswith((a + ":", a + "-")) for a in aliases):
                return kind
        return "other"

    managers = {m["key"] for m in out.get("package_managers") or []}

    pkg = texts.get("package.json", "")
    if pkg:
        runner = ("yarn" if "yarn" in managers else
                  "pnpm" if "pnpm" in managers else
                  "bun" if "bun" in managers else "npm")
        add("install", f"{runner} install", "package.json")
        try:
            scripts = (json.loads(pkg).get("scripts") or {})
        except Exception:
            scripts = {}
        if isinstance(scripts, dict):
            for name in list(scripts)[:24]:
                verb = "run " if runner in ("npm", "pnpm", "bun") else ""
                add(kind_of(str(name)), f"{runner} {verb}{name}".strip(), "package.json script")

    make = texts.get("Makefile", "") or texts.get("GNUmakefile", "")
    if make:
        for target in _MAKE_TARGET.findall(make)[:24]:
            if target.startswith("."):
                continue
            add(kind_of(target), f"make {target}", "Makefile target")

    if "pip" in managers:
        manifest = next((m["manifest"] for m in out["package_managers"]
                         if m["key"] == "pip"), "requirements.txt")
        # ⚠️ `-r` IS FOR A REQUIREMENTS FILE. `pip` also stands in for a bare
        # PEP-621 `pyproject.toml`, and `pip install -r pyproject.toml` is not a
        # command — it is a command-shaped error that Task 31 would commit to a
        # file the agent later runs.
        add("install", "pip install -e ." if manifest.endswith(".toml")
            else f"pip install -r {manifest}", "pip")
    if "poetry" in managers:
        add("install", "poetry install", "Poetry")
    if "pdm" in managers:
        add("install", "pdm install", "PDM")
    if "uv" in managers:
        add("install", "uv sync", "uv")
    if "pipenv" in managers:
        add("install", "pipenv install", "Pipenv")
    if "bundler" in managers:
        add("install", "bundle install", "Bundler")
    if "composer" in managers:
        add("install", "composer install", "Composer")

    if "cargo" in managers:
        add("build", "cargo build", "Cargo")
        add("test", "cargo test", "Cargo")
        add("dev", "cargo run", "Cargo")
    if "go" in managers:
        add("build", "go build ./...", "Go modules")
        add("test", "go test ./...", "Go modules")
    if "maven" in managers:
        add("build", "mvn package", "Maven")
        add("test", "mvn test", "Maven")
    if "gradle" in managers:
        add("build", "./gradlew build", "Gradle")
        add("test", "./gradlew test", "Gradle")

    runners = set((out.get("tests") or {}).get("runners") or [])
    if "pytest" in runners:
        dirs = (out.get("tests") or {}).get("dirs") or []
        # The discovered test directory, not a guessed `tests/`. In THIS repository
        # the suite lives in `.github/tests/`, and a hard-coded `pytest tests/`
        # would print a command that collects nothing.
        target = dirs[0] if dirs else ""
        add("test", f"python -m pytest {target}".strip(), "pytest")
    if "Jest" in runners and "npx jest" not in seen:
        add("test", "npx jest", "Jest")
    if "Vitest" in runners:
        add("test", "npx vitest run", "Vitest")
    if "RSpec" in runners:
        add("test", "bundle exec rspec", "RSpec")

    if "ruff" in deps or "ruff.toml" in w.root_names or ".ruff.toml" in w.root_names \
            or "[tool.ruff]" in texts.get("pyproject.toml", ""):
        add("lint", "ruff check .", "ruff config")
    if "mypy" in deps or "[tool.mypy]" in texts.get("pyproject.toml", ""):
        add("lint", "mypy .", "mypy config")
    if "manage.py" in w.root_names:
        add("dev", "python manage.py runserver", "Django")
        add("test", "python manage.py test", "Django")
    if "docker-compose.yml" in w.root_names or "docker-compose.yaml" in w.root_names:
        add("dev", "docker compose up", "docker-compose")

    out["commands"] = cmds


def _conventions(out: dict, base: Path, w: _Walk, texts: dict[str, str]) -> None:
    """Observations a contributor would be told on day one, each with evidence."""
    notes: list[str] = []

    tests = out.get("tests") or {}
    if tests.get("dirs"):
        runner = (tests.get("runners") or [""])[0]
        notes.append(f"Tests live in {tests['dirs'][0]}/"
                     f"{f' ({runner})' if runner else ''} — {tests.get('files', 0)} test file(s)")
    elif tests.get("files"):
        notes.append(f"{tests['files']} test file(s), beside the code rather than in a test tree")
    else:
        notes.append("No test files were found")

    pyproj = texts.get("pyproject.toml", "")
    if m := re.search(r"^\s*line-length\s*=\s*(\d+)", pyproj, re.M):
        notes.append(f"Line length {m.group(1)} (ruff)")
    elif m := re.search(r"^\s*max-line-length\s*=\s*(\d+)", texts.get("setup.cfg", ""), re.M):
        notes.append(f"Line length {m.group(1)} (flake8)")

    if m := re.search(r"^\s*target-version\s*=\s*[\"']?py(\d)(\d+)", pyproj, re.M):
        notes.append(f"Targets Python {m.group(1)}.{m.group(2)}")

    editor = texts.get(".editorconfig", "")
    if m := re.search(r"^\s*indent_style\s*=\s*(\w+)", editor, re.M):
        style = m.group(1)
        size = re.search(r"^\s*indent_size\s*=\s*(\d+)", editor, re.M)
        notes.append(f"Indentation: {style}{f' {size.group(1)}' if size else ''} (.editorconfig)")
    else:
        tabs = spaces = 0
        widths: dict[int, int] = {}
        for path in w.source_samples[:MAX_INDENT_SAMPLE]:
            for line in _read_text(path, 40_000).splitlines()[:200]:
                if not line.strip():
                    continue
                if line.startswith("\t"):
                    tabs += 1
                elif line.startswith(" "):
                    spaces += 1
                    n = len(line) - len(line.lstrip(" "))
                    if n:
                        widths[n] = widths.get(n, 0) + 1
        if tabs or spaces:
            if tabs > spaces:
                notes.append(f"Indentation: tabs (sampled {len(w.source_samples)} file(s))")
            else:
                common = min((n for n, c in widths.items() if c >= 3), default=0)
                notes.append(f"Indentation: spaces{f' ({common})' if common else ''} "
                             f"(sampled {len(w.source_samples)} file(s))")

    if any(sd in w.top for sd in ("src", "lib")):
        notes.append("`src/`-style layout — source is not at the repository root")
    if w.has_ci:
        notes.append("CI runs from .github/workflows/ (or .gitlab equivalent)")
    elif ".github" in w.top:
        notes.append("`.github/` present — CI or repository configuration lives there")
    if ".pre-commit-config.yaml" in w.root_names:
        notes.append("pre-commit hooks are configured")
    docs = out.get("docs") or []
    agent_docs = [d for d in docs if d.lower() in ("claude.md", "agents.md", "agent.md")]
    if agent_docs:
        notes.append(f"Agent guidance already exists in {agent_docs[0]} — read it before editing")
    if "LICENSE" in w.root_names or "LICENSE.md" in w.root_names:
        notes.append("Licensed (LICENSE at the root)")

    out["conventions"] = notes[:MAX_CONVENTIONS]


def _git(out: dict, base: Path) -> None:
    """The repository, from `core.gitstate` — the one git reader.

    ⚠️ `snapshot()` and `describe()` are called on the SAME path, so the TTL cache
    makes this one set of `git` subprocesses rather than two. And ⚠️ a partial read
    is reported as `unknown`, never as a clean tree — see the module docstring.
    """
    from agent2.core import gitstate as _git_state

    snap = _git_state.snapshot(base) or {}
    repo = bool(snap.get("repo"))
    branch = str(snap.get("branch") or "")
    head = str(snap.get("head") or "")
    commits = list(snap.get("commits") or [])
    out["git"] = {
        "repo": repo,
        # Learned that it IS a repository and nothing else: `gitstate._read` stops
        # at the first subprocess that succeeds, so this state is indistinguishable
        # from a clean checkout — and "clean" is the reading that would let Task 31
        # write "no uncommitted changes" over a dirty tree.
        "unknown": bool(repo and not branch and not head),
        "branch": branch,
        "detached": bool(snap.get("detached")),
        # `dirty` counts changed PATHS once; the three below double-count a path
        # that is both staged and modified, which is why the summary uses this one.
        "changed": int(snap.get("dirty") or 0),
        "staged": int(snap.get("staged") or 0),
        "unstaged": int(snap.get("unstaged") or 0),
        "untracked": int(snap.get("untracked") or 0),
        "ahead": int(snap.get("ahead") or 0),
        "behind": int(snap.get("behind") or 0),
        "upstream": str(snap.get("upstream") or ""),
        "describe": _git_state.describe(base),
        # Copied out rather than aliased: `snapshot()` returns a SHALLOW copy, so
        # the commit dicts inside it are the cache's own objects.
        "commits": [dict(c) for c in commits[:_git_state.LOG_COUNT]],
    }


# ── The one entry point ───────────────────────────────────────────────────────
def scan(root=None) -> dict:
    """Analyse a project and return the complete result. Never raises.

    ⚠️ NO PATH ARGUMENT REACHES THIS FROM A USER. `/init` and `GET /api/project`
    both call it with no argument, so the scanned tree is always
    `workspace.root()` — the security boundary itself. `root=` exists for tests
    and for Tasks 30/31, which already hold a validated root.
    """
    out = empty()
    t0 = time.time()
    out["at"] = t0
    try:
        base = Path(str(root)) if root else _workspace_root()
    except Exception:
        base = None
    if base is None:
        out["errors"].append({"where": "root", "error": "no workspace root"})
        return out
    try:
        ok = base.is_dir()
    except Exception:
        ok = False
    if not ok:
        out["root"] = str(base)
        out["errors"].append({"where": "root", "error": "not a directory"})
        return out

    out["root"] = str(base)
    out["name"] = base.name or str(base)

    w = _step(out, "walk", _walk, base, out) or _Walk()
    texts = _step(out, "manifests", _manifests, base, w) or {}
    deps = _step(out, "dependencies", _dependencies, texts) or set()

    _step(out, "languages", _languages, out, w)
    _step(out, "structure", _structure, out, w)
    _step(out, "docs", _docs, out, base, w)
    _step(out, "package_managers", _package_managers, out, w, texts)
    _step(out, "frameworks", _frameworks, out, w, deps)
    _step(out, "entry_points", _entry_points, out, base, w, texts)
    # After `_entry_points`: the files the project itself declares as its start are
    # the first ones whose own description is worth reading.
    _step(out, "notes", _notes, out, base, w)
    _step(out, "tests", _tests, out, w, deps)
    _step(out, "build", _build, out, w)
    _step(out, "config_files", _config_files, out, w)
    # After `_package_managers` and `_tests`: it reads both of their results.
    _step(out, "commands", _commands, out, w, texts, deps)
    _step(out, "conventions", _conventions, out, base, w, texts)
    _step(out, "git", _git, out, base)

    out["files"] = w.files
    out["dirs"] = w.dirs
    out["bytes"] = w.bytes
    out["elapsed_ms"] = round((time.time() - t0) * 1000.0, 1)

    try:
        from agent2.core import logging as alog
        alog.project_scanned(
            root=str(base), files=w.files, languages=len(out["languages"]),
            managers=len(out["package_managers"]), tests=out["tests"]["files"],
            truncated=out["truncated"], errors=len(out["errors"]),
            ms=out["elapsed_ms"],
        )
    except Exception:
        pass
    return out


def summary(report: dict) -> str:
    """One line for a status bar or a log — `Python · pip · 1836 tests · main`."""
    rep = report or {}
    bits: list[str] = []
    if rep.get("primary_language"):
        bits.append(str(rep["primary_language"]))
    mgrs = [str(m.get("label") or "") for m in (rep.get("package_managers") or [])]
    if mgrs:
        bits.append(" + ".join(m for m in mgrs[:2] if m))
    tests = (rep.get("tests") or {}).get("files") or 0
    if tests:
        bits.append(f"{tests} test file(s)")
    git = rep.get("git") or {}
    if git.get("describe"):
        bits.append(str(git["describe"]))
    elif git.get("repo") and git.get("unknown"):
        bits.append("git state unknown")
    return " · ".join(bits)
