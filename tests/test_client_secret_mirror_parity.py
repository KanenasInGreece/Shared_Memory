"""R6 (fix round 1, Opus review, probe-confirmed) — the client's standalone
mirror of secure_env.is_secret_key() had drifted from what it mirrors:
_CLIENT_KNOWN_SECRET_NAMES was missing PG_CONN (a full DSN embeds the
Postgres password verbatim) and _CLIENT_SECRET_SUFFIXES was missing
_SECRET/_KEY/_CREDENTIAL(S) — both added to secure_env.py in ITS own review
round and never brought over. Probe-confirmed live: PG_CONN and two
suffix-matched provider-key names all landed in the client's own os.environ
from a scratch shared-memory/.env, with the process environment pre-cleared
— exactly the class of leak S-18/A2 finding 7 closed, reopened by drift.

This file pins the two predicates against each other over a fixed name
corpus so the NEXT drift fails a test instead of needing a fresh probe to
find. It is a contract test between two files in different "surfaces"
(Group 4's secure_env.py is server-only; Group 1's memory_bridge.py is the
client) — deliberately checking the numbers agree without importing one
from the other, because the client must never depend on a server-only
module (that constraint is exactly why the mirror is a duplicate in the
first place, not an import).

MCPW-R2-C5 (origin fact:1816): the MCP door (mcp/vector-skill.py) ported the
identical mirror — its own duplicate, not an import of memory_bridge's,
since the two clients ship alone (Group 1). This file's job doubles
accordingly: pin vector-skill's copies against secure_env exactly as it
already pins memory_bridge's, so a drift in EITHER client's mirror fails
here first. `vector-skill.py` has a dash in its filename, so it cannot be
imported with a plain `import` statement — loaded with the same
importlib-by-path idiom `tests/test_vector_skill.py` uses
(`load_vector_skill()`). That import executes the module's env loader as a
side effect; with no VECTOR_SKILL_ENV set and no mcp/.env on disk in this
checkout, the loader finds nothing to load, so the comparisons below stay
pure (Delta D3) — never mutate the real process env without monkeypatch."""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import memory_bridge  # noqa: E402
import secure_env  # noqa: E402


def _load_vector_skill():
    """Same importlib-by-path idiom as tests/test_vector_skill.py's
    load_vector_skill() — vector-skill.py's dash makes it unimportable by
    name. A fresh module object each call, isolated from test_vector_skill.py's
    own sys.modules["vector_skill"] entry."""
    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill_mirror_parity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vector_skill = _load_vector_skill()

# The corpus: every name a real deployment's shared-memory/.env can hold,
# per .env.example — secret and non-secret, drawn from the same file both
# probes (Opus's and this fix round's) used.
_SECRET_COINCIDING_NAMES = (
    "PG_PASSWORD", "NEO4J_PASSWORD", "TAVILY_API_KEY", "AGENT_TOKENS",
    "BACKUP_ADMIN_TOKEN", "PG_CONN", "DEEPSEEK_API_KEY", "XAI_API_KEY",
    "DEEPSEEK_SECRET", "OPENROUTER_CREDENTIAL", "SOME_VENDOR_CREDENTIALS",
    "SOME_NEW_TOKEN", "openrouter_key",
)
_NON_SECRET_NAMES = (
    "EMBEDDER_URL", "LLM_BACKENDS", "LLM_BACKENDS_JSON", "NEO4J_MAX_POOL",
    "AUDIT_LOG_PATH", "MEMORY_LOG_PATH", "MEMORY_LOG_LEVEL", "GATEWAY_URL",
)


def test_client_predicate_classifies_every_known_secret_name_as_secret():
    for name in _SECRET_COINCIDING_NAMES:
        assert secure_env.is_secret_key(name), f"{name} not classified secret server-side (test corpus bug)"
        assert memory_bridge._is_client_secret_key(name), (
            f"{name} is server-classified secret but the CLIENT mirror missed it — "
            f"R6 drift has recurred"
        )


def test_client_predicate_does_not_misclassify_ordinary_config():
    for name in _NON_SECRET_NAMES:
        assert not secure_env.is_secret_key(name), f"{name} unexpectedly secret server-side (test corpus bug)"
        assert not memory_bridge._is_client_secret_key(name), (
            f"{name} is ordinary config but the client predicate now treats it as "
            f"secret — over-classification is harmless (the value is just skipped, "
            f"per the client's own comment) but signals the two predicates have "
            f"drifted apart in an unexpected direction"
        )


