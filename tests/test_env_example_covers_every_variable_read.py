"""Every `os.environ` read in the framework's own scripts must be either
TEMPLATED (a `KEY=` / `# KEY=` line in the right `.env.example`) or explicitly
ALLOWLISTED by name with a falsifiable reason.

WHY AST, NEVER A LINE REGEX. `memory_bridge.py` reads `PROJECT_ROOT_MARKERS`
inside a multi-line `os.environ.get(` call (`:296-297`) — the exact variable
the v0.9.71 audit and the v1 re-audit both missed. A same-line regex matches
`os.environ.get("X"` only when the whole call sits on one line, so it goes
green while missing precisely the variable this test exists to catch. AST
parsing sees the call regardless of how the source wraps it.

WHY A FAIL-CLOSED GLOB, NEVER A HARD-CODED FILE LIST. A literal list of
filenames proves nothing about the file nobody remembered to add — that is
what happened to `apply.py` before this cycle (D5). `shared-memory/scripts/*.py`
+ `shared-memory/migrations/*.py`, every `.py` file in scope; `_EXCLUDE`
below is the one place a file leaves that scope, and it must name a reason.

WHY BOTH `memory_bridge.py` COPIES. `shared-memory/scripts/memory_bridge.py`
is the source of truth; `shared-memory-skill/shared-memory/scripts/memory_bridge.py`
is the tracked copy `sync_skills.sh` ships. They are byte-identical today —
nothing pins that, so this test parses both rather than assuming it.

WHY THE ALLOWLIST IS (name, reason) AND FAILS ON A STALE ENTRY. A name-only
allowlist accumulates silently and nobody can tell, years later, why an entry
is there or whether it still applies. Requiring a reason makes each entry
reviewable; failing when the name is no longer read at all means the list
cannot silently outlive the code it was written against.

⭐ THIS TEST HAS TWO TEMPLATES, NOT ONE. `memory_bridge.py` is CLIENT code —
its variables are checked against the CLIENT template
(`shared-memory-skill/shared-memory/.env.example`). Every other file in the
glob is FRAMEWORK code — checked against the FRAMEWORK template
(`shared-memory/.env.example`). Conflating them would let a client variable
"pass" because some unrelated framework knob happens to share a template file,
or vice versa.

⚠ MECHANICAL RESULT VS. THE W7 BRIEF (`Local_Documentation/ColdBriefs/
W7_RULED_BRIEF_2026-09-10.md`, D4/D5): the brief names 4 client variables (2
templated + 2 allowlisted) and 9 framework variables as the coverage gap.
Running this exact mechanism against the WHOLE glob (not just memory_bridge.py
and the eight D5 scripts) finds substantially more framework names absent from
the template than D5 enumerated — e.g. `CREDENTIALS_DIRECTORY`, `HOME`, `PATH`,
`MOCK_LLM`, `PG_CONN`, `REM_TEMPERATURE`/`NREM_TEMPERATURE`,
`SMEM_ONTOLOGY_PATH`, `WRITE_QUIESCE_SEC`, and others — see
`W7_SETS.md` and the build report for the full list and classification.
RULING: per the brief's own instruction ("if it does not balance, stop and
report — do not allowlist the remainder"), none of those extra names is
allowlisted here without an operator decision on each. **This test is
therefore RED after this PR's fix, on names outside the brief's stated
scope** — that is a finding for the operator, not a defect in the test.
"""
import ast
import glob
import os
import re

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

# Fail-closed input set. Nothing here is a hand-picked file list — every .py
# file under these two directories is in scope unless named in _EXCLUDE below,
# with a reason. Currently empty: every script and migration tool in the tree
# is genuinely in scope for this check.
_GLOB_PATTERNS = [
    os.path.join(REPO_ROOT, "shared-memory", "scripts", "*.py"),
    os.path.join(REPO_ROOT, "shared-memory", "migrations", "*.py"),
]
_EXCLUDE: dict[str, str] = {
    # "some_file.py": "reason this file is out of scope",
}

_CLIENT_TEMPLATE = os.path.join(
    REPO_ROOT, "shared-memory-skill", "shared-memory", ".env.example"
)
_FRAMEWORK_TEMPLATE = os.path.join(REPO_ROOT, "shared-memory", ".env.example")
_CLIENT_COPY = os.path.join(
    REPO_ROOT, "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py"
)

