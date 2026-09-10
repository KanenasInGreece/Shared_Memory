"""Every environment variable the framework's own scripts read must be either
TEMPLATED (a `KEY=` / `# KEY=` line in the right `.env.example`) or explicitly
ALLOWLISTED by name with a falsifiable reason.

⭐ WHAT THIS TEST CLAIMS, EXACTLY. Every `os.environ` read, env-HELPER call,
module-level key constant and string function default **in the Python glob
below** is templated or excused. Shell scripts, the compose file and the
systemd units read names of their own; those are outside this mechanism and
this test says nothing about them.

WHY AST, NEVER A LINE REGEX. `memory_bridge.py` reads `PROJECT_ROOT_MARKERS`
inside a multi-line `os.environ.get(` call (`:296-297`) — the exact variable
the v0.9.71 audit and the v1 re-audit both missed. A same-line regex matches
`os.environ.get("X"` only when the whole call sits on one line, so it goes
green while missing precisely the variable this test exists to catch. AST
parsing sees the call regardless of how the source wraps it.

⭐ WHY DIRECT `os.environ` READS ARE NOT ENOUGH — the W7/F2 defect. Until this
repair the extractor enumerated only `os.environ.get("LITERAL")`,
`os.getenv("LITERAL")` and `os.environ["LITERAL"]`. That is not how this tree
mostly reads configuration. **Measured twice, independently:** deleting
`# POOL_MIN=2` from `shared-memory/.env.example` left the suite 4/4 GREEN,
while deleting `# PG_HOST=localhost` correctly turned it red — a gate
certifying an incomplete fix. Three further forms are therefore discovered:

  1. **ENV-HELPER CALLS.** A helper is a function whose body reads one of its
     own PARAMETERS through `os.environ.get(p)`, `os.getenv(p)`,
     `os.environ[p]` **or `p in os.environ`**. That last form is not optional:
     `secure_env.get_secret` (`secure_env.py:999-1000`) is a membership test
     plus a subscript, and it is how this tree reads SECRETS — without it the
     repair would miss the single most important helper in the repository.
     Call sites are matched in BOTH bare (`get_secret("X")`) and attribute
     (`secure_env.get_secret("X")`) form, and the string-literal argument in
     the parameter's own position is collected.
  2. **MODULE-LEVEL STRING CONSTANTS USED AS THE KEY** —
     `os.environ.get(_ENV_ROSTER_VAR)` with
     `_ENV_ROSTER_VAR = "SHARED_MEMORY_READ_ONLY_AGENTS"` (`agent_roles.py`).
  3. **STRING FUNCTION DEFAULTS THAT ARE THEN READ** —
     `def read_daemon_token_from_fd(env_var: str = "AGENT_TOKEN_FD")`, whose
     every call site passes nothing, so the name exists only as a default.

⛔ DISCOVERY IS ONE LEVEL DEEP, AND THAT LIMIT IS DELIBERATE AND STATED. A
helper that reads a parameter is found; a helper that merely FORWARDS its
parameter to another helper is not. No pair in the tree does that today, but
the rule does not survive an obvious refactor (`def _cfg(name): return
_env_int(name, 0)` would hide every one of its call sites again), and an
undocumented limit is exactly how this class of defect recurs. If such a
forwarding helper is ever added, extend `_discover_env_helpers` to iterate to
a fixed point — do not allowlist the names it hides.

⚠ THE CONSTANT MAP IS REPO-WIDE, NOT PER-FILE, so a constant defined in one
module and imported into another still resolves. Two modules defining the SAME
constant name with different values would make this over-collect — which fails
LOUD (a template line is demanded for a name that is read somewhere), never
silently, and no such collision exists today.

WHY A FAIL-CLOSED GLOB, NEVER A HARD-CODED FILE LIST. A literal list of
filenames proves nothing about the file nobody remembered to add — that is
what happened to `apply.py` before this cycle (D5). `shared-memory/scripts/*.py`
+ `shared-memory/migrations/*.py`, every `.py` file in scope; `_EXCLUDE`
below is the one place a file leaves that scope, and it must name a reason.

⛔ WHY `mcp/vector-skill.py` IS NOT IN THE GLOB. `_collect()` splits CLIENT
from FRAMEWORK on `basename == "memory_bridge.py"`. Globbing the MCP connector
in would therefore dump its thirteen MCP-CLIENT variable names into the
FRAMEWORK template's expected set and turn this gate red on names that must
never appear in `shared-memory/.env.example`. The connector's own environment
is documented in prose (`mcp/system-prompt.md`, `mcp/README.md`); it is parsed
here for ONE narrow purpose only — `_OUT_OF_GLOB_READERS`, which keeps the
allowlist's staleness check honest about a name like `VECTOR_SKILL_ENV` that
is genuinely read, just not inside the glob.

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

⛔ THE BALANCE IS NOT NEGOTIABLE. Every name the mechanism finds and the
templates do not carry is TEMPLATED, with the code's real default read out of
the code. Allowlisting a knob to make this gate look complete is the one thing
both adversarial reviews of the W7 proposal independently said must not be
traded away: an allowlist entry means "templating this would be WRONG", never
"templating this was work".
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

# Files that are OUT of the coverage glob (their variables belong to no
# template this test owns) but ARE real readers — parsed only so the
# allowlist's staleness check can tell "no longer read anywhere" from "read
# somewhere this gate does not cover". See the docstring.
_OUT_OF_GLOB_READERS = [
    os.path.join(REPO_ROOT, "mcp", "vector-skill.py"),
]

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
        "VECTOR_SKILL_ENV",
        "the MCP connector's twin of SECURE_ENV_FILE, and inert for the same "
        "reason: mcp/vector-skill.py:215 reads it to CHOOSE which .env to load, "
        "before loading one, so a line inside that file could never be seen. It "
        "belongs in the MCP host's own env block (mcp/README.md documents it "
        "there). Listed here rather than in a template because the connector is "
        "deliberately outside this test's glob — see the module docstring.",
    ),
    (
        "AGENT_TOKEN_FD",
        "an internal parent-to-child handoff, not configuration: "
        "hive_mind_proxy.py:2950 sets it on the environment of each daemon it "
        "spawns, naming the read end of a pipe it just created "
        "(secure_env.read_daemon_token_from_fd reads it back). A file-descriptor "
        "number is meaningless outside that one process tree, so an "
        "operator-written value could only ever point at the wrong fd — or at "
        "someone else's.",
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
# docstring for why this one key is presence-checked only.
_PRESENCE_ONLY_KEYS = {"PG_CONN"}


def _iter_input_files():
    seen = set()
    for pattern in _GLOB_PATTERNS:
        for path in sorted(glob.glob(pattern)):
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


def _environ_key_expression(node):
    """If `node` reads `os.environ` in any supported form, return the AST node
    standing for the KEY; otherwise None.

    Four forms, and the fourth is the one the pre-W7 extractor lacked:
      os.environ.get(<key>)   os.getenv(<key>)
      os.environ[<key>]       <key> in os.environ
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        if func.attr == "get" and _is_os_environ_attr(func.value) and node.args:
            return node.args[0]
        if (
            func.attr == "getenv"
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
            and node.args
        ):
            return node.args[0]
    if isinstance(node, ast.Subscript) and _is_os_environ_attr(node.value):
        return node.slice
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.In)
        and _is_os_environ_attr(node.comparators[0])
    ):
        return node.left
    return None


