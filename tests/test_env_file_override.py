"""SECURE_ENV_FILE — the one decision point for which .env a process loads.

The loader re-populates ``os.environ`` from the framework .env on every
module reload, and a test (or any embedder) can defeat that only by SETTING
a key — never by deleting one, since setdefault re-adds what delenv removed.
So hermeticity needs a switch at the FILE level: a path loads exactly that
file, the empty string loads nothing, and a set-but-missing path is loud and
loads nothing rather than silently falling through to a file the deployer
did not name.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _fresh():
    import secure_env
    importlib.reload(secure_env)
    return secure_env


def test_empty_override_selects_no_env_file(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    assert _fresh()._select_env_file() is None


def test_whitespace_override_also_selects_no_env_file(monkeypatch):
    monkeypatch.setenv("SECURE_ENV_FILE", "   ")
    assert _fresh()._select_env_file() is None


def test_explicit_path_is_selected_verbatim(monkeypatch, tmp_path):
    f = tmp_path / "custom.env"
    f.write_text("SOME_CONFIG_KEY=value\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(f))
    assert _fresh()._select_env_file() == f


def test_missing_path_is_loud_and_loads_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SECURE_ENV_FILE", str(tmp_path / "nope.env"))
    assert _fresh()._select_env_file() is None
    assert "SECURE_ENV_FILE" in capsys.readouterr().err


def test_loading_an_explicit_file_splits_config_from_secrets(monkeypatch, tmp_path):
    f = tmp_path / "custom.env"
    f.write_text("MY_PLAIN_CONFIG=hello\nMY_TEST_PASSWORD=hunter2\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(f))
    monkeypatch.delenv("MY_PLAIN_CONFIG", raising=False)
    se = _fresh()
    se.load_split_env()
    assert os.environ.get("MY_PLAIN_CONFIG") == "hello"
    assert "MY_TEST_PASSWORD" not in os.environ          # secret: store only
    assert se.get_secret("MY_TEST_PASSWORD") == "hunter2"
    monkeypatch.delenv("MY_PLAIN_CONFIG", raising=False)


def test_empty_override_makes_load_a_no_op(monkeypatch):
    """The hermeticity contract conftest.py relies on: with SECURE_ENV_FILE
    empty, a reload-triggered load_split_env() reads no file and therefore
    cannot resurrect a key a test deleted."""
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    monkeypatch.delenv("LLM_BACKENDS_JSON", raising=False)
    se = _fresh()
    se.load_split_env()
    assert "LLM_BACKENDS_JSON" not in os.environ


# ── the CLIENT honours the same contract ────────────────────────────────────

def _exec_bridge():
    import importlib.util
    path = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py"))
    spec = importlib.util.spec_from_file_location("memory_bridge_env_override", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge_env_override"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_client_empty_override_walks_no_candidates(monkeypatch):
    """In admin mode the client's candidate 2 IS the live gateway .env —
    with the pin empty it must load nothing, or a test importing the bridge
    plants the deployer's config (LLM_BACKENDS_JSON included) in os.environ
    for every later test in the process."""
    monkeypatch.setenv("SECURE_ENV_FILE", "")
    assert _exec_bridge()._ENV_CANDIDATES == []


def test_client_explicit_override_is_the_only_candidate(monkeypatch, tmp_path):
    f = tmp_path / "client.env"
    f.write_text("")
    monkeypatch.setenv("SECURE_ENV_FILE", str(f))
    assert _exec_bridge()._ENV_CANDIDATES == [str(f)]