def test_pg_conn_specifically_is_classified_secret_by_the_client():
    """The exact R6 casualty: a full DSN embeds the Postgres password."""
    assert memory_bridge._is_client_secret_key("PG_CONN")


def test_client_known_secret_names_is_a_subset_of_the_server_list():
    """AGENT_TOKEN is the one deliberate asymmetry — it is on the SERVER's
    KNOWN_SECRET_NAMES (R4, fix round 1) but never on the client's, because
    the client routes it through its own dedicated _AGENT_TOKEN_FROM_FILE
    path instead of this predicate at all (see _is_client_secret_key's own
    early-return). Every other name on the client's list must also appear
    on the server's — a name here that ISN'T secret server-side would be a
    different kind of drift (over-restrictive, not under)."""
    extra = memory_bridge._CLIENT_KNOWN_SECRET_NAMES - secure_env.KNOWN_SECRET_NAMES
    assert extra == set(), f"client claims these are secret but the server does not: {extra}"


def test_client_secret_suffixes_is_a_subset_of_the_server_suffixes():
    extra = set(memory_bridge._CLIENT_SECRET_SUFFIXES) - set(secure_env._SECRET_SUFFIXES)
    assert extra == set(), f"client has suffixes the server does not: {extra}"


def test_client_secret_suffixes_is_not_missing_any_server_suffix():
    """The direction that actually matters — R6 was a MISSING suffix set,
    not an extra one. A future secure_env.py suffix widening must be
    brought over here too, or this test fails first."""
    missing = set(secure_env._SECRET_SUFFIXES) - set(memory_bridge._CLIENT_SECRET_SUFFIXES)
    assert missing == set(), f"server has suffixes the client mirror is missing: {missing}"


def test_client_known_secret_names_is_not_missing_any_server_name_except_agent_token():
    missing = secure_env.KNOWN_SECRET_NAMES - memory_bridge._CLIENT_KNOWN_SECRET_NAMES
    assert missing == {"AGENT_TOKEN"}, (
        f"server KNOWN_SECRET_NAMES has entries the client mirror is missing "
        f"(besides the deliberate AGENT_TOKEN exception): {missing - {'AGENT_TOKEN'}}"
    )


# ── MCPW-R2-C5: the MCP door's ported mirror, pinned the same way ──────────

def test_vector_skill_predicate_classifies_every_known_secret_name_as_secret():
    for name in _SECRET_COINCIDING_NAMES:
        assert secure_env.is_secret_key(name), f"{name} not classified secret server-side (test corpus bug)"
        assert vector_skill._is_client_secret_key(name), (
            f"{name} is server-classified secret but the MCP client mirror missed it — "
            f"R6 drift has recurred"
        )


def test_vector_skill_predicate_does_not_misclassify_ordinary_config():
    for name in _NON_SECRET_NAMES:
        assert not secure_env.is_secret_key(name), f"{name} unexpectedly secret server-side (test corpus bug)"
        assert not vector_skill._is_client_secret_key(name), (
            f"{name} is ordinary config but the MCP client predicate now treats it as "
            f"secret — over-classification is harmless (the value is just skipped) but "
            f"signals the two predicates have drifted apart in an unexpected direction"
        )


def test_pg_conn_specifically_is_classified_secret_by_the_vector_skill_client():
    """The exact R6 casualty: a full DSN embeds the Postgres password."""
    assert vector_skill._is_client_secret_key("PG_CONN")


def test_vector_skill_known_secret_names_is_a_subset_of_the_server_list():
    """AGENT_TOKEN is the one deliberate asymmetry — it is on the SERVER's
    KNOWN_SECRET_NAMES but never on either client's, because both clients
    route it through their own dedicated _AGENT_TOKEN_FROM_FILE path instead
    of this predicate at all."""
    extra = vector_skill._CLIENT_KNOWN_SECRET_NAMES - secure_env.KNOWN_SECRET_NAMES
    assert extra == set(), f"MCP client claims these are secret but the server does not: {extra}"