def _positional_params(func_node):
    """(names, default-nodes) aligned by index, positional parameters only."""
    args = func_node.args
    names = [a.arg for a in list(args.posonlyargs) + list(args.args)]
    defaults = [None] * (len(names) - len(args.defaults)) + list(args.defaults)
    return names, defaults


def _parse_all(paths):
    trees = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            trees[path] = ast.parse(f.read(), filename=path)
    return trees


def _module_string_constants(trees):
    """Repo-wide {NAME: "value"} for module-level string assignments."""
    consts = {}
    for tree in trees.values():
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                consts[node.targets[0].id] = node.value.value
    return consts


def _discover_env_helpers(trees):
    """{function name: (parameter index, parameter name, string default or None)}

    ONE LEVEL DEEP by design — see the module docstring.
    """
    helpers = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names, defaults = _positional_params(node)
            if not names:
                continue
            for child in ast.walk(node):
                key = _environ_key_expression(child)
                if isinstance(key, ast.Name) and key.id in names:
                    index = names.index(key.id)
                    default = defaults[index]
                    literal = (
                        default.value
                        if isinstance(default, ast.Constant)
                        and isinstance(default.value, str)
                        and default.value
                        else None
                    )
                    helpers[node.name] = (index, names[index], literal)
                    break
    return helpers


