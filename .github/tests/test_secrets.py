# Author: Aarav Shah
# Portfolio: aaravshah1311.is-great.net
# github: github.com/aaravshah1311

"""Tests for the SecretStore (agent2/core/secrets.py — Task 16).

The feature is easy to state and easy to get subtly, silently wrong: credentials
stop being readable in `agent2.db`, and *nothing stops working*. The second half is
the hard one, so most of this file is about it.

Three failure modes are specifically hunted, because each one ships green:

1. **Silent credential loss.** A store that encrypts with a key it cannot persist,
   a migration that deletes plaintext it failed to replace, or a keyring that
   accepts a write and forgets it. In every case the user's only API key is gone
   and the app reports "no keys configured" — a data-loss bug wearing a security
   feature's clothes.
2. **A migration that half-runs.** Every reader must handle a plaintext row and a
   reference interchangeably, or an interrupted upgrade bricks the install.
3. **Ciphertext escaping into a surface.** A ref is not a secret, but it is not
   public either, and it looks exactly like a credential to a human who will paste
   it into a bug report.
"""

import base64
import json
import os

import pytest

from agent2 import database as db
from agent2.core import secrets as S


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Give every test its own master key file and a clean backend decision.

    The master key and the resolved backend are process-global caches by design
    (re-resolving mid-run would mean two backends wrote refs in one process), so
    they have to be reset explicitly or the first test's choice decides the file.
    """
    monkeypatch.setenv("AGENT2_SECRET_KEY_FILE", str(tmp_path / "secret.key"))
    monkeypatch.delenv("AGENT2_SECRET_BACKEND", raising=False)
    monkeypatch.delenv("AGENT2_SECRET_MASTER_KEY", raising=False)
    S.reset_cache()
    db.init_db()
    yield
    S.reset_cache()


# ══════════════════════════════════════════════════════════════════════════════
# Round trip
# ══════════════════════════════════════════════════════════════════════════════

def test_a_sealed_value_resolves_back_byte_identical():
    ref = S.seal("AIzaSyEXAMPLE-key-0123456789")
    assert S.is_ref(ref)
    assert S.resolve(ref) == "AIzaSyEXAMPLE-key-0123456789"


def test_the_reference_does_not_contain_the_secret():
    secret = "sk-ant-super-secret-value-42"
    ref = S.seal(secret)
    assert secret not in ref
    assert secret.encode() not in base64.urlsafe_b64decode(
        ref.split(":")[-1] + "==")


@pytest.mark.parametrize("value", [
    "short", "x" * 4096, "unicode-ключ-🔑", "  leading and trailing  ",
    "with\nnewline", "=/+padding-hostile=",
])
def test_round_trip_survives_awkward_values(value):
    assert S.resolve(S.seal(value)) == value


def test_sealing_the_same_value_twice_gives_different_refs():
    """A fresh nonce per call — which is correct crypto and the reason
    `add_api_key`'s duplicate check had to stop comparing stored values."""
    a, b = S.seal("same-secret-value"), S.seal("same-secret-value")
    assert a != b
    assert S.resolve(a) == S.resolve(b) == "same-secret-value"


def test_an_empty_value_is_not_sealed():
    """`""` has to keep meaning "cleared" — `state.set_config` relies on it."""
    assert S.seal("") == ""
    assert S.seal(None) == ""


def test_sealing_a_ref_is_a_no_op():
    """Idempotence, so a migration re-run cannot double-wrap and a caller that
    passes an already-stored value through does no damage."""
    ref = S.seal("value-1")
    assert S.seal(ref) == ref
    assert S.resolve(ref) == "value-1"


# ══════════════════════════════════════════════════════════════════════════════
# The properties that stop this becoming a data-loss bug
# ══════════════════════════════════════════════════════════════════════════════