def test_vector_skill_secret_suffixes_is_a_subset_of_the_server_suffixes():
    extra = set(vector_skill._CLIENT_SECRET_SUFFIXES) - set(secure_env._SECRET_SUFFIXES)
    assert extra == set(), f"MCP client has suffixes the server does not: {extra}"


def test_vector_skill_secret_suffixes_is_not_missing_any_server_suffix():
    """The direction that actually matters — R6 was a MISSING suffix set,
    not an extra one. A future secure_env.py suffix widening must be
    brought over to BOTH client mirrors, or this test fails first."""
    missing = set(secure_env._SECRET_SUFFIXES) - set(vector_skill._CLIENT_SECRET_SUFFIXES)
    assert missing == set(), f"server has suffixes the MCP client mirror is missing: {missing}"


def test_vector_skill_known_secret_names_is_not_missing_any_server_name_except_agent_token():
    missing = secure_env.KNOWN_SECRET_NAMES - vector_skill._CLIENT_KNOWN_SECRET_NAMES
    assert missing == {"AGENT_TOKEN"}, (
        f"server KNOWN_SECRET_NAMES has entries the MCP client mirror is missing "
        f"(besides the deliberate AGENT_TOKEN exception): {missing - {'AGENT_TOKEN'}}"
    )


def test_vector_skill_mirror_agrees_with_memory_bridge_mirror():
    """Group 1 parity, one level up: the two CLIENT mirrors — duplicated from
    each other by construction, never imported — must themselves agree, or
    the two front doors classify the same key differently."""
    assert vector_skill._CLIENT_KNOWN_SECRET_NAMES == memory_bridge._CLIENT_KNOWN_SECRET_NAMES
    assert set(vector_skill._CLIENT_SECRET_SUFFIXES) == set(memory_bridge._CLIENT_SECRET_SUFFIXES)


# ── MCPW-R2-C5 SEC round (F3): filter-bypass fix at the CALL SITES ─────────
# SEC build review HIGH-1 (case sensitivity) + HIGH-2 (BOM) (2026-09-01,
# gemini): _is_client_secret_key is an exact-match-then-suffix predicate, so
# `pg_conn` (lowercase) or a BOM-prefixed `PG_CONN`/`AGENT_TOKEN` sailed past
# both the AGENT_TOKEN divert and the secret check and landed straight in
# os.environ. Fixed at the two LOADER CALL SITES in mcp/vector-skill.py
# (both the dotenv_values() loop and _load_env_manually), never inside
# _is_client_secret_key itself — it stays a byte-identical mirror of
# memory_bridge's, pinned above; the ruling was explicit that mirror parity
# must not drift. These pins therefore run the fix THROUGH THE LOADER, not
# against the bare predicate (which cannot see case/BOM handling that lives
# one level up the call stack) — the SEC reviewer's own finding 6 was that
# predicate-only pins can pass while the loader still leaks.
#
# The BOM-prefixed keys are placed AFTER a first, neutral line: dotenv_values
# (and Python's `utf-8-sig` codec, used by _load_env_manually) only strip a
# BOM that sits at byte offset 0 of the FILE — i.e. only the very first key
# would be auto-cleaned "for free". Placing the BOM-carrying keys later in
# the file (measured, both loaders confirmed to still see the raw
# character there) is what actually exercises the call-site .lstrip("﻿")
# fix rather than accidentally relying on a library that already handles the
# leading-byte case.
_BOM = "\ufeff"


def _write_bypass_fixture(path):
    path.write_text(
        "PROBE_FIRST_LINE=neutral\n"
        f"{_BOM}AGENT_TOKEN=tok_bom_prefixed\n"
        "pg_conn=postgresql://leaked_lowercase\n"
        f"{_BOM}PG_CONN=postgresql://leaked_bom\n"
        # H-1 (2026-09-01 follow-up, blocking regression in 5d40292): a
        # lowercase agent_token= line. This one is deliberately placed
        # AFTER the BOM-prefixed AGENT_TOKEN above, so first-definition-
        # wins means it does NOT itself become _AGENT_TOKEN_FROM_FILE here
        # — this fixture only proves it is never exported under any
        # casing. Diversion-when-first is pinned separately below in a
        # dedicated minimal fixture (this shared one already carries a
        # higher-precedence token line, so it cannot deterministically
        # prove that half here).
        "agent_token=tok_lower_variant\n"
        "probe_lower=benign_val\n"
    )


