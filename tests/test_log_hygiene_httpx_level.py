"""D6 (HYG round, step 2, lane alpha) — httpx logger silenced to WARNING in
the two daemons, matching the gateway half shipped in step 1.

Each of `hive_mind_proxy.py`, `rem_loop.py` and `consolidation_loop.py` runs
under its OWN `logging.basicConfig(level=logging.INFO)` root config and
imports httpx for outbound calls. At INFO, every httpx request became a
journal line duplicating the aiohttp access log's per-request record. The
fix (gateway: `hive_mind_proxy.py:118`, shipped step 1; here: `rem_loop.py`
and `consolidation_loop.py`, this lane) adds
`logging.getLogger("httpx").setLevel(logging.WARNING)` immediately after
each module's real `basicConfig(` call, so the daemon still logs its OWN
INFO lines but httpx's internal per-request chatter is dropped; a real
client failure still surfaces at WARNING.

`shared-memory/scripts/memory_bridge.py` (its own `basicConfig` near :613)
is deliberately EXEMPT and not checked here: it is a CLI client, not a
long-running journal producer, and its root logger is already WARNING (no
INFO-level httpx chatter to silence in the first place).

Two independent guards:

1. Behavioural — import (via `importlib.reload`, so collection order of
   other test files that already imported these modules cannot make this
   test a false positive) each of the three modules and assert
   `logging.getLogger("httpx").level == logging.WARNING` afterward. All
   three modules are confirmed side-effect-free to import/reload in this
   test environment: `tests/test_gateway_startup_journal_scrub.py` already
   reloads `hive_mind_proxy` per-test, `tests/test_rem_loop.py` loads
   `rem_loop.py` via `importlib.util.spec_from_file_location`, and
   `tests/test_nrem_confidence.py` does a plain `import consolidation_loop`
   — none require live infrastructure at import time (DB/network clients are
   constructed lazily inside functions, not at module scope), and
   `tests/conftest.py` pins `SECURE_ENV_FILE=""` before any daemon import so
   `secure_env.load_split_env()` (called at `hive_mind_proxy.py:62`) never
   reads a real `.env` during collection.

2. Source-level (`ast`, so comments/docstrings can't fake a pass and can't
   fake a failure either — `coordinator.py:2444`/`3457` are prose mentions
   of `basicConfig(`, not real calls, which is exactly why a naive string
   grep would be the wrong instrument; `coordinator.py` is not one of the
   three files checked here regardless) — every REAL `logging.basicConfig(`
   call in each of the three files is followed, later in the same module,
   by a `logging.getLogger("httpx").setLevel(logging.WARNING)` call.
"""
import ast
import importlib
import logging
import os
import sys

SCRIPTS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

MODULE_NAMES = ["hive_mind_proxy", "rem_loop", "consolidation_loop"]


def _reload(name):
    """Import-then-reload a daemon module so its module-level code (the
    basicConfig/setLevel calls under test) definitely re-executes now,
    regardless of whether some earlier test file already imported it."""
    mod = importlib.import_module(name)
    return importlib.reload(mod)


# ── 1. Behavioural ───────────────────────────────────────────────────────────

def _reset_httpx_logger_level():
    """`logging.getLogger("httpx")` is a process-wide singleton shared across
    every test in this session. Without resetting it to a level NONE of the
    three modules would produce (NOTSET is what a never-configured logger
    starts at) immediately before each behavioural check, a PRECEDING test's
    reload (e.g. hive_mind_proxy's, which runs first below) leaves it at
    WARNING and the next module's assertion passes whether or not THAT
    module's own setLevel call still exists — a false positive that mutation
    -testing this file caught: deleting rem_loop's setLevel line left this
    test green until the reset below was added."""
    logging.getLogger("httpx").setLevel(logging.NOTSET)


def test_hive_mind_proxy_sets_httpx_logger_to_warning():
    _reset_httpx_logger_level()
    _reload("hive_mind_proxy")
    assert logging.getLogger("httpx").level == logging.WARNING


def test_rem_loop_sets_httpx_logger_to_warning():
    _reset_httpx_logger_level()
    _reload("rem_loop")
    assert logging.getLogger("httpx").level == logging.WARNING


def test_consolidation_loop_sets_httpx_logger_to_warning():
    _reset_httpx_logger_level()
    _reload("consolidation_loop")
    assert logging.getLogger("httpx").level == logging.WARNING


# ── 2. Source-level (ast) ────────────────────────────────────────────────────

def _real_basicconfig_calls(tree):
    """Every AST Call node that is a real `logging.basicConfig(...)` call —
    excludes comments/docstrings/any other text mentioning the name."""
    calls = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "basicConfig"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logging"
        ):
            calls.append(node)
    return calls


def _httpx_warning_setlevel_calls(tree):
    """Every AST Call node that is a real
    `logging.getLogger("httpx").setLevel(logging.WARNING)` call."""
    calls = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setLevel"
        ):
            continue
        inner = node.func.value
        if not (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "getLogger"
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "logging"
        ):
            continue
        if not (
            inner.args
            and isinstance(inner.args[0], ast.Constant)
            and inner.args[0].value == "httpx"
        ):
            continue
        if not (
            node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "WARNING"
            and isinstance(node.args[0].value, ast.Name)
            and node.args[0].value.id == "logging"
        ):
            continue
        calls.append(node)
    return calls


def _assert_every_basicconfig_followed_by_httpx_warning(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src, filename=path)
    basicconfig_calls = _real_basicconfig_calls(tree)
    assert basicconfig_calls, f"expected a real logging.basicConfig( call in {path}, found none"
    setlevel_calls = _httpx_warning_setlevel_calls(tree)
    assert setlevel_calls, (
        f'expected a logging.getLogger("httpx").setLevel(logging.WARNING) call in {path}, found none'
    )
    for bc in basicconfig_calls:
        assert any(sl.lineno > bc.lineno for sl in setlevel_calls), (
            f"logging.basicConfig( at {path}:{bc.lineno} is not followed, later in the "
            f"same module, by a logging.getLogger(\"httpx\").setLevel(logging.WARNING) call"
        )


def test_hive_mind_proxy_source_basicconfig_followed_by_httpx_warning():
    _assert_every_basicconfig_followed_by_httpx_warning(
        os.path.join(SCRIPTS_DIR, "hive_mind_proxy.py")
    )


def test_rem_loop_source_basicconfig_followed_by_httpx_warning():
    _assert_every_basicconfig_followed_by_httpx_warning(
        os.path.join(SCRIPTS_DIR, "rem_loop.py")
    )


def test_consolidation_loop_source_basicconfig_followed_by_httpx_warning():
    _assert_every_basicconfig_followed_by_httpx_warning(
        os.path.join(SCRIPTS_DIR, "consolidation_loop.py")
    )
