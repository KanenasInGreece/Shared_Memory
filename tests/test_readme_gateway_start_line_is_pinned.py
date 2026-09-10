"""D1 — the gateway start line must be PINNED (via `requirements-gateway.lock`)
at every site README.md documents it, and the systemd unit's `ExecStart` must
carry the same literal.

WHY LINE CONTINUATIONS ARE JOINED BEFORE MATCHING. README.md:1108-1109 is a
two-line continuation — the `uv run` invocation ends in a backslash and
`hive_mind_proxy.py 8888` sits alone on the next line. A matcher that looks at
one line at a time catches the single-line site (`:454`) and silently misses
this one — measured by a reviewer during this cycle's brief review. That is a
green test certifying a README that still floats the gateway's dependencies at
one of its two sites. This test joins every backslash line-continuation in the
file into one logical line before matching anything.

WHY THE EXACT VALUE, NOT THE SUBSTRING `requirements-gateway.lock`. A line
still missing `--no-project`, or one that still carries a leftover
`--with aiohttp`, both contain that substring and would pass a substring
check while shipping a broken or half-pinned invocation.

WHY `ExecStart` IS A SEPARATE, THIRD ASSERTION — NOT "README == ExecStart".
`hive-mind-gateway.service`'s `ExecStart` begins with `/usr/bin/uv`, a
documented PLACEHOLDER `install_service.sh` substitutes at install time — an
equality assertion between the two would either force README to carry that
placeholder (wrong) or force a special-case strip (fragile, hides drift on the
part that actually matters). Both are checked against the same literal
independently instead.

MUTATION EVIDENCE (captured manually, recorded in the W7 build report): each
of README's two gateway-start sites was mutated separately (one dependency flag
changed) and this test caught each failure on its own — confirming the two
sites are not aliased to a single pass/fail.
"""
import os
import re

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
README = os.path.join(REPO_ROOT, "README.md")
GATEWAY_LOCK = os.path.join(REPO_ROOT, "requirements-gateway.lock")
GATEWAY_SERVICE = os.path.join(
    REPO_ROOT, "shared-memory", "ops", "hive-mind-gateway.service"
)

# The D1 exact replacement string -- the literal README must carry, kept here
# so the documented line and this test cannot disagree (brief D1).
PINNED_GATEWAY_LINE = (
    "uv run --no-project --with-requirements requirements-gateway.lock "
    "python shared-memory/scripts/hive_mind_proxy.py 8888"
)


def _join_line_continuations(text):
    """Return [(1-based start lineno, logical line), ...], merging any
    backslash-newline continuation into the line it starts on."""
    raw_lines = text.split("\n")
    logical = []
    i = 0
    n = len(raw_lines)
    while i < n:
        start = i
        buf = raw_lines[i]
        while buf.endswith("\\") and i + 1 < n:
            i += 1
            buf = buf[:-1].rstrip() + " " + raw_lines[i].strip()
        logical.append((start + 1, buf))
        i += 1
    return logical


def _gateway_invocation_sites(logical_lines):
    """Every logical line that looks like an attempt to start the gateway
    (names hive_mind_proxy.py and the port), regardless of whether it is
    correctly pinned -- the site set itself must not depend on the fix."""
    return [
        (lineno, line)
        for lineno, line in logical_lines
        if "hive_mind_proxy.py" in line and "uv run" in line
    ]


def test_readme_gateway_start_line_is_pinned():
    with open(README, encoding="utf-8") as f:
        text = f.read()
    logical_lines = _join_line_continuations(text)
    sites = _gateway_invocation_sites(logical_lines)

    assert sites, "no gateway-start invocation found in README.md at all"
    assert len(sites) >= 2, (
        f"expected at least 2 documented gateway-start sites (D1: :454 and "
        f":1108-1109), found {len(sites)}: {[ln for ln, _ in sites]}"
    )

    for lineno, line in sites:
        assert PINNED_GATEWAY_LINE in line, (
            f"README.md:{lineno} does not carry the exact pinned gateway start "
            f"line. Got: {line!r}"
        )
        # No leftover `--with <package>` alongside the lock pin. Checked with a
        # trailing space so "--with-requirements" (part of the pinned form
        # itself) is never mistaken for a leftover "--with <pkg>" flag.
        assert "--with " not in line, (
            f"README.md:{lineno} still carries a leftover '--with <package>' "
            f"flag alongside the lock pin: {line!r}"
        )

    assert os.path.isfile(GATEWAY_LOCK), (
        "requirements-gateway.lock does not exist in-tree -- the pinned line "
        "points at a file that isn't there"
    )


def test_gateway_service_execstart_carries_the_same_pinned_literal():
    """A separate, third assertion -- never README == ExecStart (see module
    docstring)."""
    with open(GATEWAY_SERVICE, encoding="utf-8") as f:
        service_text = f.read()
    execstart_lines = [
        line for line in service_text.split("\n") if line.startswith("ExecStart=")
    ]
    assert execstart_lines, "no ExecStart= line found in hive-mind-gateway.service"
    assert PINNED_GATEWAY_LINE in execstart_lines[0], (
        f"ExecStart does not carry the same pinned literal as README's proposed "
        f"text: {execstart_lines[0]!r}"
    )
