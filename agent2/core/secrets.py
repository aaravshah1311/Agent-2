# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""
agent2/core/secrets.py
──────────────────────
THE SecretStore (Task 16). Every credential the app persists goes through here,
and what lands in `agent2.db` is a **reference**, never the secret.

What this closes
────────────────
Three tables held live credentials in plaintext: `api_keys.api_key` (Gemini),
`providers.api_key` (any custom endpoint) and `mcp_config.security_key` (ZAP).
`agent2.db` is a file people copy — a backup, a `docker cp`, a mounted volume, a
screen-shared `sqlite3 agent2.db 'select * from api_keys'`. Any one of those handed
over working keys. `web_sessions` already stored hashes for exactly this reason
(migration 13); this module extends the same property to credentials that, unlike
a session, cannot be replaced by hashing because the app has to *send* them.

⚠️ WHAT THIS DOES AND DOES NOT PROTECT — STATED PLAINLY
The encrypted backend keeps the ciphertext in the DB and the master key **outside**
it, by default in `~/.agent2/secret.key`. So it defends against exposure of the
database *alone*, which is the realistic accident: a stray backup, a committed
file, a copied `/data` volume, a shared screen. It does **not** defend against an
attacker who can already read arbitrary files as this user — nothing that has to
decrypt unattended can. Saying otherwise would be the security theatre rule 29
exists to prevent, so `describe()["protects_against"]` says it in the payload too.

The backends, in the order they are tried
─────────────────────────────────────────
1. `keyring` — the real OS credential store (Windows Credential Manager, macOS
   Keychain, libsecret). Used only when the package is installed AND a probe
   round-trip actually succeeds, because `keyring` "works" on a headless Linux box
   right up until you store something.
2. `encrypted` — AES-256-GCM when `cryptography` is importable, otherwise a
   stdlib BLAKE2b keystream with an HMAC-SHA256 tag. Both are decrypted by scheme
   tag, so a store written by one is readable by the other's build as long as its
   own primitive is present.
3. `plaintext` — announced, never silent. Reached only when no durable key
   location can be written (a read-only home *and* a read-only DB directory).

⚠️ THE PLAINTEXT FALLBACK EXISTS BECAUSE THE ALTERNATIVE IS DATA LOSS.
The tempting fallback is a random in-memory key, which looks strictly safer and is
catastrophic: the app would write ciphertext it can never decrypt again, so every
key silently stops working on the next restart and the *user's* credential is gone,
not merely exposed. A store that cannot persist its master key must degrade to
storing the value, loudly — see `_master_key`.

⚠️ NOTHING HERE MAY LOSE A CREDENTIAL. THAT OUTRANKS ENCRYPTING IT.
Two rules carry it:
  * `resolve()` passes a NON-ref through unchanged. Every legacy plaintext row
    keeps working forever, with or without a migration, so an install that fails
    to migrate degrades to "not yet encrypted" and never to "signed out".
  * `seal()` verifies by reading back what it wrote, and returns the original
    value if the round trip disagrees. A caller therefore cannot store a reference
    that does not resolve. This is what makes the migration in `database.py`
    safe to run unattended: it replaces plaintext only after the replacement has
    been proven to read back byte-identical.

⚠️ A REF IS NOT A SECRET, BUT IT IS NOT PUBLIC EITHER.
`a2s:enc:…` is ciphertext, safe to show only in the sense that it is useless
without the key file. Surfaces still print `state.mask_secret()`, never a ref: a
ref is long, looks like a credential to a human, and pasting one into a bug report
would hand over the ciphertext for offline work. `describe()` never includes one.

Layer: config → secrets → database · llm/keys · llm/providers · integrations/state.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as _rand
import threading
from pathlib import Path

from agent2.core import logging as audit

#: The one prefix that marks a stored value as a reference rather than a secret.
#: Deliberately unlike anything a real credential looks like — Gemini keys start
#: `AIza`, OpenAI `sk-`, Anthropic `sk-ant-` — so `is_ref()` can never be fooled
#: by a credential and `resolve()` can never mangle one.
PREFIX = "a2s:"

