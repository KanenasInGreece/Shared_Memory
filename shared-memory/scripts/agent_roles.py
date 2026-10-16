"""The read-only agent roster — ONE definition, read by the minter and the gateway.

WHY THIS FILE EXISTS. `READ_ONLY_AGENTS` used to live only in generate_tokens.py,
which meant the guarantee was a *minting convention*: whoever minted a token was
trusted to write `name:read` into AGENT_ROLES, and the gateway simply believed
whatever the file said. That has two holes, and both were found by review:

  * An agent registered BEFORE the roster was honoured on a given path keeps full
    access forever — nothing ever revisits it.
  * Absence from AGENT_ROLES means FULL read/write in the gateway, so any edit,
    partial write, or older tool that rewrites that line silently WIDENS a
    read-only identity. A guarantee that a file edit can switch off is not a
    guarantee.

⛔ THE ROSTER IS ENFORCED AT THE GATEWAY, NOT PROMISED AT MINT TIME (operator
ruling, 2026-08-23: "always read only, enforced"). The gateway applies
`effective_role()` to every authenticated request, so a name on this list is
confined no matter what AGENT_ROLES contains — including when it contains
nothing at all. Minting still writes the entry, because the .env should describe
reality, but correctness no longer depends on it having been written.

Deliberately dependency-free: generate_tokens.py runs during install, long before
the gateway's asyncpg/aiohttp stack exists, so this module must import cleanly
with nothing but the standard library.
"""

# Built-in read-only roster. A deployment can add names via SHARED_MEMORY_READ_ONLY_AGENTS, and an existing AGENT_ROLES `name:read` must not be widened by a later mint: the roster cannot know names it has never heard of.
_READ_ONLY_AGENTS_BUILTIN = ["monitor"]

# Imported by callers. read_only_agents() is the accessor that also honours the environment.
READ_ONLY_AGENTS = list(_READ_ONLY_AGENTS_BUILTIN)

_ENV_ROSTER_VAR = "SHARED_MEMORY_READ_ONLY_AGENTS"


def read_only_agents() -> "list[str]":
    """The built-in roster plus any names added by the environment.

    Read at CALL time, never frozen at import: the gateway and the minter are
    separate processes with separate lifetimes, and a roster captured at import
    would ignore an identity confined after the process started.
    """
    import os
    names = list(READ_ONLY_AGENTS)
    raw = os.environ.get(_ENV_ROSTER_VAR, "")
    for extra in raw.split(","):
        extra = extra.strip()
        if extra and extra not in names:
            names.append(extra)
    return names

# Reserved maintenance admin identity.
MAINTENANCE_ADMIN_AGENT = "maintenance-admin"

# Roles the gateway understands. Roles only ever NARROW access; absence from the
# map means full read/write, which is why an omission is the WIDEST outcome and
# never a safe default.
VALID_ROLES = ("read", "full", "admin")


def effective_role(name: str, declared: "str | None") -> str:
    """The role actually applied to `name`, given what AGENT_ROLES declared.

    `declared` is whatever the file said, or None when the file said nothing.

    A read-only identity resolves to "read" in every case — declared "full",
    declared "admin", declared nothing, or a file that has lost the line
    entirely.

    MAINTENANCE_ADMIN_AGENT ("maintenance-admin") resolves to "admin" when its
    declared role is missing, "full" or "write"; an explicit "read", or
    membership of the read-only roster, stays "read" (fact:3099: maintenance-admin pin narrows only).

    Everyone else keeps what was declared, defaulting to "full", which
    preserves the long-standing behaviour that an unlisted agent is unconfined.

    Pure, so the rule is testable without a gateway, a .env or a database.
    """
    if name in read_only_agents():
        return "read"
    if name == MAINTENANCE_ADMIN_AGENT:
        # (fact:3099: maintenance-admin pin narrows only)
        if declared == "read":
            return "read"
        return "admin"
    # An operator-declared `name:read` is itself a read-only identity — the
    # roster cannot enumerate names this framework has never heard of, so a
    # declaration is the other way in. VALID_ROLES membership is checked so a
    # malformed value cannot widen anyone by accident.
    if declared in VALID_ROLES:
        return declared
    return "full"


