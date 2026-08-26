"""The evidence sweep is RETIRED (v0.9.67) — no process mints a typed
Entity→Entity edge, and the reference resolver has no LLM judge.

Two guards, both structural because the subject is an ABSENCE:

1. The sweep's module is gone and its name survives nowhere in the shipped
   Python — comments included. A retired mechanism whose name still appears in
   a live comment teaches the next reader a contract that no longer exists,
   which is exactly how the fenced-off-but-not-deleted shape creeps back.
2. `reference_resolver` exposes no judge surface at all and classifies with
   zero I/O: any attempt to open a socket during classification fails the test,
   so the deterministic path cannot quietly regain a network branch.
"""
import os
import socket
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
MCP_DIR = os.path.join(os.path.dirname(__file__), "..", "mcp")

sys.path.insert(0, SCRIPTS_DIR)
from ontology import ONT  # noqa: E402
import reference_resolver as rr  # noqa: E402


def _shipped_python() -> list[str]:
    out = []
    for root in (SCRIPTS_DIR, MCP_DIR):
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.endswith(".py"):
                    out.append(os.path.join(dirpath, fn))
    return sorted(out)


def test_relation_sweep_module_is_gone():
    assert not os.path.exists(os.path.join(SCRIPTS_DIR, "relation_sweep.py"))


def test_no_shipped_python_names_the_relation_sweep():
    """Whole-file read (comments and docstrings included), not an import scan."""
    files = _shipped_python()
    assert len(files) > 10, f"walk found only {len(files)} files — wrong root?"
    offenders = []
    for path in files:
        with open(path, encoding="utf-8") as fh:
            if "relation_sweep" in fh.read():
                offenders.append(os.path.relpath(path, os.path.dirname(__file__)))
    assert offenders == [], f"retired sweep still named in: {offenders}"


# ── The reference resolver's judge ────────────────────────────────────────────

@pytest.mark.parametrize("attr", ["judge_enabled", "REFERENCE_JUDGE_MODE", "_MODE", "_URL", "_MODEL"])
def test_reference_resolver_has_no_judge_surface(attr):
    assert not hasattr(rr, attr), f"reference_resolver still exposes {attr}"


def test_classify_relation_opens_no_socket(monkeypatch):
    """The removed branch reached the network through httpx, which reaches it
    through socket — so forbidding socket creation catches any resurrection,
    whichever client library it picks."""
    def _forbidden(*a, **kw):
        raise AssertionError("classify_relation must perform no I/O")
    monkeypatch.setattr(socket, "socket", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    assert rr.classify_relation(ONT.decision, ONT.decision, "addendum to 257") == ONT.informed_by
    assert rr.classify_relation(ONT.fact, ONT.decision, "see 276") == ONT.references
    assert rr.classify_relation(ONT.decision, ONT.fact, "refines decision 381") == ONT.references


def test_classify_relation_takes_no_client_argument():
    """The `client=` keyword was the judge's connection-reuse hook; a call site
    still passing one would be silently accepted by **kwargs, so assert the
    signature itself."""
    import inspect
    params = list(inspect.signature(rr.classify_relation).parameters)
    assert params == ["src_label", "tgt_label", "snippet"]