_BYPASS_ENV_KEYS = ("PROBE_FIRST_LINE", "AGENT_TOKEN", f"{_BOM}AGENT_TOKEN",
                    "agent_token", "pg_conn", "PG_CONN", f"{_BOM}PG_CONN",
                    "probe_lower")


def test_manual_parser_filters_lowercase_and_bom_prefixed_secrets(tmp_path, monkeypatch):
    """Through _load_env_manually (the no-dotenv fallback path) — the second
    of the two call sites the ruling requires fixed. Mutation check: removing
    the `.upper()` from this loop's `_is_client_secret_key(key_norm.upper())`
    call makes the lowercase `pg_conn` pin below die (recorded in HANDOFF).

    H-1 follow-up: also pins the lowercase `agent_token=` line is never
    exported under either casing — this fixture's own precedence rules
    mean it is NOT the one diverted (see _write_bypass_fixture's comment);
    diversion-when-first is a separate, dedicated test below."""
    for key in _BYPASS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "bypass.env"
    _write_bypass_fixture(env_file)
    monkeypatch.setattr(vector_skill, "_AGENT_TOKEN_FROM_FILE", "")
    try:
        vector_skill._load_env_manually(str(env_file))
        assert "pg_conn" not in os.environ, "lowercase pg_conn leaked past the filter"
        assert "PG_CONN" not in os.environ, "BOM-prefixed PG_CONN leaked under its bare name"
        assert f"{_BOM}PG_CONN" not in os.environ, "BOM-prefixed PG_CONN leaked under its raw key"
        assert vector_skill._AGENT_TOKEN_FROM_FILE == "tok_bom_prefixed", (
            "the BOM-prefixed AGENT_TOKEN was not diverted to the private variable"
        )
        assert "AGENT_TOKEN" not in os.environ
        assert f"{_BOM}AGENT_TOKEN" not in os.environ
        assert "agent_token" not in os.environ, (
            "H-1: a lowercase agent_token= line leaked the bearer into os.environ"
        )
        assert os.environ.get("probe_lower") == "benign_val", (
            "a benign lowercase key must still export under its ORIGINAL name — "
            "normalization is for filtering only, never for the exported name"
        )
    finally:
        # try/finally with a DIRECT os.environ.pop, not monkeypatch.delenv:
        # the loader writes os.environ DIRECTLY, never via monkeypatch, and
        # monkeypatch.delenv on a key set that way only restores it again
        # at THIS test's own fixture teardown (measured — see
        # tests/test_vector_skill.py's twin fix and its detailed note) —
        # which would silently re-leak it into the next test to run.
        for key in _BYPASS_ENV_KEYS:
            os.environ.pop(key, None)


def test_dotenv_values_path_filters_lowercase_and_bom_prefixed_secrets(tmp_path, monkeypatch):
    """Through the dotenv_values() primary path — the branch actually taken
    at real import time in THIS suite, since python-dotenv is transitively
    installed via fastmcp (QA build review MED-1, 2026-09-01). Mutation
    check: removing the `.upper()` from this loop's
    `_is_client_secret_key(_k_norm.upper())` call makes the lowercase
    `pg_conn` pin below die.

    H-1 follow-up: also pins the lowercase `agent_token=` line is never
    exported under either casing (diversion-when-first is separate, below)."""
    for key in _BYPASS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "bypass.env"
    _write_bypass_fixture(env_file)
    monkeypatch.setenv("VECTOR_SKILL_ENV", str(env_file))
    try:
        fresh = _load_vector_skill()
        assert "pg_conn" not in os.environ, "lowercase pg_conn leaked past the filter"
        assert "PG_CONN" not in os.environ, "BOM-prefixed PG_CONN leaked under its bare name"
        assert f"{_BOM}PG_CONN" not in os.environ, "BOM-prefixed PG_CONN leaked under its raw key"
        assert fresh._AGENT_TOKEN_FROM_FILE == "tok_bom_prefixed", (
            "the BOM-prefixed AGENT_TOKEN was not diverted to the private variable"
        )
        assert "AGENT_TOKEN" not in os.environ
        assert f"{_BOM}AGENT_TOKEN" not in os.environ
        assert "agent_token" not in os.environ, (
            "H-1: a lowercase agent_token= line leaked the bearer into os.environ"
        )
        assert os.environ.get("probe_lower") == "benign_val", (
            "a benign lowercase key must still export under its ORIGINAL name — "
            "normalization is for filtering only, never for the exported name"
        )
    finally:
        # Direct os.environ.pop, not monkeypatch.delenv — see the sibling
        # test above (same reason, same measured behaviour).
        for key in _BYPASS_ENV_KEYS:
            os.environ.pop(key, None)