# (name, reason) — reason states why templating would be WRONG, not merely
# unnecessary (brief PART 3, T1).
_ALLOWLIST: list[tuple[str, str]] = [
    (
        "SECURE_ENV_FILE",
        "process-env-only bootstrap selector, read BEFORE any .env is loaded — "
        "it SELECTS which file loads, so a value written into the file it would "
        "select is inert (D4). Set it in the shell or an MCP env block, never "
        "in the file it selects.",
    ),
    (
        "AGENT_ID",
        "round 3 correction (C4): this is NOT a test/internal variable — "
        "mcp/system-prompt.md:158 documents it as legitimate, operator-facing "
        "config for the MCP client's own .env ('That client file holds only "
        "AGENT_TOKEN (optionally COORDINATOR_URL / AGENT_ID)'). It is "
        "excluded from THIS template (the CLI skill's .env.example) for a "
        "narrower, verified reason: with gateway auth on, coordinator.py:7717 "
        "resolves the save's agent_id as "
        "`request.get('authenticated_agent') or body.get('agent_id', ...)` — "
        "the verified TOKEN identity always wins over whatever AGENT_ID a "
        "client sent, so templating it here would teach an operator to set a "
        "name the server silently ignores whenever a token is presented. "
        "mcp/vector-skill.py reads the same variable for the MCP surface but "
        "lives outside this glob (mcp/, not shared-memory/scripts or "
        "shared-memory/migrations) and is documented separately, in prose, "
        "not via a .env.example this test checks.",
    ),
    (
        "XDG_RUNTIME_DIR",
        "standard OS/session variable (fixed by name in the brief's "
        "ALLOWLIST-SET) — the session sets it, never the operator via this "
        "framework's own config files.",
    ),
    (
        "HOME",
        "OS-supplied (round 2 ruling) — a shipped template must never set it; "
        "read by migrate_env.py purely to report the current environment, not "
        "as a configuration knob.",
    ),
    (
        "PATH",
        "OS-supplied (round 2 ruling) — same reasoning as HOME; read by "
        "migrate_env.py to report the current environment, never to be set by "
        "an operator via this file.",
    ),
    (
        "CREDENTIALS_DIRECTORY",
        "supplied by systemd LoadCredential= at runtime (round 2 ruling) — an "
        "operator-written value in this file would be overwritten by systemd "
        "or, on a non-systemd host, misleadingly imply a mechanism that is not "
        "active.",
    ),
    (
        "MOCK_LLM",
        "test-only switch (rem_loop.py:1115; round 2 ruling) — templating it "
        "in a production config invites an operator to set it and silently "
        "stub their LLM calls.",
    ),
    (
        "SM_PRE_UPDATE_VERSION",
        "internal parent-to-child handoff set by update_framework.sh itself "
        "between its own steps (round 2 ruling) — not operator configuration; "
        "an operator-set value would be overwritten by the very next update run.",
    ),
]
_ALLOWLIST_NAMES = {name for name, _ in _ALLOWLIST}

# PG_CONN is TEMPLATED (not allowlisted) but its code default is dynamic and
# CREDENTIAL-BEARING (coordinator.py:2388-2389 interpolates the live
# PG_PASSWORD into a DSN) — this public-mirror repo ships placeholder values
# only, so the default-value assertion cannot apply to it. Presence is still
# required and enforced; see test_this_prs_templated_keys_carry_the_codes_real_default's
# docstring and W7_SETS.md for why this one key is presence-checked only.
_PRESENCE_ONLY_KEYS = {"PG_CONN"}


def _iter_input_files():
    seen = set()
    for pattern in _GLOB_PATTERNS:
        for path in glob.glob(pattern):
            base = os.path.basename(path)
            if base in _EXCLUDE:
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path
    # memory_bridge.py's tracked skill copy is outside the glob's own reach —
    # T1 parses it too (brief PART 3, T1).
    if _CLIENT_COPY not in seen and os.path.isfile(_CLIENT_COPY):
        yield _CLIENT_COPY