def role_for_mint(name: str, explicit: "str | None" = None,
                  declared: "str | None" = None) -> "str | None":
    """The role to WRITE into AGENT_ROLES when minting `name`.

    Returns None when no entry is needed (an ordinary, unconfined agent).

    ⛔ READ_ONLY_AGENTS IS AUTHORITATIVE. A name on the list always yields
    "read", and an explicit request to widen it is refused rather than honoured
    — even though the gateway would now override it anyway, because a .env that
    *claims* a monitor has full access is a lie an operator will act on.

    MAINTENANCE_ADMIN_AGENT ("maintenance-admin") resolves to "admin" when its
    declared role is missing, "full" or "write". An explicit "read" or roster
    membership yields "read". An explicit request to widen it to "full" or "write"
    raises ValueError (fact:3099: maintenance-admin pin narrows only).

    Raises ValueError on an unknown role, and on an attempt to widen a read-only
    identity or alter MAINTENANCE_ADMIN_AGENT. Callers refuse BEFORE minting anything.
    """
    roster = read_only_agents()
    if name in roster:
        if explicit is not None and explicit.strip().lower() != "read":
            raise ValueError(
                f"{name!r} is a read-only identity (roster) and always gets the "
                f"'read' role — refusing --role {explicit}. If this agent genuinely "
                f"needs write access it must be taken off the roster deliberately, "
                f"in code or in {_ENV_ROSTER_VAR}, not widened at mint time.")
        return "read"

    if name == MAINTENANCE_ADMIN_AGENT:
        if explicit is not None:
            explicit_norm = explicit.strip().lower()
            if explicit_norm == "read":
                return "read"
            if explicit_norm == "admin":
                if declared == "read":
                    raise ValueError(
                        f"{name!r} is already declared read-only in AGENT_ROLES — refusing "
                        f"--role {explicit}. Widening an existing read-only identity is a "
                        f"privilege change, so it is made by editing that declaration "
                        f"deliberately, never as a side effect of minting.")
                return "admin"
            if explicit_norm in ("full", "write"):
                raise ValueError(
                    f"{name!r} is the reserved maintenance admin identity and cannot be "
                    f"widened to --role {explicit}. Only 'admin' or 'read' are permitted."
                )
            raise ValueError(
                f"unknown role {explicit!r} — expected one of {', '.join(VALID_ROLES)}")
        if declared == "read":
            return "read"
        return "admin"

    if explicit is not None:
        explicit = explicit.strip().lower()
        if explicit not in VALID_ROLES:
            raise ValueError(
                f"unknown role {explicit!r} — expected one of {', '.join(VALID_ROLES)}")
        if declared == "read" and explicit != "read":
            raise ValueError(
                f"{name!r} is already declared read-only in AGENT_ROLES — refusing "
                f"--role {explicit}. Widening an existing read-only identity is a "
                f"privilege change, so it is made by editing that declaration "
                f"deliberately, never as a side effect of minting.")
        return explicit
    if name in roster:
        return "read"
    # Already confined by an operator declaration: preserve it rather than
    # returning None, which would drop the entry and mean FULL access.
    if declared == "read":
        return "read"
    return None


def enforce_roster(roles: dict) -> dict:
    """Return `roles` with read-only identities and maintenance admin pinned appropriately.

    Used on the WRITE side so a minted .env states the truth the gateway will
    enforce anyway — and so a roster that drifted (an agent registered before
    this rule, or a line rewritten by an older tool) is repaired the next time
    anything mints, rather than staying wrong until someone notices.
    """
    fixed = dict(roles)
    for name in read_only_agents():
        if name in fixed and fixed[name] != "read":
            fixed[name] = "read"
    if MAINTENANCE_ADMIN_AGENT in fixed:
        if fixed[MAINTENANCE_ADMIN_AGENT] != "read":
            fixed[MAINTENANCE_ADMIN_AGENT] = "admin"
    # Nothing here ever turns a "read" entry into anything wider: an operator
    # declaration is a confinement, and this function only ever tightens.
    return fixed
