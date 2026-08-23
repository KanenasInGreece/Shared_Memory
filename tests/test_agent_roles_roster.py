"""The read-only roster — one definition, read by the minter and the gateway.

Operator ruling, 2026-08-23: "always read only, enforced", then "we may have
other agents read only. so keep the code enforcing anything that is supposed to
be read only."

The defect being closed: READ_ONLY_AGENTS lived only in generate_tokens.py, so
being read-only was a MINTING CONVENTION. The gateway believed AGENT_ROLES, and
absence from that line means FULL read/write — so an identity registered before
the rule was honoured, a partial write, or an older tool rewriting the line all
silently widened a read-only agent.

The gateway-side enforcement is proved in test_auth.py against the real
middleware. This file pins the rule itself, which is pure and therefore testable
without a gateway, a .env, or a database — the property whose absence let the
original asymmetry go unnoticed.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import agent_roles as ar


# ── effective_role: what the gateway actually applies ────────────────────────

@pytest.mark.parametrize("declared", [None, "full", "admin", "", "nonsense"])
def test_a_roster_identity_is_read_whatever_the_file_says(declared):
    """Every state a .env can be in, including having lost the line entirely."""
    assert ar.effective_role("monitor", declared) == "read"


def test_an_operator_declared_read_identity_is_read():
    """The roster cannot enumerate names this framework has never heard of, so a
    declaration is the other way an identity becomes read-only."""
    assert ar.effective_role("some_dashboard", "read") == "read"


def test_an_ordinary_identity_keeps_full_access():
    """The counterweight. Without it, pinning everyone to 'read' would satisfy
    every other test in this file."""
    assert ar.effective_role("claude", None) == "full"
    assert ar.effective_role("claude", "full") == "full"


def test_an_admin_identity_is_not_widened_or_narrowed():
    assert ar.effective_role("backup", "admin") == "admin"


def test_a_malformed_declaration_does_not_widen_anyone():
    """A junk value must not be honoured as a role; it falls back to the
    documented default rather than being passed through to the gateway."""
    assert ar.effective_role("backup", "administrator") == "full"


def test_the_roster_is_extensible_through_the_environment(monkeypatch):
    """A deployment confines an identity this framework has never shipped,
    without editing a shipped file."""
    monkeypatch.setenv("SHARED_MEMORY_READ_ONLY_AGENTS", "dashboard, grafana")
    assert "dashboard" in ar.read_only_agents()
    assert "grafana" in ar.read_only_agents()
    assert ar.effective_role("dashboard", "full") == "read"


def test_the_environment_roster_is_read_at_call_time(monkeypatch):
    """Not frozen at import: the gateway and the minter are separate processes,
    and a roster captured at import would ignore a later confinement."""
    assert "late_addition" not in ar.read_only_agents()
    monkeypatch.setenv("SHARED_MEMORY_READ_ONLY_AGENTS", "late_addition")
    assert "late_addition" in ar.read_only_agents()


# ── role_for_mint: what gets written into AGENT_ROLES ────────────────────────

def test_a_roster_identity_is_minted_read_without_being_asked():
    assert ar.role_for_mint("monitor") == "read"


def test_an_ordinary_identity_gets_no_entry():
    """Full access is the ABSENCE of an entry. Emitting `claude:full` would also
    'work' and would quietly change what an unlisted name means."""
    assert ar.role_for_mint("claude") is None


@pytest.mark.parametrize("widen", ["full", "admin"])
def test_widening_a_roster_identity_is_refused(widen):
    with pytest.raises(ValueError):
        ar.role_for_mint("monitor", widen)


@pytest.mark.parametrize("widen", ["full", "admin"])
def test_widening_an_already_declared_read_identity_is_refused(widen):
    """A privilege change is made by editing the declaration deliberately, never
    as a side effect of minting."""
    with pytest.raises(ValueError):
        ar.role_for_mint("some_dashboard", widen, declared="read")


def test_an_existing_read_declaration_is_preserved_when_minting():
    """Returning None here would drop the entry — and a dropped entry is FULL
    access, so the quiet path is the dangerous one."""
    assert ar.role_for_mint("some_dashboard", None, declared="read") == "read"


def test_an_unknown_role_name_is_refused():
    with pytest.raises(ValueError):
        ar.role_for_mint("claude", "superuser")


def test_restating_read_on_a_roster_identity_is_allowed():
    assert ar.role_for_mint("monitor", "read") == "read"


# ── enforce_roster: repairing a drifted file ─────────────────────────────────

def test_enforce_roster_tightens_a_widened_entry():
    assert ar.enforce_roster({"monitor": "full"})["monitor"] == "read"


def test_enforce_roster_never_widens_anything():
    """It only ever tightens — an operator confinement is not something a repair
    pass may undo."""
    before = {"backup": "admin", "some_dashboard": "read", "claude": "full"}
    assert ar.enforce_roster(before) == before


def test_enforce_roster_does_not_invent_entries_for_absent_agents():
    """A roster identity that is not registered on this host has no business
    appearing in its .env."""
    assert "monitor" not in ar.enforce_roster({"claude": "full"})