def test_a_plaintext_value_resolves_to_itself():
    """⚠️ THE SINGLE MOST LOAD-BEARING LINE IN THE MODULE.

    Every legacy row keeps working with no migration at all, so a failed or
    partial migration degrades to "not yet encrypted" and never to "the user's
    keys stopped working". It is also what lets every reader call `resolve()`
    unconditionally.
    """
    assert S.resolve("AIzaSyPLAINTEXT-legacy-key") == "AIzaSyPLAINTEXT-legacy-key"
    assert S.resolve("") == ""
    assert S.resolve(None) is None


def test_a_ref_that_cannot_be_resolved_returns_empty_not_the_ref():
    """Handing ciphertext to `genai.Client(api_key=…)` produces a baffling 400 from
    the vendor instead of the app's own "no keys configured"."""
    assert S.resolve("a2s:enc:a1:not-valid-base64!!") == ""
    assert S.resolve("a2s:unknown-scheme:whatever") == ""


def test_seal_verifies_by_reading_back_and_falls_back_to_plaintext(monkeypatch):
    """⚠️ A caller can never persist a reference that does not resolve.

    This is what makes migration 14 safe to run unattended: it only replaces
    plaintext with something already proven to read back identical. Simulated here
    by breaking `resolve` for one call.
    """
    monkeypatch.setattr(S, "resolve", lambda v: "WRONG")
    out = S.seal("real-secret-value")
    assert out == "real-secret-value", "a broken store must hand the value back"
    assert not S.is_ref(out)


def test_no_writable_key_location_degrades_to_plaintext_not_to_a_lost_key(monkeypatch, tmp_path):
    """⚠️ The fallback this module refuses to have is a random in-memory key.

    It looks strictly safer and is catastrophic: the app writes ciphertext it can
    never decrypt after a restart, so the credential is *gone* rather than merely
    exposed. A store that cannot persist its key must store the value, loudly.
    """
    unwritable = tmp_path / "nope" / "deeper" / "secret.key"
    monkeypatch.setenv("AGENT2_SECRET_KEY_FILE", str(unwritable))
    monkeypatch.setattr(S, "_db_dir_key_file", lambda: unwritable)
    monkeypatch.setattr(S, "_write_key", lambda p, m: False)
    monkeypatch.setattr(S, "_keyring_module", lambda: None)
    S.reset_cache()

    assert S.backend() == S.BACKEND_PLAINTEXT
    out = S.seal("must-not-be-lost")
    assert S.resolve(out) == "must-not-be-lost"


def test_the_plaintext_degradation_is_announced(monkeypatch, tmp_path, caplog):
    unwritable = tmp_path / "nope" / "secret.key"
    monkeypatch.setenv("AGENT2_SECRET_KEY_FILE", str(unwritable))
    monkeypatch.setattr(S, "_db_dir_key_file", lambda: unwritable)
    monkeypatch.setattr(S, "_write_key", lambda p, m: False)
    monkeypatch.setattr(S, "_keyring_module", lambda: None)
    S.reset_cache()
    with caplog.at_level("INFO", logger="agent2"):
        S.backend()
    assert any("secrets.backend" in r.getMessage() for r in caplog.records)


def test_a_wrong_master_key_cannot_forge_a_value(tmp_path, monkeypatch):
    """Authenticated encryption, not obfuscation: a tampered blob decodes to
    nothing rather than to garbage that gets sent to a vendor as a key."""
    ref = S.seal("original-secret")
    monkeypatch.setenv("AGENT2_SECRET_MASTER_KEY", "a-completely-different-key")
    S.reset_cache()
    assert S.resolve(ref) == ""


def test_tampering_with_the_ciphertext_is_detected():
    ref = S.seal("integrity-matters-here")
    body = ref.split(":", 3)[3]
    flipped = ("A" if body[10] != "A" else "B")
    tampered = f"a2s:enc:a1:{body[:10]}{flipped}{body[11:]}"
    assert S.resolve(tampered) == ""


# ══════════════════════════════════════════════════════════════════════════════
# Backends
# ══════════════════════════════════════════════════════════════════════════════