# ── H-1 (2026-09-01 follow-up, blocking): diversion-when-first, minimal ────
# fixtures. The shared bypass fixture above already carries a HIGHER-
# precedence token (the BOM-prefixed AGENT_TOKEN, first in the file), so it
# cannot deterministically prove a lowercase agent_token= line diverts —
# first-definition-wins means the earlier line always claims
# _AGENT_TOKEN_FROM_FILE first. These two tests use a fixture with ONLY a
# lowercase token line (plus one benign key) so the diversion assertion is
# unambiguous.

_LOWERCASE_TOKEN_ONLY_KEYS = ("agent_token", "AGENT_TOKEN", "probe_lower")


def _write_lowercase_token_only_fixture(path):
    path.write_text(
        "agent_token=tok_lower_variant\n"
        "probe_lower=benign_val\n"
    )


def test_manual_parser_diverts_lowercase_agent_token_when_first(tmp_path, monkeypatch):
    """H-1: a lowercase agent_token= line, as the ONLY token line in the
    file, must be diverted to _AGENT_TOKEN_FROM_FILE — not exported under
    any casing. Mutation check: reverting to the pre-fix shape (`.upper()`
    only at the two predicate calls, key_norm/​_k_norm left case-sensitive)
    makes this assertion die (recorded in HANDOFF)."""
    for key in _LOWERCASE_TOKEN_ONLY_KEYS:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "lower_token.env"
    _write_lowercase_token_only_fixture(env_file)
    monkeypatch.setattr(vector_skill, "_AGENT_TOKEN_FROM_FILE", "")
    try:
        vector_skill._load_env_manually(str(env_file))
        assert vector_skill._AGENT_TOKEN_FROM_FILE == "tok_lower_variant", (
            "H-1: the lowercase agent_token= line was not diverted when it "
            "was the only token line present"
        )
        assert "agent_token" not in os.environ
        assert "AGENT_TOKEN" not in os.environ
        assert os.environ.get("probe_lower") == "benign_val"
    finally:
        for key in _LOWERCASE_TOKEN_ONLY_KEYS:
            os.environ.pop(key, None)


def test_dotenv_values_path_diverts_lowercase_agent_token_when_first(tmp_path, monkeypatch):
    """H-1, dotenv_values() twin of the test above — same fixture, same
    assertions, through the branch actually taken at real import time in
    this suite (python-dotenv transitively installed via fastmcp)."""
    for key in _LOWERCASE_TOKEN_ONLY_KEYS:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "lower_token.env"
    _write_lowercase_token_only_fixture(env_file)
    monkeypatch.setenv("VECTOR_SKILL_ENV", str(env_file))
    try:
        fresh = _load_vector_skill()
        assert fresh._AGENT_TOKEN_FROM_FILE == "tok_lower_variant", (
            "H-1: the lowercase agent_token= line was not diverted when it "
            "was the only token line present"
        )
        assert "agent_token" not in os.environ
        assert "AGENT_TOKEN" not in os.environ
        assert os.environ.get("probe_lower") == "benign_val"
    finally:
        for key in _LOWERCASE_TOKEN_ONLY_KEYS:
            os.environ.pop(key, None)


# ── D.3 (SEC round, ADV1-1): the CLI door's own call-site normalisation ────
# memory_bridge.py never had vector-skill.py's case/BOM fix at its two
# loader call sites (the dotenv_values() branch and the manual-parse
# fallback) until this round — these pins run the fix THROUGH THE LOADER,
# through a fresh module import from a controlled tmp skill tree (the same
# helper test_memory_bridge.py's own S-18 tests use), never against the bare
# predicate alone (predicate-only pins cannot see loader-level case/BOM
# handling).

