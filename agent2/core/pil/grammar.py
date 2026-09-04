# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2.core.pil.grammar
────────────────────────
Module 2 — Grammar Enhancement (offline, opt-in).

Runs ONLY when the user enables it in settings. Before a prompt is sent to the
AI model it:

    • corrects spelling (common typos + the user's learned vocabulary)
    • fixes capitalization (sentence starts, standalone "i")
    • normalizes punctuation and spacing

It MUST NEVER change the meaning of the request, and it MUST NEVER touch
technical content. Everything below is masked out before any rule runs and
restored verbatim afterwards:

    • fenced ```code``` blocks and `inline code`
    • URLs and file paths
    • API / identifier names (camelCase, snake_case, dotted, hyphenated)
    • anything containing a digit (versions, hex, ports …)

This is a deterministic rule engine — no model, no network — so it is safe on
the hot path and fully offline. It returns the corrected text plus the list of
edits it made (for transparency in the UI).
"""

import re

# ── Protected-span patterns (masked before correction, restored after) ────────
_PROTECT = [
    re.compile(r"```.*?```", re.DOTALL),          # fenced code blocks
    re.compile(r"`[^`]+`"),                        # inline code
    re.compile(r"https?://\S+"),                   # URLs
    re.compile(r"\b[\w.\-]+://\S+"),               # other schemes (ftp://, ssh://)
    re.compile(r"(?:[A-Za-z]:\\|\.{0,2}/)[\w./\\\-]+"),  # win + posix paths
    re.compile(r"\b\w+\.(?:py|js|ts|tsx|jsx|html|css|json|md|txt|yml|yaml|toml|sh|c|cpp|go|rs|java|rb|php)\b"),  # filenames
    re.compile(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z]*\b"),     # camelCase / PascalCase-ish
    re.compile(r"\b\w*_\w+\b"),                    # snake_case
    re.compile(r"\b[\w\-]+\.[\w.\-]+\b"),          # dotted names (node.js, a.b.c)
    # anything with a digit (versions, hex, ports…). Excludes the \x00 sentinel
    # so it never re-masks an existing placeholder into a nested one.
    re.compile(r"[^\s\x00]*\d[^\s\x00]*"),
    re.compile(r"@\w+"),                           # @handles / decorators
    re.compile(r"--?[\w\-]+"),                     # CLI flags (-v, --help)
]

# All protected spans combined into ONE alternation. Masking does a single pass
# over the ORIGINAL text with this — priority is left-to-right (fenced code first,
# CLI flags last), exactly matching the list order above. re.DOTALL is only needed
# by the fenced-code pattern; no other pattern uses an unescaped ".", so applying
# it globally is safe.
_PROTECT_RE = re.compile("|".join(f"(?:{p.pattern})" for p in _PROTECT), re.DOTALL)

# Common misspellings → correction. Deliberately conservative; only unambiguous,
# non-technical everyday words. Applied case-insensitively, preserving case.
_TYPOS = {
    "teh": "the", "adn": "and", "recieve": "receive", "recieved": "received",
    "seperate": "separate", "definately": "definitely", "occured": "occurred",
    "occurance": "occurrence", "wich": "which", "thier": "their", "alot": "a lot",
    "untill": "until", "begining": "beginning", "beleive": "believe",
    "accross": "across", "acheive": "achieve", "sucessful": "successful",
    "succesful": "successful", "enviroment": "environment", "existance": "existence",
    "neccessary": "necessary", "necesary": "necessary", "wanna": "want to",
    "gonna": "going to", "cant": "can't", "dont": "don't", "doesnt": "doesn't",
    "wont": "won't", "isnt": "isn't", "wasnt": "wasn't", "didnt": "didn't",
    "im": "I'm", "ive": "I've", "id": "I'd", "ill": "I'll",
    "responsize": "responsive", "responive": "responsive",
    "fucntion": "function", "funtion": "function", "retrun": "return",
    "lenght": "length", "widht": "width", "heigth": "height",
}

_WORD_RE = re.compile(r"[A-Za-z']+")
_SENT_SPLIT = re.compile(r"([.!?]\s+)")


def _match_case(original: str, replacement: str) -> str:
    """Apply *original*'s casing pattern to *replacement*."""
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def _mask(text: str) -> tuple[str, list[str]]:
    """Replace every protected span with a placeholder token. Returns the masked
    text and the ordered list of originals to restore.

    Masking is a SINGLE pass over the original text (via the combined
    ``_PROTECT_RE``), so a placeholder we emit is never re-scanned by a later
    pattern — that used to nest placeholders (the ``\\x00N\\x00`` token contains a
    digit) and leak raw sentinels into the output."""
    saved: list[str] = []

    def _store(m: re.Match) -> str:
        saved.append(m.group(0))
        # Placeholder is padding-free and itself protected (no letters to touch).
        return f"\x00{len(saved) - 1}\x00"

    masked = _PROTECT_RE.sub(_store, text)
    return masked, saved


def _unmask(text: str, saved: list[str]) -> str:
    def _restore(m: re.Match) -> str:
        idx = int(m.group(1))
        return saved[idx] if 0 <= idx < len(saved) else m.group(0)
    return re.sub(r"\x00(\d+)\x00", _restore, text)


def enhance(text: str, *, vocab_lookup=None) -> tuple[str, list[str]]:
    """Grammar-correct *text*, protecting all technical content.

    *vocab_lookup* is an optional callable ``token -> bool`` used to spare the
    user's learned personal vocabulary from spell-correction (so their project
    names and jargon are never "fixed"). Returns ``(corrected_text, edits)``.
    """
    if not text or not text.strip():
        return text, []

    masked, saved = _mask(text)
    edits: list[str] = []

    # ── Spacing / punctuation normalization (on masked text — safe) ───────────
    before = masked
    masked = re.sub(r"[ \t]{2,}", " ", masked)                 # collapse spaces
    masked = re.sub(r"\s+([,.!?;:])", r"\1", masked)           # no space before punct
    masked = re.sub(r"([,;:])(?=[^\s\x00])", r"\1 ", masked)   # space after comma/;
    masked = re.sub(r"([.!?])(?=[A-Za-z])", r"\1 ", masked)    # space after sentence end
    if masked != before:
        edits.append("normalized spacing/punctuation")

    # ── Word-level corrections (typos + "i" → "I") ────────────────────────────
    def _fix_word(m: re.Match) -> str:
        w = m.group(0)
        low = w.lower()
        # Curated everyday typos win over everything: a misspelling stays a
        # misspelling even if the user has typed it enough to learn it. The typo
        # list is deliberately non-technical, so this never "corrects" real jargon.
        if low in _TYPOS:
            fixed = _match_case(w, _TYPOS[low])
            edits.append(f"{w} → {fixed}")
            return fixed
        # Otherwise never touch the user's own learned vocabulary (project names,
        # jargon) — it is correct by definition.
        if vocab_lookup is not None:
            try:
                if vocab_lookup(low):
                    return w
            except Exception:
                pass
        if w == "i":
            edits.append("i → I")
            return "I"
        return w

    masked = _WORD_RE.sub(_fix_word, masked)

    # ── Sentence-start capitalization ─────────────────────────────────────────
    masked = _capitalize_sentences(masked, edits, vocab_lookup)

    corrected = _unmask(masked, saved)

    # Trailing whitespace cleanup (never adds/removes meaning).
    corrected = corrected.rstrip() + ("\n" if text.endswith("\n") else "")
    corrected = corrected if corrected.strip() else text
    return corrected, edits


def _capitalize_sentences(text: str, edits: list[str], vocab_lookup=None) -> str:
    """Capitalize the first alphabetic character of each sentence. Skips spans
    that begin with a placeholder (protected technical content) and never
    capitalizes a token the user has in their learned vocabulary — a personal
    project name like ``myproj`` must stay lowercase, capitalizing it would be
    modifying technical terminology."""
    parts = _SENT_SPLIT.split(text)
    out: list[str] = []
    changed = False
    for seg in parts:
        if not seg or _SENT_SPLIT.fullmatch(seg):
            out.append(seg)
            continue
        # Find the first non-space char; skip if it's inside a protected span.
        i = 0
        while i < len(seg) and seg[i].isspace():
            i += 1
        if i < len(seg) and seg[i].isalpha() and seg[i].islower():
            if not _is_spared_word(seg, i, vocab_lookup):
                seg = seg[:i] + seg[i].upper() + seg[i + 1:]
                changed = True
        out.append(seg)
    if changed:
        edits.append("capitalized sentence start")
    return "".join(out)


def _is_spared_word(seg: str, i: int, vocab_lookup) -> bool:
    """True if the word starting at *seg[i]* is protected learned vocabulary."""
    if vocab_lookup is None:
        return False
    j = i
    while j < len(seg) and (seg[j].isalpha() or seg[j] == "'"):
        j += 1
    word = seg[i:j].lower()
    try:
        return bool(word and vocab_lookup(word))
    except Exception:
        return False