def test_the_stdlib_cipher_round_trips_when_cryptography_is_absent(monkeypatch):
    """The fallback has to be a real construction, not a stub — a build without
    `cryptography` must still protect the DB."""
    monkeypatch.setattr(S, "_aesgcm", lambda: None)
    ref = S.seal("stdlib-path-secret")
    assert ref.startswith(f"{S.PREFIX}{S.SCHEME_BLAKE}:")
    assert S.resolve(ref) == "stdlib-path-secret"


def test_the_stdlib_cipher_also_authenticates(monkeypatch):
    monkeypatch.setattr(S, "_aesgcm", lambda: None)
    ref = S.seal("stdlib-integrity")
    body = ref.split(":", 3)[3]
    tampered = f"a2s:enc:b1:{body[:-4]}AAAA"
    assert S.resolve(tampered) == ""


def test_both_schemes_stay_readable_regardless_of_which_wrote(monkeypatch):
    """⚠️ `resolve()` dispatches on the ref's OWN scheme tag, never on the current
    backend. Otherwise installing (or losing) `cryptography` would orphan every
    credential written before the change."""
    aes_ref = S.seal("written-with-aes")
    monkeypatch.setattr(S, "_aesgcm", lambda: None)
    blake_ref = S.seal("written-with-blake")
    assert S.resolve(blake_ref) == "written-with-blake"
    monkeypatch.undo()
    assert S.resolve(aes_ref) == "written-with-aes"
    assert S.resolve(blake_ref) == "written-with-blake"


def test_an_unusable_keyring_is_not_selected(monkeypatch):
    """⚠️ Importability is not availability.

    On a headless Linux box `keyring` imports fine and its backend raises on the
    first `set_password` — i.e. exactly when the user adds their API key. The probe
    moves that failure to selection time, where the answer is "use encryption".
    """
    class _Broken:
        @staticmethod
        def set_password(*a):
            raise RuntimeError("no D-Bus")

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Broken())
    S.reset_cache()
    assert S.backend() == S.BACKEND_ENCRYPTED


def test_a_keyring_that_forgets_is_not_selected(monkeypatch):
    """The nastier variant: the write is accepted and the read comes back empty.
    A store that accepts and forgets is how credentials vanish."""
    class _Amnesiac:
        @staticmethod
        def set_password(*a):
            return None

        @staticmethod
        def get_password(*a):
            return None

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Amnesiac())
    S.reset_cache()
    assert S.backend() == S.BACKEND_ENCRYPTED


def test_a_working_keyring_is_preferred_and_keeps_the_secret_out_of_the_ref(monkeypatch):
    store: dict = {}

    class _Good:
        @staticmethod
        def set_password(service, account, value):
            store[(service, account)] = value

        @staticmethod
        def get_password(service, account):
            return store.get((service, account))

        @staticmethod
        def delete_password(service, account):
            store.pop((service, account), None)

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Good())
    S.reset_cache()
    assert S.backend() == S.BACKEND_KEYRING

    ref = S.seal("os-stored-secret", namespace="api_keys", name="1")
    assert ref == "a2s:kr:api_keys/1"
    assert "os-stored-secret" not in ref
    assert S.resolve(ref) == "os-stored-secret"

    S.forget(ref)
    assert S.resolve(ref) == ""


def test_the_backend_can_be_forced_to_encrypted(monkeypatch):
    """An operator who does not want their Keychain touched needs a way to say so."""
    class _Good:
        @staticmethod
        def set_password(*a):
            return None

        @staticmethod
        def get_password(*a):
            return "ok"

    monkeypatch.setitem(__import__("sys").modules, "keyring", _Good())
    monkeypatch.setenv("AGENT2_SECRET_BACKEND", "encrypted")
    S.reset_cache()
    assert S.backend() == S.BACKEND_ENCRYPTED


def test_a_supplied_master_key_is_used_and_nothing_is_written_to_disk(monkeypatch, tmp_path):
    """The Docker / K8s secret path: the key arrives as an env var."""
    path = tmp_path / "should-not-appear.key"
    monkeypatch.setenv("AGENT2_SECRET_KEY_FILE", str(path))
    monkeypatch.setenv("AGENT2_SECRET_MASTER_KEY", "an-operator-supplied-passphrase")
    S.reset_cache()
    assert S.resolve(S.seal("env-keyed-secret")) == "env-keyed-secret"
    assert not path.exists()


