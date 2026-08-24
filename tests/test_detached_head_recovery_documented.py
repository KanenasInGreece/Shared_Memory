"""Fix D (corpus fact:1511/fact:1512): the detached-HEAD refusal names a
concrete remedy, and AGENTS.md's upgrade runbook documents the case.

MEASURED FINDING (context item 3): update_framework.sh's detached-HEAD
refusal told an operator to "check out the release branch or tag you intend
to run" without naming WHICH ref that is on an installed host, and no
document names it either -- and the fresh-install path itself can produce a
detached HEAD (`git checkout <tag>` right after cloning is a defensible
reading of "install the release"), so a stranger stalls exactly there with
no way forward short of reading the script's source.

THE FIX. Two parts, checked separately and cross-checked against each other
so they cannot silently diverge:
  1. update_framework.sh's detached-HEAD refuse() message names `main`
     concretely (`git checkout main`) as the default remedy, while keeping
     the tag alternative for an operator who deliberately wants a pinned
     release rather than the moving branch.
  2. AGENTS.md's upgrade runbook (## Upgrade (gateway host)) gains a
     paragraph documenting the detached-HEAD case and the same recovery.

README.md is explicitly OUT OF SCOPE for this fix (build brief: propose,
never edit) -- this file does not touch or assert on it.
"""
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


UPDATE_FRAMEWORK = ("shared-memory", "scripts", "update_framework.sh")


def _detached_head_refusal_text() -> str:
    """The exact string literal passed to refuse() for the detached-HEAD
    case -- extracted rather than hardcoded here, so this test tracks the
    real message instead of a copy of it that could drift."""
    script = _read(*UPDATE_FRAMEWORK)
    m = re.search(
        r'refuse "this checkout is on a DETACHED HEAD.*?"\s*\|\|\s*pull_blocked=1',
        script, re.S,
    )
    assert m, (
        "could not find the detached-HEAD refuse() call in update_framework.sh "
        "-- the message or its structure changed; update this test's regex"
    )
    return m.group(0)


def test_the_refusal_names_main_as_the_concrete_remedy():
    text = _detached_head_refusal_text()
    assert "git checkout main" in text, (
        f"the detached-HEAD refusal no longer names 'git checkout main' as "
        f"the concrete remedy:\n{text}"
    )
    # The tag alternative must survive too -- a deliberate pinned-tag
    # checkout is legitimate, not itself a mistake to be refused away.
    assert "tag" in text.lower(), (
        f"the pinned-tag alternative was dropped from the refusal:\n{text}"
    )


def test_the_old_unnamed_remedy_wording_does_not_survive_verbatim():
    """Pins the fix, not merely an addition alongside stale text -- the OLD
    message ("Check out the release branch or tag you intend to run") named
    no concrete ref at all, which is the defect this fix closes."""
    text = _detached_head_refusal_text()
    assert "Check out the release branch or tag you intend to run" not in text, (
        f"the old, unnamed-remedy wording still appears verbatim:\n{text}"
    )


def test_agents_md_documents_the_detached_head_case():
    agents = _read("AGENTS.md")
    # Scoped to the Upgrade runbook section specifically, not just anywhere
    # in the file, per the build brief ("AGENTS.md's upgrade runbook gains a
    # short row/paragraph").
    m = re.search(r"### Upgrade \(gateway host\)(.*?)\n## ", agents, re.S)
    assert m, "could not find the '### Upgrade (gateway host)' section in AGENTS.md"
    section = m.group(1)

    assert "DETACHED HEAD" in section, (
        "the Upgrade runbook section does not mention the detached-HEAD case"
    )
    assert "git checkout main" in section, (
        "the Upgrade runbook section does not name the same concrete remedy "
        "('git checkout main') the script itself uses"
    )


def test_agents_md_and_the_script_name_the_same_remedy():
    """Cross-check: both halves of the fix must name the SAME ref, so a
    future rename of the release branch cannot update one and silently leave
    the other stale."""
    script_text = _detached_head_refusal_text()
    agents = _read("AGENTS.md")
    m = re.search(r"### Upgrade \(gateway host\)(.*?)\n## ", agents, re.S)
    assert m
    section = m.group(1)

    # Both sides also mention `git checkout <tag>` when describing how a
    # detached HEAD is reached in the first place -- match the REMEDY
    # specifically (the checkout recommended to FIX the state), not the
    # first "git checkout" substring encountered.
    script_remedy = re.search(r"run 'git checkout (\S+?)' to", script_text)
    doc_remedy = re.search(r"[Rr]ecover with\s+`git checkout (\S+?)`", section)
    assert script_remedy and doc_remedy, (
        "remedy command not found on one side "
        f"(script matched: {bool(script_remedy)}, doc matched: {bool(doc_remedy)})"
    )
    assert script_remedy.group(1) == doc_remedy.group(1), (
        f"update_framework.sh recommends 'git checkout {script_remedy.group(1)}' "
        f"but AGENTS.md recommends 'git checkout {doc_remedy.group(1)}' -- they "
        f"have diverged"
    )