def _is_os_environ_attr(node):
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _extract_names(path):
    """AST-walk one file for os.environ.get(...), os.getenv(...), os.environ[...]
    string-literal names. Never a line regex — see module docstring."""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr == "get" and _is_os_environ_attr(func.value):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                    node.args[0].value, str
                ):
                    names.add(node.args[0].value)
            elif (
                func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(
                    node.args[0].value, str
                ):
                    names.add(node.args[0].value)
        if isinstance(node, ast.Subscript) and _is_os_environ_attr(node.value):
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                names.add(sl.value)
    return names


def _template_covers(name, template_path):
    with open(template_path, encoding="utf-8") as f:
        text = f.read()
    return bool(re.search(r"^#?\s*" + re.escape(name) + r"=", text, re.MULTILINE))


def _default_line_present(name, default, template_path):
    """The DEFAULT, not just the name (brief PART 3, T1) — a name-presence-only
    check would pass `# PG_PORT=5433` against a code default of 5432 (fact:1309).
    """
    with open(template_path, encoding="utf-8") as f:
        text = f.read()
    pattern = r"^#\s*" + re.escape(name) + r"=" + re.escape(default) + r"\s*$"
    return bool(re.search(pattern, text, re.MULTILINE))


def _collect():
    """Returns (client_names, framework_names) -- the mechanical CATCH-SET,
    split by which template each file's variables are checked against."""
    client_names = set()
    framework_names = set()
    for path in _iter_input_files():
        names = _extract_names(path)
        if os.path.basename(path) == "memory_bridge.py":
            client_names |= names
        else:
            framework_names |= names
    return client_names, framework_names


def test_the_allowlist_has_no_stale_entry():
    """A name no longer read anywhere in the glob must be removed from the
    allowlist — otherwise the list only ever grows and nobody can tell which
    entries still matter (brief PART 3, T1)."""
    client_names, framework_names = _collect()
    all_names = client_names | framework_names
    stale = [name for name in _ALLOWLIST_NAMES if name not in all_names]
    assert not stale, (
        f"allowlist entries no longer read anywhere in the glob: {stale} — "
        "remove them from _ALLOWLIST"
    )


def test_env_example_covers_every_variable_read():
    client_names, framework_names = _collect()

    uncovered_client = sorted(
        n
        for n in client_names
        if n not in _ALLOWLIST_NAMES and not _template_covers(n, _CLIENT_TEMPLATE)
    )
    uncovered_framework = sorted(
        n
        for n in framework_names
        if n not in _ALLOWLIST_NAMES and not _template_covers(n, _FRAMEWORK_TEMPLATE)
    )

    assert not uncovered_client, (
        f"client variable(s) read by memory_bridge.py but absent from "
        f"{os.path.relpath(_CLIENT_TEMPLATE, REPO_ROOT)} and not allowlisted: "
        f"{uncovered_client}"
    )
    assert not uncovered_framework, (
        f"framework variable(s) read somewhere in shared-memory/scripts or "
        f"shared-memory/migrations but absent from "
        f"{os.path.relpath(_FRAMEWORK_TEMPLATE, REPO_ROOT)} and not allowlisted: "
        f"{uncovered_framework}"
    )


def test_this_prs_templated_keys_carry_the_codes_real_default():
    """D4 + D5's own variables, asserted by VALUE, not merely by name — a
    name-presence-only test is satisfied by a wrong default (fact:1309)."""
    client_defaults = {
        "PROJECT_ROOT_MARKERS": ".git,CLAUDE.md,AGENTS.md,GEMINI.md",
    }
    for name, default in client_defaults.items():
        assert _default_line_present(name, default, _CLIENT_TEMPLATE), (
            f"{name}'s commented default line in the client template does not "
            f"match the code default {default!r} (memory_bridge.py)"
        )
    # SHARED_MEMORY_PROJECT's own code default is "" (the empty string, i.e.
    # "use the directory walk") -- there is no non-empty literal to pin, so it
    # is checked for presence + staying commented instead, in the coverage
    # test above and the commented-not-live check below.

    framework_defaults = {
        "PG_HOST": "localhost",
        "PG_PORT": "5432",
        "PG_USER": "postgres",
        "PG_DATABASE": "agent_data",
        "PG_MAINTENANCE_DB": "postgres",
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USER": "neo4j",
        "PROJECT_CLOSE_MATCH_CUTOFF": "0.6",
    }
    for name, default in framework_defaults.items():
        assert _default_line_present(name, default, _FRAMEWORK_TEMPLATE), (
            f"{name}'s commented default line in the framework template does "
            f"not match the code default {default!r}"
        )
    # PROJECT_ROOTS's own code default is "" (no roots configured) -- same
    # reasoning as SHARED_MEMORY_PROJECT above.

    # Round 2 (2026-09-10): the 15 further framework variables, each asserted
    # by the CODE's real default, not a guess (round-2 ruling). See
    # W7_SETS.md for the file:line each was read from.
    round_2_defaults = {
        "DOMAIN_CONFUSABLE_SIMILARITY": "0.6",
        "ENTITY_CONFUSABLE_SIMILARITY": "0.6",
        "PROJECT_CONFUSABLE_SIMILARITY": "0.6",
        "MIN_ENTITY_NAME_LEN": "2",
        "MAX_ENTITY_NAME_WORDS": "4",
        # REM_TEMPERATURE/NREM_TEMPERATURE fall through to DREAM_TEMPERATURE
        # when unset, and to 0.6 if DREAM_TEMPERATURE is unset too -- the
        # ULTIMATE default (both unset) is what is asserted here.
        "REM_TEMPERATURE": "0.6",
        "NREM_TEMPERATURE": "0.6",
        "NREM_INSIGHT_SLOT_INPUT_CHARS": "2000",
        "WRITE_QUIESCE_SEC": "30",
        # hive_mind_proxy.py's own default is the *expression* str(1024*1024);
        # the LITERAL it evaluates to is what is asserted here.
        "EMBED_RERANK_BUFFER_CAP": "1048576",
        "LLM_WEDGE_SUSPECT_AGE": "900",
        # secure_env.py's own default is the *expression* str(64*1024); the
        # LITERAL it evaluates to is what is asserted here.
        "SECURE_ENV_SECRET_FILE_MAX_BYTES": "65536",
        "POOL_STATUS_URL": "http://localhost:8888/pool/status",
    }
    for name, default in round_2_defaults.items():
        assert _default_line_present(name, default, _FRAMEWORK_TEMPLATE), (
            f"{name}'s commented default line in the framework template does "
            f"not match the code default {default!r}"
        )
    # SMEM_ONTOLOGY_PATH's own code default is COMPUTED, not a literal (unset
    # = try shared-memory/ontology.yaml, then a repo-root fallback) -- no
    # single string to pin, so it is checked for presence + staying commented
    # only, same as SHARED_MEMORY_PROJECT/PROJECT_ROOTS above.
    assert _template_covers("SMEM_ONTOLOGY_PATH", _FRAMEWORK_TEMPLATE)

    # EMBED_URL (round 3, C1 correction): migrate_retro_edges.py no longer
    # has its own literal default -- it derives from GATEWAY_URL +
    # "/v1/embeddings" by default and only reads EMBED_URL as a deprecated
    # override (os.environ.get("EMBED_URL"), no second arg -- no default to
    # pin). Presence + staying commented only, same reasoning as
    # SMEM_ONTOLOGY_PATH above.
    assert _template_covers("EMBED_URL", _FRAMEWORK_TEMPLATE)

    # PG_CONN is DELIBERATELY EXEMPT from the default-value assertion. Its
    # code default (coordinator.py:2388-2389) is
    # f"postgresql://postgres:{_pg_pass}@localhost:5432/agent_data" -- it
    # INTERPOLATES A LIVE PASSWORD. This repository is a public mirror and
    # ships placeholder values only, so asserting the real default here would
    # mean asserting a line that must never exist in this file. Presence is
    # still required (enforced by the coverage test above, since PG_CONN is
    # in TEMPLATE-SET, not ALLOWLIST-SET) -- this is a documented, deliberate
    # gap in the default-equality check, not an oversight.
    assert "PG_CONN" in _PRESENCE_ONLY_KEYS
    assert _template_covers("PG_CONN", _FRAMEWORK_TEMPLATE), (
        "PG_CONN is presence-only (see this test's docstring) but must still "
        "be present in the framework template"
    )


def test_templated_keys_stay_commented_not_live():
    """LOAD-BEARING, not style (brief D4): sync_skills.sh/update_skill.sh MERGE
    .env.example into every live skill .env. An uncommented
    SHARED_MEMORY_PROJECT= would silently pin every agent's project tag
    fleet-wide on the next sync."""
    with open(_CLIENT_TEMPLATE, encoding="utf-8") as f:
        client_text = f.read()
    for name in ("SHARED_MEMORY_PROJECT", "PROJECT_ROOT_MARKERS"):
        assert not re.search(
            r"^" + re.escape(name) + r"=", client_text, re.MULTILINE
        ), f"{name} is LIVE (uncommented) in the client .env.example — must stay commented"

    with open(_FRAMEWORK_TEMPLATE, encoding="utf-8") as f:
        framework_text = f.read()
    for name in (
        "PG_HOST",
        "PG_PORT",
        "PG_USER",
        "PG_DATABASE",
        "PG_MAINTENANCE_DB",
        "NEO4J_URI",
        "NEO4J_USER",
        "PROJECT_CLOSE_MATCH_CUTOFF",
        "PROJECT_ROOTS",
        "PG_CONN",
        "DOMAIN_CONFUSABLE_SIMILARITY",
        "ENTITY_CONFUSABLE_SIMILARITY",
        "PROJECT_CONFUSABLE_SIMILARITY",
        "MIN_ENTITY_NAME_LEN",
        "MAX_ENTITY_NAME_WORDS",
        "REM_TEMPERATURE",
        "NREM_TEMPERATURE",
        "NREM_INSIGHT_SLOT_INPUT_CHARS",
        "WRITE_QUIESCE_SEC",
        "EMBED_RERANK_BUFFER_CAP",
        "LLM_WEDGE_SUSPECT_AGE",
        "SECURE_ENV_SECRET_FILE_MAX_BYTES",
        "SMEM_ONTOLOGY_PATH",
        "EMBED_URL",
        "POOL_STATUS_URL",
    ):
        assert not re.search(
            r"^" + re.escape(name) + r"=", framework_text, re.MULTILINE
        ), f"{name} is LIVE (uncommented) in shared-memory/.env.example — must stay commented"