def test_the_key_file_is_created_once_and_reused(tmp_path):
    """Two processes racing on first start must not each write a different key —
    the loser would be unable to decrypt the winner's ciphertext."""
    ref = S.seal("first-write")
    path = S.key_file()
    assert path.exists()
    first = path.read_bytes()

    S.reset_cache()
    assert S.resolve(ref) == "first-write"
    assert path.read_bytes() == first


def test_the_key_file_defaults_outside_the_database_directory(monkeypatch):
    """⚠️ The property that makes the ciphertext worth anything.

    A key file beside `agent2.db` travels with every copy of it — including the
    `/data` volume Docker mounts — so the default has to be the user's home.
    """
    monkeypatch.delenv("AGENT2_SECRET_KEY_FILE", raising=False)
    S.reset_cache()
    from pathlib import Path

    from agent2.config import DB
    assert S.key_file().parent != Path(DB).resolve().parent
    assert str(Path.home()) in str(S.key_file())


# ══════════════════════════════════════════════════════════════════════════════
# The three credential stores
# ══════════════════════════════════════════════════════════════════════════════

def test_a_gemini_key_is_stored_as_a_ref_and_read_back_usable():
    ok, label = db.add_api_key("AIzaSyGEMINI-TEST-KEY-000001")
    assert ok, label
    raw = db.qone("SELECT api_key FROM api_keys WHERE label=?", (label,))["api_key"]
    assert S.is_ref(raw), "the plaintext key is still in the row"
    assert "AIzaSyGEMINI-TEST-KEY-000001" not in raw

    listed = {r["label"]: r["api_key"] for r in db.list_api_keys()}
    assert listed[label] == "AIzaSyGEMINI-TEST-KEY-000001"


def test_the_rotator_still_loads_keys_it_can_use():
    """The consumer that matters. `KeyRotator.reload()` was deliberately not
    changed — `list_api_keys()` resolves, so the rotator needs no knowledge of
    references at all."""
    from agent2.llm.keys import rotator
    db.add_api_key("AIzaSyROTATOR-TEST-KEY-00002")
    rotator.reload()
    assert any(e["key"] == "AIzaSyROTATOR-TEST-KEY-00002" for e in rotator.entries)


def test_adding_the_same_gemini_key_twice_is_still_refused():
    """⚠️ Regression pin on the dedup rewrite.

    The old check was `WHERE api_key=?`, which cannot match once rows hold
    references (fresh nonce per seal). Left unfixed, a user adds one key five times
    and watches the rotator round-robin through five copies of a single quota.
    """
    ok1, _ = db.add_api_key("AIzaSyDUPLICATE-TEST-000003")
    ok2, why = db.add_api_key("AIzaSyDUPLICATE-TEST-000003")
    assert ok1 is True
    assert ok2 is False and why == "already_exists"


def test_removing_a_gemini_key_releases_the_material(monkeypatch):
    forgotten = []
    monkeypatch.setattr(S, "forget", lambda ref: forgotten.append(ref))
    ok, label = db.add_api_key("AIzaSyFORGET-TEST-KEY-000004")
    assert ok
    db.remove_api_key(label)
    assert forgotten and S.is_ref(forgotten[0])


def test_a_provider_key_is_stored_as_a_ref_and_sent_resolved():
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("t", "https://example.invalid", "sk-provider-000005",
                          "model-x", "openai")
    raw = db.qone("SELECT api_key FROM providers WHERE id=?",
                  (made["id"],))["api_key"]
    assert S.is_ref(raw)
    assert P.get_provider(made["id"])["api_key"] == "sk-provider-000005"