def _load_memory_bridge_from_skill_dir(skill_dir):
    """Same fresh-import-from-tmp-tree idiom as test_memory_bridge.py's own
    _load_memory_bridge_from() -- duplicated rather than imported, since
    that helper lives in a sibling test module and this file already avoids
    importing test_vector_skill.py's loader for the same reason."""
    import importlib.util
    import shutil
    import uuid
    scripts_dir = os.path.join(skill_dir, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    src = os.path.join(
        os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory",
        "scripts", "memory_bridge.py",
    )
    dest = os.path.join(scripts_dir, "memory_bridge.py")
    shutil.copy(src, dest)
    mod_name = f"memory_bridge_bypass_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(mod_name, dest)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_memory_bridge_dotenv_path_filters_lowercase_and_bom_prefixed_secrets(tmp_path, monkeypatch):
    """Through the dotenv_values() primary path -- the branch actually taken
    at real import time (python-dotenv is transitively installed via
    fastmcp). Mutation check: removing the key_norm computation and reverting
    to the two separate `.upper()` calls the fix replaced makes the
    lowercase `pg_conn` / BOM-prefixed pins below die (recorded in HANDOFF)."""
    for key in _BYPASS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    _pin = os.environ.pop("SECURE_ENV_FILE", None)
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    _write_bypass_fixture(skill_dir / ".env")
    try:
        mod = _load_memory_bridge_from_skill_dir(str(skill_dir))
        assert "pg_conn" not in os.environ, "lowercase pg_conn leaked past the filter"
        assert "PG_CONN" not in os.environ, "BOM-prefixed PG_CONN leaked under its bare name"
        assert f"{_BOM}PG_CONN" not in os.environ, "BOM-prefixed PG_CONN leaked under its raw key"
        assert mod._AGENT_TOKEN_FROM_FILE == "tok_bom_prefixed", (
            "the BOM-prefixed AGENT_TOKEN was not diverted to the private variable"
        )
        assert "AGENT_TOKEN" not in os.environ
        assert f"{_BOM}AGENT_TOKEN" not in os.environ
        assert "agent_token" not in os.environ, (
            "a lowercase agent_token= line leaked the bearer into os.environ"
        )
        assert os.environ.get("probe_lower") == "benign_val", (
            "a benign lowercase key must still export under its ORIGINAL name — "
            "normalization is for filtering only, never for the exported name"
        )
    finally:
        for key in _BYPASS_ENV_KEYS:
            os.environ.pop(key, None)
        if _pin is not None:
            os.environ["SECURE_ENV_FILE"] = _pin


def test_memory_bridge_dotenv_path_diverts_lowercase_agent_token_when_first(tmp_path, monkeypatch):
    """H-1 twin: a lowercase agent_token= line, as the ONLY token line in
    the file, must be diverted -- not exported under any casing."""
    for key in _LOWERCASE_TOKEN_ONLY_KEYS:
        monkeypatch.delenv(key, raising=False)
    _pin = os.environ.pop("SECURE_ENV_FILE", None)
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    _write_lowercase_token_only_fixture(skill_dir / ".env")
    try:
        mod = _load_memory_bridge_from_skill_dir(str(skill_dir))
        assert mod._AGENT_TOKEN_FROM_FILE == "tok_lower_variant"
        assert "agent_token" not in os.environ
        assert "AGENT_TOKEN" not in os.environ
        assert os.environ.get("probe_lower") == "benign_val"
    finally:
        for key in _LOWERCASE_TOKEN_ONLY_KEYS:
            os.environ.pop(key, None)
        if _pin is not None:
            os.environ["SECURE_ENV_FILE"] = _pin


def test_both_tracked_memory_bridge_copies_stay_byte_identical():
    """Group 1 standing rule: the framework copy and the shared-memory-skill
    tracked copy must never diverge — sync_skills.sh's whole job is copying
    one to the other. This is the automated version of the manual `diff`
    every A4 commit already ran by hand."""
    here = os.path.dirname(os.path.abspath(__file__))
    framework = os.path.join(here, "..", "shared-memory", "scripts", "memory_bridge.py")
    skill = os.path.join(here, "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")
    with open(framework) as f:
        framework_content = f.read()
    with open(skill) as f:
        skill_content = f.read()
    assert framework_content == skill_content, (
        "shared-memory/scripts/memory_bridge.py and "
        "shared-memory-skill/shared-memory/scripts/memory_bridge.py have diverged "
        "— run sync_skills.sh (or hand-apply the same edit to both, then diff)"
    )
