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