def test_list_providers_masks_with_a_constant_and_never_shows_the_ref():
    """⚠️ Two failures at once, both invisible without this.

    `k[:6]…k[-4:]` printed most of a short credential (the trap
    `state.mask_secret` was written for) and, once rows hold refs, would have shown
    six characters of ciphertext — useless to the user and confusing to anyone
    comparing it with what they pasted.
    """
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("t", "https://example.invalid", "sk-provider-000006",
                          "model-x", "openai")
    row = next(r for r in P.list_providers(safe=True) if r["id"] == made["id"])
    assert "sk-provider" not in row["api_key"]
    assert not S.is_ref(row["api_key"])
    assert row["api_key"] == "••••••••"
    assert row["key_set"] is True


def test_updating_a_provider_with_the_echoed_mask_does_not_wipe_the_key():
    """⚠️ The UI shows bullets and submits the form unchanged when only the base
    URL was edited. Treating that echo as a new key overwrites a working
    credential with `••••••••` — the same guard `state.set_config` carries."""
    from agent2.llm import providers as P
    P.init_providers_table()
    made = P.add_provider("t", "https://example.invalid", "sk-provider-000007",
                          "model-x", "openai")
    P.update_provider(made["id"], base_url="https://moved.invalid",
                      api_key="••••••••")
    got = P.get_provider(made["id"])
    assert got["api_key"] == "sk-provider-000007"
    assert got["base_url"] == "https://moved.invalid"


def test_the_zap_key_is_stored_as_a_ref_and_resolved_only_by_the_transport():
    from agent2.integrations import state as st
    from agent2.integrations.zap_mcp import zap
    zap.set_key("zap-key-000008")
    raw = db.qone("SELECT security_key FROM mcp_config WHERE server='zap'")
    assert S.is_ref(str(raw["security_key"]))
    assert zap.key == "zap-key-000008"
    assert zap.auth_headers()["Authorization"] == "zap-key-000008"
    # …and the surface payload still carries neither the key nor the ref.
    shown = json.dumps(st.config_for("zap"))
    assert "zap-key-000008" not in shown and "a2s:" not in shown


def test_clearing_the_zap_key_still_means_no_auth_header():
    from agent2.integrations.zap_mcp import zap
    zap.set_key("zap-key-000009")
    zap.set_key("")
    assert zap.auth_headers() == {}


def test_a_port_only_edit_does_not_disturb_the_stored_key():
    """Task 12's guarantee, re-pinned because sealing added a second value that an
    edit could plausibly clobber."""
    from agent2.integrations import state as st
    from agent2.integrations.zap_mcp import zap
    zap.set_key("zap-key-000010")
    st.set_config("zap", url="http://127.0.0.1:9999")
    assert zap.key == "zap-key-000010"


# ══════════════════════════════════════════════════════════════════════════════
# Migration 14
# ══════════════════════════════════════════════════════════════════════════════

def test_migration_14_seals_existing_plaintext_and_loses_nothing():
    from agent2.llm import providers as P

    P.init_providers_table()
    # Plant plaintext the way a pre-Task-16 install has it, bypassing the sealing
    # writers on purpose — that is exactly the state the migration must repair.
    db.exe("INSERT INTO api_keys(label, api_key, name, active) VALUES('99',?,?,1)",
           ("AIzaSyLEGACY-PLAINTEXT-0011", "legacy"))
    db.exe("INSERT INTO providers(id, name, base_url, api_key, model_id, format) "
           "VALUES('leg1','n','https://x.invalid',?,'m','openai')",
           ("sk-legacy-plaintext-0012",))
    db.exe("INSERT OR REPLACE INTO mcp_config(server, url, security_key) "
           "VALUES('zap','http://127.0.0.1:8282',?)", ("legacy-zap-0013",))

    with db._checkout() as (c, _owned):
        db._seal_existing_secrets(c)
        c.commit()

    # Nothing readable…
    for table, col in (("api_keys", "api_key"), ("providers", "api_key"),
                       ("mcp_config", "security_key")):
        blob = json.dumps([dict(r) for r in
                           db.qall(f"SELECT {col} FROM {table}")])  # noqa: S608
        assert "PLAINTEXT" not in blob and "legacy-zap" not in blob \
            and "sk-legacy" not in blob, table

    # …and nothing lost.
    assert {r["label"]: r["api_key"] for r in db.list_api_keys()}["99"] \
        == "AIzaSyLEGACY-PLAINTEXT-0011"
    assert P.get_provider("leg1")["api_key"] == "sk-legacy-plaintext-0012"
    from agent2.integrations import state as st
    st.invalidate()
    assert st.get_key("zap") == "legacy-zap-0013"


