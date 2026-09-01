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
