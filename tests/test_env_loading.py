"""The verifiers must load their env WITHOUT python-dotenv installed.

These two scripts exist to PROVE something — that a fresh install matches the
live schema, and that every declared graph constraint is in force. Both used to
`import dotenv` and `return` silently when it was absent, so with the dependency
missing they loaded nothing and the next connection failed with
`fe_sendauth: no password supplied`: a CREDENTIALS error reported for what was
actually a missing dependency.

That mattered more than an ordinary papercut because **every documented
invocation omits the dependency** — `AGENTS.md` and `README.md` between them
show five `uv run` lines for these two tools, none with `--with python-dotenv`.
So the documented way to prove an install was sound could not work, and failed
by pointing the reader at passwords, roles and `pg_hba`.

The fix is the form `apply.py` has always used: parse the file directly. These
tests bite the property rather than the wording — they run the loader with the
`dotenv` import BLOCKED, which is the condition the defect needed.
"""
import builtins
import importlib
import os
import sys

import pytest

_MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "migrations")
sys.path.insert(0, _MIGRATIONS)

VERIFIERS = ["verify_schema_init", "verify_neo4j_init"]

# Every module in the tree that parses the framework .env with
# `key, _, val = line.partition("=")`. The two verifiers plus the two migration
# tools; all four import from shared-memory/migrations with no side effects.
# The two verifiers point at the tmp tree through `HERE`, the other two through
# `MIGRATIONS_DIR`.
LOADERS = VERIFIERS + ["apply", "generate_schema_init"]
_TMP_DIR_ATTR = {
    "verify_schema_init": "HERE",
    "verify_neo4j_init": "HERE",
    "apply": "MIGRATIONS_DIR",
    "generate_schema_init": "MIGRATIONS_DIR",
}


@pytest.fixture
def no_dotenv(monkeypatch):
    """Make `import dotenv` raise, the way an install without it behaves."""
    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "dotenv" or name.startswith("dotenv."):
            raise ImportError("python-dotenv is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.delitem(sys.modules, "dotenv", raising=False)


@pytest.mark.parametrize("module_name", VERIFIERS)
def test_the_verifier_loads_its_env_without_python_dotenv(
    module_name, no_dotenv, monkeypatch, tmp_path
):
    """THE regression. With dotenv unavailable the loader must still populate the
    environment from the framework .env — otherwise the tool dies reporting a
    credentials problem it does not have."""
    mod = importlib.import_module(module_name)
    (tmp_path / "shared-memory").mkdir()
    (tmp_path / "shared-memory" / ".env").write_text(
        "PG_PASSWORD=from-the-framework-env\n"
        "# a comment line\n"
        "\n"
        "NEO4J_PASSWORD=graph-secret\n"
    )
    # HERE is <repo>/shared-memory/migrations, so HERE.parent is the framework dir.
    monkeypatch.setattr(mod, "HERE", tmp_path / "shared-memory" / "migrations")
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)

    mod._load_env()

    assert os.environ.get("PG_PASSWORD") == "from-the-framework-env"
    assert os.environ.get("NEO4J_PASSWORD") == "graph-secret"


@pytest.mark.parametrize("module_name", VERIFIERS)
def test_the_repo_root_env_is_a_fallback_not_the_first_choice(
    module_name, no_dotenv, monkeypatch, tmp_path
):
    """The framework env is `shared-memory/.env`; the repo root is the pre-0.6
    fallback. Three scripts once read the root ALONE and died on a correctly
    installed machine, which is why the candidate list has both — in this order."""
    mod = importlib.import_module(module_name)
    (tmp_path / "shared-memory").mkdir()
    (tmp_path / "shared-memory" / ".env").write_text("PG_PASSWORD=framework\n")
    (tmp_path / ".env").write_text("PG_PASSWORD=repo-root\n")
    monkeypatch.setattr(mod, "HERE", tmp_path / "shared-memory" / "migrations")
    monkeypatch.delenv("PG_PASSWORD", raising=False)

    mod._load_env()

    assert os.environ.get("PG_PASSWORD") == "framework"


@pytest.mark.parametrize("module_name", VERIFIERS)
def test_a_real_environment_variable_is_never_overwritten_by_a_file(
    module_name, no_dotenv, monkeypatch, tmp_path
):
    """An operator pointing the tool at another database with an exported value
    must not be silently handed this deployment's instead."""
    mod = importlib.import_module(module_name)
    (tmp_path / "shared-memory").mkdir()
    (tmp_path / "shared-memory" / ".env").write_text("PG_PASSWORD=from-file\n")
    monkeypatch.setattr(mod, "HERE", tmp_path / "shared-memory" / "migrations")
    monkeypatch.setenv("PG_PASSWORD", "exported-by-the-operator")

    mod._load_env()

    assert os.environ.get("PG_PASSWORD") == "exported-by-the-operator"


@pytest.mark.parametrize("module_name", VERIFIERS)
def test_a_missing_env_file_is_not_an_error(module_name, no_dotenv, monkeypatch, tmp_path):
    """A deployment configured purely through exported variables has no file at
    all, and the loader must be a no-op rather than a crash."""
    mod = importlib.import_module(module_name)
    monkeypatch.setattr(mod, "HERE", tmp_path / "nowhere" / "migrations")
    mod._load_env()   # must not raise


@pytest.mark.parametrize("module_name", LOADERS)
def test_a_pasted_banner_line_is_skipped_not_fatal(module_name, monkeypatch, tmp_path):
    """A line with an EMPTY KEY must be skipped, and the loader must keep going.

    Measured (fact:2002): the block generate_tokens.py printed was pasted into
    the framework .env, leaving lines like `=== Gateway .env — ... ===`. Those
    are non-blank, do not start with `#`, and DO contain `=`, so they reach
    `line.partition("=")` with an empty key — and
    `os.environ.setdefault("", ...)` raises `OSError: [Errno 22] Invalid
    argument`. update_framework.sh runs apply.py before its restart, so the
    update stopped there (fact:1997).

    THE ORDERING IS THE PROOF: `SM_TEST_KEY` is written AFTER the banner, so a
    loader that merely `return`s or `break`s on the bad line would leave it
    unset. Only `continue` gets it into the environment.
    """
    mod = importlib.import_module(module_name)
    (tmp_path / "shared-memory").mkdir()
    (tmp_path / "shared-memory" / ".env").write_text(
        "=== Gateway .env — add this line (digest form; safe to print/paste) ===\n"
        "SM_TEST_KEY=value\n"
        "=\n"
        "# comment\n"
    )
    monkeypatch.setattr(
        mod, _TMP_DIR_ATTR[module_name], tmp_path / "shared-memory" / "migrations",
    )
    monkeypatch.delenv("SM_TEST_KEY", raising=False)

    try:
        mod._load_env()   # must not raise OSError
        assert os.environ.get("SM_TEST_KEY") == "value", (
            "the key defined AFTER the banner never reached the environment — "
            "the loader stopped at the bad line instead of skipping it")
    finally:
        os.environ.pop("SM_TEST_KEY", None)