def test_migration_14_is_idempotent():
    db.exe("INSERT INTO api_keys(label, api_key, name, active) VALUES('98',?,?,1)",
           ("AIzaSyIDEMPOTENT-0014", "x"))
    for _ in range(3):
        with db._checkout() as (c, _owned):
            db._seal_existing_secrets(c)
            c.commit()
    assert {r["label"]: r["api_key"] for r in db.list_api_keys()}["98"] \
        == "AIzaSyIDEMPOTENT-0014"


def test_migration_14_leaves_a_row_alone_when_sealing_cannot_verify(monkeypatch):
    """⚠️ "Still readable in the DB" is a bad outcome; "the user's only key is
    gone" is unrecoverable. The migration can only ever produce the first."""
    db.exe("INSERT INTO api_keys(label, api_key, name, active) VALUES('97',?,?,1)",
           ("AIzaSyUNSEALABLE-0015", "x"))
    monkeypatch.setattr(S, "seal", lambda v, **kw: v)   # backend refuses
    with db._checkout() as (c, _owned):
        db._seal_existing_secrets(c)
        c.commit()
    raw = db.qone("SELECT api_key FROM api_keys WHERE label='97'")["api_key"]
    assert raw == "AIzaSyUNSEALABLE-0015"
    assert {r["label"]: r["api_key"] for r in db.list_api_keys()}["97"] \
        == "AIzaSyUNSEALABLE-0015"


def test_migration_14_does_not_raise_when_providers_does_not_exist():
    """`providers` is created by llm/providers.py, not by init_db() — see
    migration 5's note. A migration that raised on that breaks every first run."""
    db.exe("DROP TABLE IF EXISTS providers")
    with db._checkout() as (c, _owned):
        db._seal_existing_secrets(c)      # must not raise
        c.commit()


def test_migration_14_is_registered_and_bumps_the_schema_version():
    versions = {v for v, _, _ in db._MIGRATIONS}
    assert 14 in versions
    assert db.SCHEMA_VERSION >= 14
    name = next(n for v, n, _ in db._MIGRATIONS if v == 14)
    assert "secret" in name


# ══════════════════════════════════════════════════════════════════════════════
# Reporting
# ══════════════════════════════════════════════════════════════════════════════

def test_describe_states_the_limits_of_the_protection():
    """⚠️ A field named `encrypted: true` invites the reader to assume more than is
    true, so the payload says what it does and does not defend against."""
    snap = S.describe()
    json.dumps(snap)
    assert snap["backend"] in (S.BACKEND_KEYRING, S.BACKEND_ENCRYPTED,
                              S.BACKEND_PLAINTEXT)
    assert snap["protects_against"]
    if snap["backend"] == S.BACKEND_ENCRYPTED:
        assert "already read this user's files" in snap["not_protected_against"]


def test_describe_never_carries_a_secret_or_a_ref():
    S.seal("describe-must-not-show-this")
    blob = json.dumps(S.describe())
    assert "describe-must-not-show-this" not in blob
    assert "a2s:" not in blob


def test_mask_reveals_nothing_including_the_length():
    assert S.mask("a") == S.mask("a" * 200) == "••••••••"
    assert S.mask("") == ""
    assert S.mask(None) == ""


def test_the_ref_prefix_cannot_collide_with_a_real_credential():
    """`is_ref` must never misread a credential as a reference, or `resolve()` would
    mangle a key the user pasted."""
    for real in ("AIzaSyABC123", "sk-ant-api03-xyz", "sk-proj-abc",
                 "ghp_abcdefg", "xoxb-1-2-3", "eyJhbGciOiJI"):
        assert not S.is_ref(real), real
