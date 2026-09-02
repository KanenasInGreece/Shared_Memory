"""SEC H (R-3, RULED 2026-09-02, measured on glxvm): a present-but-empty
PROXY_BIND must resolve to loopback, never all-interfaces.

TCPSite(runner, "", port) binds ALL interfaces (0.0.0.0 + [::]);
TCPSite(runner, "127.0.0.1", port) binds loopback only — measured on
glxvm. _resolve_proxy_bind_host() is a PURE function (no socket, no I/O)
precisely so this can be tested without ever binding a real port."""
import importlib
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _reloaded(monkeypatch, **env):
    monkeypatch.delenv("PROXY_BIND", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import hive_mind_proxy as g
    return importlib.reload(g)


def test_absent_resolves_to_loopback(monkeypatch):
    g = _reloaded(monkeypatch)
    assert g._resolve_proxy_bind_host() == "127.0.0.1"


def test_present_but_empty_resolves_to_loopback_not_all_interfaces(monkeypatch):
    """MUTATION TARGET: reverting to `os.environ.get("PROXY_BIND", "127.0.0.1")`
    (the `.get()` idiom) makes this resolve to "" instead — TCPSite would then
    bind all interfaces on a config that documents loopback-only."""
    g = _reloaded(monkeypatch, PROXY_BIND="")
    assert g._resolve_proxy_bind_host() == "127.0.0.1"


def test_whitespace_only_resolves_to_loopback(monkeypatch):
    g = _reloaded(monkeypatch, PROXY_BIND="   ")
    assert g._resolve_proxy_bind_host() == "127.0.0.1"


def test_explicit_all_interfaces_opt_in_is_honoured(monkeypatch):
    g = _reloaded(monkeypatch, PROXY_BIND="0.0.0.0")
    assert g._resolve_proxy_bind_host() == "0.0.0.0"


def test_explicit_value_is_honoured_and_stripped(monkeypatch):
    g = _reloaded(monkeypatch, PROXY_BIND="  10.0.0.5  ")
    assert g._resolve_proxy_bind_host() == "10.0.0.5"


def test_present_but_empty_logs_a_warning_naming_the_fallback(monkeypatch, caplog):
    g = _reloaded(monkeypatch, PROXY_BIND="")
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g._resolve_proxy_bind_host()
    assert "127.0.0.1" in caplog.text
    assert "PROXY_BIND" in caplog.text


def test_absent_does_not_log_a_warning(monkeypatch, caplog):
    g = _reloaded(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g._resolve_proxy_bind_host()
    assert caplog.text == ""


def test_explicit_value_does_not_log_a_warning(monkeypatch, caplog):
    g = _reloaded(monkeypatch, PROXY_BIND="0.0.0.0")
    with caplog.at_level(logging.WARNING, logger="hive-proxy"):
        g._resolve_proxy_bind_host()
    assert caplog.text == ""
