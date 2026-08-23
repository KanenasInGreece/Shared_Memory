"""server-setup.md — every repo-relative path named in a fenced ```bash```
command block must actually exist in the repository.

WHY THIS EXISTS (Test_Verification_Review.md, "NEW REQUIREMENT"). Three
commands in shared-memory/Documentation/server-setup.md were fixed on this
branch because they named paths that did not exist or omitted required
arguments -- caught only by a human reading the runbook, not by any test.
This pins the defect CLASS from the logic alone: walk every fenced bash
block in the doc, extract tokens that look like repo-relative paths, and
assert each one resolves against the actual repository tree. A future edit
that reintroduces a stale/renamed path in an example command fails this
test without anyone having to run the command by hand.

SCOPE, DELIBERATELY CONSERVATIVE. It is far better to check fewer paths
reliably than to produce a flaky or falsely-alarming test, so a token is
only ever treated as a path candidate when it is ALL of:
  * inside a ```bash fenced block (prose/tables outside code blocks are
    never scanned -- markdown links like [text](../foo) are out of scope);
  * contains a "/";
  * not a placeholder (contains "<" or ">", e.g. <repo-url>, <name>);
  * not a URL (contains "://") or a shell variable (starts with "$");
  * not an absolute path (starts with "/") or a home-relative one
    (starts with "~") -- neither is "repo-relative";
  * not exactly ".env" in its final path segment -- shared-memory/.env is
    the documented, gitignored, user-created target of a `cp` in the doc's
    own first-install block, and is NOT expected to exist in the repo;
  * rooted at a top-level entry that is actually TRACKED in this repo
    (via `git ls-tree --name-only HEAD`) -- this is what keeps the test
    from flagging ordinary prose fragments that merely happen to contain a
    "/" (e.g. "status`/`version`/`api_version`" in a comment describing a
    JSON field list) as if they were paths: prose fragments essentially
    never start with a real top-level directory/file name from this repo,
    so requiring that match is a cheap, reliable filter without needing
    any per-line heuristics about what "looks like prose".

If this scope ever proves too narrow or too broad for a doc change, widen
or tighten the filters here rather than special-casing individual paths --
the point is a structural check, not a pinned list.
"""
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).parent.parent
_DOC = _REPO / "shared-memory" / "Documentation" / "server-setup.md"

_TOKEN_RE = re.compile(r"[^\s\"'`]+")


def _tracked_top_level_names() -> set:
    """Top-level entries actually tracked by git, so this filter reflects
    the shipped repo tree rather than whatever local/untracked litter
    (build caches, scratch files, gitignored dirs) happens to sit at repo
    root on the machine running the suite."""
    proc = subprocess.run(
        ["git", "ls-tree", "--name-only", "HEAD"],
        cwd=_REPO, capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        # Fall back to a plain directory listing rather than silently
        # skipping every path -- still conservative, just less precise
        # about "tracked" specifically.
        return {p.name for p in _REPO.iterdir()}
    return set(proc.stdout.split())


def _extract_path_candidates(doc_text: str) -> list:
    """[(path_token, source_line), ...] for every token in a ```bash fenced
    block that plausibly names a repo-relative path, per the module-level
    scope rules above."""
    top_level = _tracked_top_level_names()
    candidates = []
    for block in re.findall(r"```bash\n(.*?)```", doc_text, re.DOTALL):
        for line in block.splitlines():
            for raw in _TOKEN_RE.findall(line):
                tok = raw.strip("()[]{},;").rstrip(".,:;")
                if "/" not in tok:
                    continue
                if "://" in tok:
                    continue
                if tok.startswith("~") or tok.startswith("/"):
                    continue
                if "<" in tok or ">" in tok:
                    continue
                if tok.startswith("$"):
                    continue
                if tok.split("/")[-1] == ".env":
                    continue
                if tok.split("/")[0] not in top_level:
                    continue
                candidates.append((tok, line.strip()))
    return candidates


def test_doc_exists():
    assert _DOC.is_file(), f"expected doc not found: {_DOC}"


def test_every_repo_relative_path_in_a_bash_block_exists():
    doc_text = _DOC.read_text()
    candidates = _extract_path_candidates(doc_text)

    # This is the coverage guarantee, not just a convenience check: if
    # extraction ever regresses to matching nothing (e.g. the doc drops all
    # ```bash fences, or the filters tighten too far), this test must FAIL
    # loudly rather than pass vacuously over zero paths.
    assert candidates, (
        "no repo-relative path candidates were extracted from "
        f"{_DOC} -- either the doc has no ```bash blocks left, or the "
        "extraction filters are too strict. A test that checks nothing "
        "is worse than no test; fix the extraction rather than let this "
        "pass silently."
    )

    missing = [(tok, line) for tok, line in candidates if not (_REPO / tok).exists()]
    assert not missing, (
        "server-setup.md references a repo-relative path in a ```bash "
        "block that does not exist:\n" +
        "\n".join(f"  {tok!r}  (from: {line})" for tok, line in missing)
    )