SCHEME_KEYRING = "kr"
SCHEME_AES = "enc:a1"      # AES-256-GCM  (cryptography)
SCHEME_BLAKE = "enc:b1"    # BLAKE2b keystream + HMAC-SHA256 (stdlib only)
SCHEME_PLAIN = "plain"

BACKEND_KEYRING = "keyring"
BACKEND_ENCRYPTED = "encrypted"
BACKEND_PLAINTEXT = "plaintext"

KEYRING_SERVICE = "agent2"
_KEY_FILE_NAME = "secret.key"
_MASTER_BYTES = 32

_LOCK = threading.RLock()
_MASTER: bytes | None = None
_MASTER_SOURCE = ""
_BACKEND: str | None = None
_ANNOUNCED: set[str] = set()


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _announce(key: str, level_fields: dict) -> None:
    """Log a backend decision once per process. A per-call line would flood."""
    with _LOCK:
        if key in _ANNOUNCED:
            return
        _ANNOUNCED.add(key)
    audit.event("secrets.backend", **level_fields)


# ── Master key ────────────────────────────────────────────────────────────────
def key_file() -> Path:
    """Where the master key lives.

    ⚠️ Defaults to the USER's home, not to the DB directory, and that is the whole
    point of the encrypted backend. A key file beside `agent2.db` travels with
    every copy of it — including the `/data` volume Docker mounts — which would
    make the ciphertext decorative. `AGENT2_SECRET_KEY_FILE` overrides for the
    deployments where home is not durable.
    """
    override = _env("AGENT2_SECRET_KEY_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent2" / _KEY_FILE_NAME


def _db_dir_key_file() -> Path:
    """The second-choice location: beside the DB.

    Weaker (it travels with a copied volume) but still better than plaintext, and
    it is the only writable place on a container with a read-only home.
    """
    try:
        from agent2.config import DB
        return Path(DB).expanduser().resolve().parent / f".agent2-{_KEY_FILE_NAME}"
    except Exception:
        return Path.cwd() / f".agent2-{_KEY_FILE_NAME}"


def _harden(path: Path) -> None:
    """Best-effort owner-only permissions. Never raises, never blocks.

    ⚠️ PORTABILITY IS THE CONTRACT, NOT THE PERMISSION. Every platform reaches the
    same *outcome* — the key is written, outside the DB directory — and the ACL is
    a bonus where the OS offers one. `chmod 0600` is honoured on POSIX and is close
    to meaningless on Windows, so Windows additionally gets an `icacls` grant when
    that tool is actually on PATH. If neither works the store still functions, which
    is why nothing here is allowed to raise or to decide anything.

    `shutil.which` before spawning, and a hard `timeout`, because "hardening the
    key file" must never be the reason the app fails to start on a locked-down box
    where `icacls` is absent or a policy makes it hang.
    """
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        return
    try:
        import shutil
        import subprocess
        tool = shutil.which("icacls")
        user = os.environ.get("USERNAME") or ""
        if not tool or not user:
            return
        subprocess.run([tool, str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                       capture_output=True, timeout=10, check=False)
    except Exception:
        pass


def _read_key(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes().strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        material = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except Exception:
        material = raw
    return material if len(material) >= 16 else None


def _write_key(path: Path, material: bytes) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create, so two processes racing on first start cannot each
        # write a different key and leave the loser unable to decrypt the winner's
        # ciphertext. The loser re-reads instead.
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, base64.urlsafe_b64encode(material))
        finally:
            os.close(fd)
    except FileExistsError:
        return _read_key(path) is not None
    except Exception:
        return False
    _harden(path)
    return True


def _master_key() -> bytes | None:
    """The 32-byte master key, or None when no durable location is writable.

    None is the signal to degrade to `plaintext`. See the module docstring for why
    an in-memory key is the one fallback this module refuses to have.
    """
    global _MASTER, _MASTER_SOURCE
    with _LOCK:
        if _MASTER is not None:
            return _MASTER

        supplied = _env("AGENT2_SECRET_MASTER_KEY")
        if supplied:
            # Accept base64 or any passphrase: a operator-supplied string is
            # stretched rather than rejected, because refusing a short value here
            # means the deployment falls back to plaintext, which is worse.
            try:
                material = base64.urlsafe_b64decode(
                    supplied + "=" * (-len(supplied) % 4))
            except Exception:
                material = b""
            if len(material) < _MASTER_BYTES:
                material = hashlib.blake2b(supplied.encode("utf-8"),
                                           digest_size=_MASTER_BYTES,
                                           person=b"a2-master").digest()
            _MASTER, _MASTER_SOURCE = material, "env"
            return _MASTER

        for path in (key_file(), _db_dir_key_file()):
            existing = _read_key(path)
            if existing:
                _MASTER, _MASTER_SOURCE = existing, str(path)
                return _MASTER
        for path in (key_file(), _db_dir_key_file()):
            if _write_key(path, _rand.token_bytes(_MASTER_BYTES)):
                material = _read_key(path)
                if material:
                    _MASTER, _MASTER_SOURCE = material, str(path)
                    return _MASTER
        return None


def master_source() -> str:
    _master_key()
    return _MASTER_SOURCE


def _subkeys() -> tuple[bytes, bytes]:
    """Separate encryption and MAC keys from the one master.

    Reusing one key for both is the classic misuse; `person=` is BLAKE2b's own
    domain separation, so this needs no extra construction.
    """
    master = _master_key() or b""
    enc = hashlib.blake2b(master, digest_size=32, person=b"a2-enc").digest()
    mac = hashlib.blake2b(master, digest_size=32, person=b"a2-mac").digest()
    return enc, mac


# ── Backend selection ─────────────────────────────────────────────────────────
def _keyring_module():
    """The `keyring` package, but only if it can actually store something.

    ⚠️ Importability is not availability. On a headless Linux box `keyring`
    imports fine and its backend is `fail.Keyring`, which raises on the first
    `set_password` — i.e. exactly when a user is adding their API key. The probe
    round-trips a throwaway value so that failure happens here, at selection time,
    where the answer is "use the encrypted backend" instead of "lose the key".
    """
    if _env("AGENT2_SECRET_BACKEND").lower() in (BACKEND_ENCRYPTED,
                                                 BACKEND_PLAINTEXT):
        return None
    try:
        import keyring
    except Exception:
        return None
    try:
        probe_user = "__a2_probe__"
        keyring.set_password(KEYRING_SERVICE, probe_user, "ok")
        got = keyring.get_password(KEYRING_SERVICE, probe_user)
        try:
            keyring.delete_password(KEYRING_SERVICE, probe_user)
        except Exception:
            pass
        return keyring if got == "ok" else None
    except Exception:
        return None


def backend() -> str:
    """Which backend is in force. Resolved once, then cached for the process."""
    global _BACKEND
    with _LOCK:
        if _BACKEND is not None:
            return _BACKEND
    forced = _env("AGENT2_SECRET_BACKEND").lower()
    chosen = ""
    if forced == BACKEND_PLAINTEXT:
        chosen = BACKEND_PLAINTEXT
        _announce("forced-plain", {"backend": chosen, "why": "forced by env"})
    elif _keyring_module() is not None:
        chosen = BACKEND_KEYRING
    elif _master_key() is not None:
        chosen = BACKEND_ENCRYPTED
    else:
        chosen = BACKEND_PLAINTEXT
        _announce("no-key-location", {
            "backend": chosen,
            "why": "no writable location for the master key — secrets are stored "
                   "as-is rather than encrypted with a key that cannot survive a "
                   "restart",
            "tried": f"{key_file()} , {_db_dir_key_file()}",
        })
    with _LOCK:
        _BACKEND = chosen
    if chosen != BACKEND_PLAINTEXT:
        _announce(f"chosen-{chosen}", {"backend": chosen,
                                       "key_source": master_source()
                                       if chosen == BACKEND_ENCRYPTED else "os"})
    return chosen


def reset_cache() -> None:
    """Forget the resolved backend and master key.

    For tests and for a deployment that changes `AGENT2_SECRET_*` at runtime. Not
    called anywhere in the app: re-resolving mid-process would mean two different
    backends wrote refs during one run, and both must remain readable — which they
    do, because `resolve()` dispatches on the ref's own scheme tag rather than on
    the current backend.
    """
    global _MASTER, _MASTER_SOURCE, _BACKEND
    with _LOCK:
        _MASTER, _MASTER_SOURCE, _BACKEND = None, "", None
        _ANNOUNCED.clear()


# ── Encryption primitives ─────────────────────────────────────────────────────
def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM
    except Exception:
        return None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _encrypt(value: str) -> str:
    """Return a ref for *value*, preferring AES-GCM."""
    plain = value.encode("utf-8")
    enc_key, mac_key = _subkeys()
    aes = _aesgcm()
    if aes is not None:
        nonce = _rand.token_bytes(12)
        blob = nonce + aes(enc_key).encrypt(nonce, plain, None)
        return f"{PREFIX}{SCHEME_AES}:{_b64e(blob)}"

    # Stdlib-only path: BLAKE2b in counter mode for the keystream, HMAC-SHA256
    # over nonce||ciphertext for integrity. Encrypt-then-MAC, so a tampered blob
    # is rejected before anything tries to decode it as UTF-8.
    nonce = _rand.token_bytes(16)
    stream = bytearray()
    counter = 0
    while len(stream) < len(plain):
        stream += hashlib.blake2b(nonce + counter.to_bytes(8, "big"),
                                  key=enc_key, digest_size=64).digest()
        counter += 1
    cipher = bytes(a ^ b for a, b in zip(plain, stream, strict=False))
    tag = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
    return f"{PREFIX}{SCHEME_BLAKE}:{_b64e(nonce + tag + cipher)}"


def _decrypt(scheme: str, payload: str) -> str | None:
    enc_key, mac_key = _subkeys()
    try:
        blob = _b64d(payload)
    except Exception:
        return None
    if scheme == SCHEME_AES:
        aes = _aesgcm()
        if aes is None:
            return None
        try:
            return aes(enc_key).decrypt(blob[:12], blob[12:], None).decode("utf-8")
        except Exception:
            return None
    if scheme == SCHEME_BLAKE:
        if len(blob) < 48:
            return None
        nonce, tag, cipher = blob[:16], blob[16:48], blob[48:]
        want = hmac.new(mac_key, nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, want):
            return None
        stream = bytearray()
        counter = 0
        while len(stream) < len(cipher):
            stream += hashlib.blake2b(nonce + counter.to_bytes(8, "big"),
                                      key=enc_key, digest_size=64).digest()
            counter += 1
        try:
            return bytes(a ^ b for a, b in
                         zip(cipher, stream, strict=False)).decode("utf-8")
        except Exception:
            return None
    return None


# ── The public store ──────────────────────────────────────────────────────────
def is_ref(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(value: str, *, namespace: str = "misc", name: str = "") -> str:
    """Store *value* and return the reference to put in the database row.

    ⚠️ VERIFIES BY READING BACK, AND RETURNS THE PLAINTEXT IF THAT FAILS.
    A caller cannot end up persisting a reference that does not resolve — which is
    what makes the plaintext→ref migration safe to run unattended, and what keeps
    "do not silently lose existing credentials" true even when the OS keychain
    accepts a write and then forgets it (it happens: locked keychains, roaming
    profiles, a container whose D-Bus went away between two calls).

    `namespace`/`name` only matter to the keyring backend, where they become the
    account under `agent2`. They are advisory elsewhere.
    """
    text = "" if value is None else str(value)
    if not text or is_ref(text):
        return text

    kind = backend()
    ref = ""
    if kind == BACKEND_KEYRING:
        kr = _keyring_module()
        account = f"{namespace}/{name or hashlib.sha256(text.encode()).hexdigest()[:16]}"
        if kr is not None:
            try:
                kr.set_password(KEYRING_SERVICE, account, text)
                ref = f"{PREFIX}{SCHEME_KEYRING}:{account}"
            except Exception as exc:
                audit.event("secrets.seal.error", backend=kind,
                            error=str(exc)[:160])
                ref = ""
    if not ref and kind != BACKEND_PLAINTEXT:
        try:
            ref = _encrypt(text)
        except Exception as exc:
            audit.event("secrets.seal.error", backend="encrypted",
                        error=str(exc)[:160])
            ref = ""
    if not ref:
        return text

    if resolve(ref) != text:
        audit.event("secrets.seal.verify_failed", backend=kind)
        forget(ref)
        return text
    return ref


def resolve(value):
    """Turn a stored value into the usable secret.

    ⚠️ A NON-REF IS RETURNED UNCHANGED, and that is load-bearing rather than
    lenient. Every legacy plaintext row keeps working with no migration at all, so
    the worst case of a failed or partial migration is "not yet encrypted" instead
    of "the user's keys stopped working". The same property means every caller can
    wrap every read in `resolve()` unconditionally.

    A ref that cannot be resolved returns `""` — never the ref itself. Handing a
    ciphertext blob to `genai.Client(api_key=…)` would produce a baffling 400 from
    the vendor instead of the app's own "no keys configured".
    """
    if value is None:
        return value
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    body = value[len(PREFIX):]
    if body.startswith(SCHEME_KEYRING + ":"):
        kr = _keyring_module()
        if kr is None:
            audit.event("secrets.resolve.no_backend", scheme=SCHEME_KEYRING)
            return ""
        try:
            return kr.get_password(KEYRING_SERVICE,
                                   body[len(SCHEME_KEYRING) + 1:]) or ""
        except Exception:
            return ""
    if body.startswith(SCHEME_PLAIN + ":"):
        try:
            return _b64d(body[len(SCHEME_PLAIN) + 1:]).decode("utf-8")
        except Exception:
            return ""
    for scheme in (SCHEME_AES, SCHEME_BLAKE):
        if body.startswith(scheme + ":"):
            return _decrypt(scheme, body[len(scheme) + 1:]) or ""
    audit.event("secrets.resolve.unknown_scheme", head=body[:12])
    return ""


def forget(ref: str) -> None:
    """Delete the material behind a ref, where the backend holds any.

    An encrypted ref holds its own ciphertext, so deleting the DB row *is*
    deleting the secret and there is nothing to do here. Only the keyring backend
    keeps state of its own, and leaving that behind would accumulate orphaned
    entries in the user's OS credential store — something they would find, not
    understand, and reasonably resent.
    """
    if not is_ref(ref):
        return
    body = ref[len(PREFIX):]
    if not body.startswith(SCHEME_KEYRING + ":"):
        return
    kr = _keyring_module()
    if kr is None:
        return
    try:
        kr.delete_password(KEYRING_SERVICE, body[len(SCHEME_KEYRING) + 1:])
    except Exception:
        pass


def mask(value) -> str:
    """A constant-width mask for display.

    ⚠️ Deliberately reveals NOTHING, not even the length — the same reasoning as
    `integrations.state.mask_secret`, whose docstring records why `providers`'
    `k[:6]…k[-4:]` prints most of a ten-character ZAP key. This function exists so
    a surface handed a *ref* still prints something sane rather than 300 characters
    of base64 that a reader will paste into a bug report.
    """
    return "••••••••" if (value is not None and str(value)) else ""


def describe() -> dict:
    """A serialisable snapshot for `/api/health` and `/api/platform`.

    ⚠️ Carries no secret and no ref. `protects_against` is spelled out because a
    field named `encrypted: true` invites the reader to assume more than is true.
    """
    kind = backend()
    return {
        "backend": kind,
        "cipher": (SCHEME_AES if _aesgcm() is not None else SCHEME_BLAKE)
                  if kind == BACKEND_ENCRYPTED else "",
        "key_source": master_source() if kind == BACKEND_ENCRYPTED else "os",
        "protects_against": (
            "exposure of agent2.db alone (backup, copied volume, shared screen)"
            if kind != BACKEND_PLAINTEXT else "nothing — values are stored as-is"),
        "not_protected_against": (
            "an attacker who can already read this user's files"
            if kind == BACKEND_ENCRYPTED else ""),
    }