def _called_function_name(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _extract_names(tree, helpers, consts):
    """Every environment-variable NAME one parsed module reads — directly, via
    an env-helper call site, via a module constant, or via a helper's own
    string default."""
    names = set()

    def _resolve(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in consts:
            return consts[node.id]
        return None

    for node in ast.walk(tree):
        key = _environ_key_expression(node)
        if key is not None:
            resolved = _resolve(key)
            if resolved:
                names.add(resolved)
        if isinstance(node, ast.Call):
            fname = _called_function_name(node)
            if fname in helpers:
                index, param, default = helpers[fname]
                arg = None
                if len(node.args) > index:
                    arg = node.args[index]
                else:
                    for kw in node.keywords:
                        if kw.arg == param:
                            arg = kw.value
                            break
                if arg is None:
                    if default:
                        names.add(default)
                else:
                    resolved = _resolve(arg)
                    if resolved:
                        names.add(resolved)
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
    paths = list(_iter_input_files())
    trees = _parse_all(paths)
    consts = _module_string_constants(trees)
    helpers = _discover_env_helpers(trees)

    client_names = set()
    framework_names = set()
    for path, tree in trees.items():
        names = _extract_names(tree, helpers, consts)
        if os.path.basename(path) == "memory_bridge.py":
            client_names |= names
        else:
            framework_names |= names
    return client_names, framework_names


def _names_read_outside_the_glob():
    """Names read by a real reader this gate deliberately does not template.
    Used ONLY by the staleness check — never by the coverage assertion."""
    existing = [p for p in _OUT_OF_GLOB_READERS if os.path.isfile(p)]
    if not existing:
        return set()
    trees = _parse_all(existing)
    consts = _module_string_constants(trees)
    helpers = _discover_env_helpers(trees)
    names = set()
    for tree in trees.values():
        names |= _extract_names(tree, helpers, consts)
    return names


def test_the_env_helper_discovery_actually_finds_the_helpers_this_tree_uses():
    """The repair itself, asserted rather than assumed.

    `get_secret` is the one that matters: it is a MEMBERSHIP TEST plus a
    subscript (`if name in os.environ: return os.environ[name]`), the form the
    pre-W7 extractor could not see, and it is how this tree reads secrets. If
    this assertion ever fails, every `get_secret("...")` name has silently
    dropped out of the catch-set again.
    """
    trees = _parse_all(list(_iter_input_files()))
    helpers = _discover_env_helpers(trees)
    for name in ("get_secret", "_env_int", "_env_float", "read_daemon_token_from_fd"):
        assert name in helpers, (
            f"{name} is an env helper in this tree but discovery did not find "
            f"it — found {sorted(helpers)}"
        )
    assert helpers["read_daemon_token_from_fd"][2] == "AGENT_TOKEN_FD", (
        "read_daemon_token_from_fd's env name exists only as a string default "
        "(no call site passes one); discovery must carry that default"
    )


def test_the_allowlist_has_no_stale_entry():
    """A name no longer read anywhere must be removed from the allowlist —
    otherwise the list only ever grows and nobody can tell which entries still
    matter (brief PART 3, T1). `_OUT_OF_GLOB_READERS` is consulted here and
    ONLY here, so an entry excusing a name read by the MCP connector is not
    reported as stale."""
    client_names, framework_names = _collect()
    all_names = client_names | framework_names | _names_read_outside_the_glob()
    stale = [name for name in _ALLOWLIST_NAMES if name not in all_names]
    assert not stale, (
        f"allowlist entries no longer read anywhere: {stale} — "
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
    # by the CODE's real default, not a guess (round-2 ruling).
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

    # Round 4 (W7/F2): the nine the repaired extractor uncovered -- every one
    # of them reached only through an env-HELPER call or a module constant,
    # which is why no earlier round saw them. Same rule: the CODE's default
    # (coordinator.py:1053/1122/1123/1397/1402/2504/2511/2519), read out of the
    # code, never retyped from a brief.
    round_4_defaults = {
        "ENTITY_NAME_MAX_LEN": "200",
        "ENTITY_LIST_MAX_LEN": "50",
        "ENTITY_PROPOSAL_LIMIT": "5",
        "GRAPH_EXPANSION_LIMIT": "15",
        "SEARCH_CANDIDATE_FLOOR": "20",
        "SEARCH_DOMAINS_FILTER_CAP": "16",
        # _env_float, so the default is 45.0 -- the literal the code carries,
        # not the integer it would round to.
        "BACKUP_DAEMON_DRAIN_TIMEOUT": "45.0",
        "BACKUP_RETRY_AFTER": "30",
    }
    for name, default in round_4_defaults.items():
        assert _default_line_present(name, default, _FRAMEWORK_TEMPLATE), (
            f"{name}'s commented default line in the framework template does "
            f"not match the code default {default!r}"
        )
    # SHARED_MEMORY_READ_ONLY_AGENTS' own code default is "" (agent_roles.py:64
    # -- the built-in roster alone), so there is no literal to pin: presence +
    # staying commented only, same as SHARED_MEMORY_PROJECT above.
    assert _template_covers("SHARED_MEMORY_READ_ONLY_AGENTS", _FRAMEWORK_TEMPLATE)

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
        "ENTITY_NAME_MAX_LEN",
        "ENTITY_LIST_MAX_LEN",
        "ENTITY_PROPOSAL_LIMIT",
        "GRAPH_EXPANSION_LIMIT",
        "SEARCH_CANDIDATE_FLOOR",
        "SEARCH_DOMAINS_FILTER_CAP",
        "BACKUP_DAEMON_DRAIN_TIMEOUT",
        "BACKUP_RETRY_AFTER",
        "SHARED_MEMORY_READ_ONLY_AGENTS",
    ):
        assert not re.search(
            r"^" + re.escape(name) + r"=", framework_text, re.MULTILINE
        ), f"{name} is LIVE (uncommented) in shared-memory/.env.example — must stay commented"
