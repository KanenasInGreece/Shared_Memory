"""The capture surface and its documentation must not drift apart.

WHY THIS EXISTS. `SKILL.md` is what every agent reads before deciding what to
elicit from an operator, and nothing executes it — so it goes stale silently and
stays stale. Measured: its worked `/health` and `--version` examples still showed
`0.5.0` and `api_version 1` roughly forty releases after both had moved, in a
document that also instructs the reader to compare `api_version` and act on a
mismatch. Prose about a mechanism ages slowly; a contract surface ages at the
speed of the release.

WHAT IT CHECKS, and the shape is deliberate: every capture FLAG a client offers,
and every ingress REFUSAL the gateway can return, must be mentioned in the skill
document. It does not check the wording — that would make every edit a test
failure and teach people to silence it. It checks that a caller-visible part of
the contract cannot be ADDED without a decision about how to explain it.

⚠ THE EXEMPTION LIST IS THE POINT, not a loophole. A new flag fails this test
until someone either documents it or names it mechanical, and both are answers;
what must not happen is a third option where nobody notices. Adding a name to
`_UNDOCUMENTED_BY_DESIGN` is a deliberate, reviewable act.
"""
import os
import re
import sys

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCRIPTS = os.path.join(_ROOT, "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

SKILL = os.path.join(_ROOT, "shared-memory", "SKILL.md")
BRIDGE = os.path.join(_SCRIPTS, "memory_bridge.py")
COORDINATOR = os.path.join(_SCRIPTS, "coordinator.py")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# Flags that carry no captured MEANING — they select an output shape, name a
# record, or repeat a value the model section already explains. Each is exempt
# because explaining it in the record model would add a line that serves no
# unique need, which is the discipline that document is held to.
_UNDOCUMENTED_BY_DESIGN = {
    "--json", "--version", "--limit", "--pg-id", "--by", "--summary-id",
    "--promote", "--date", "--source", "--title", "--decided-by", "--rationale",
    "--assisted-by", "--notes", "--elicited", "--project", "--distinct-from",
    "--new-project", "--new-domain", "--confirm-distinct-from",
}


def _client_flags() -> set:
    """Every long flag the CLI offers on a WRITE path."""
    return set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', _read(BRIDGE)))


def _ingress_errors() -> set:
    """Every machine-readable refusal code the gateway can answer a save with.

    These are the strings a client branches on, so an undocumented one is a
    refusal an agent meets with no idea what to do next — which is the specific
    failure the registry protocol exists to avoid.
    """
    return set(re.findall(r'"error":\s*"([a-z_]+)"', _read(COORDINATOR)))


def test_every_capture_flag_is_explained_in_the_skill_document():
    skill = _read(SKILL)
    # The FLAG FORM must appear, not the bare word. Matching `domain` rather
    # than `--domain` made this check nearly vacuous: any prose use of a common
    # word satisfied it, so a flag could be added, described nowhere, and still
    # pass. A reader also needs the exact string they are meant to type.
    undocumented = sorted(
        f for f in _client_flags()
        if f not in _UNDOCUMENTED_BY_DESIGN and f not in skill
    )
    assert not undocumented, (
        f"these client flags appear in no skill documentation: {undocumented}. "
        "An agent reads SKILL.md to decide whether a field applies at all — a "
        "flag it cannot find there is one nobody will elicit. Document it in the "
        "record-model section, or add it to _UNDOCUMENTED_BY_DESIGN if it "
        "captures no meaning."
    )


def test_every_ingress_refusal_is_explained_in_the_skill_document():
    skill = _read(SKILL)
    undocumented = sorted(e for e in _ingress_errors() if e not in skill)
    assert not undocumented, (
        f"these refusal codes are undocumented: {undocumented}. A client branches "
        "on `error`, so an agent that meets an undocumented refusal has no "
        "second submission it can make — the exchange dead-ends where the "
        "protocol was designed to continue."
    )


def test_every_retrospective_rating_is_explained():
    """The ratings are an enum a caller must choose from, and one of them
    (`reversed`) has a structural side effect. A caller who cannot see the list
    guesses, and a wrong guess here supersedes a decision."""
    from ontology import RETRO_RATINGS
    skill = _read(SKILL)
    missing = sorted(r for r in RETRO_RATINGS if r not in skill)
    assert not missing, f"undocumented outcome ratings: {missing}"


def test_the_worked_examples_state_the_current_contract():
    """A pasted response is part of the contract the document describes. This one
    drifted roughly forty releases while the document instructed the reader to
    compare `api_version` and act on a mismatch — so it taught a comparison
    against a number that had been wrong for a year of releases."""
    import memory_bridge
    skill = _read(SKILL)
    assert f'"{memory_bridge.VERSION}"' in skill, (
        f"SKILL.md's worked examples do not mention the current version "
        f"{memory_bridge.VERSION} — a version bump changes the client surface, "
        "so the examples must move with it."
    )
    assert f"api_version\":{memory_bridge.API_VERSION}" in skill.replace(" ", ""), (
        f"SKILL.md's worked examples do not show api_version "
        f"{memory_bridge.API_VERSION}"
    )
