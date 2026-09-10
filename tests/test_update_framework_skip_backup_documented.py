"""D2 — `--skip-backup` is documented NOWHERE that a reader would find it
before needing it.

The flag is parsed at `update_framework.sh:157` and today appears only in
failure prose (visible only AFTER a backup has already failed) — not in the
`--help` header block (awk-extracted from the top-of-file comment, lines
2-40ish) and not in `AGENTS.md`'s update-path table. This test closes D2
against recurrence: it asserts the flag AND its safety condition ("never on a
host holding the only copy of the data") appear in both places.
"""
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
UPDATE_SCRIPT = os.path.join(REPO_ROOT, "shared-memory", "scripts", "update_framework.sh")
AGENTS_MD = os.path.join(REPO_ROOT, "AGENTS.md")


def _help_text():
    """The exact --help output: awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}'
    -- the same extraction the script's own -h/--help branch runs."""
    with open(UPDATE_SCRIPT, encoding="utf-8") as f:
        lines = f.read().split("\n")
    out = []
    for i, line in enumerate(lines):
        if i == 0:
            continue
        if line.startswith("#"):
            out.append(re.sub(r"^#\s?", "", line))
            continue
        break
    return "\n".join(out)


def test_skip_backup_appears_in_the_help_header_block():
    help_text = _help_text()
    assert "--skip-backup" in help_text, (
        "--skip-backup is absent from update_framework.sh's header comment "
        "block, i.e. from --help output"
    )
    assert "only copy" in help_text.lower(), (
        "--skip-backup's safety condition (never on a host holding the only "
        "copy of the data) is not stated in the --help header block"
    )


def test_skip_backup_actually_prints_via_the_help_flag():
    """The header-block extraction above must match what the script itself
    prints for -h/--help -- not just what a human reading the file sees."""
    result = subprocess.run(
        ["bash", UPDATE_SCRIPT, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "--skip-backup" in result.stdout, (
        f"'bash update_framework.sh --help' does not mention --skip-backup. "
        f"stdout was:\n{result.stdout}"
    )


def _update_path_table_rows():
    """The rows of AGENTS.md's numbered update-step table.

    W7/F10-rest: the original assertion accepted `--skip-backup` ANYWHERE in
    AGENTS.md, so a mention buried in unrelated prose — or left behind in a
    section a reader following the update path never opens — would have kept
    it green. D2 is specifically about the table a reader consults BEFORE
    running the upgrade, so that is where it is now required. The table is
    found by SHAPE (a numbered pipe-row naming ops/backup.sh), not by line
    number, so ordinary edits above it cannot silently move the target.
    """
    with open(AGENTS_MD, encoding="utf-8") as f:
        lines = f.read().split("\n")
    anchor = None
    for i, line in enumerate(lines):
        if line.startswith("|") and "ops/backup.sh" in line and "`" in line:
            anchor = i
            break
    assert anchor is not None, (
        "AGENTS.md no longer has an update-step table row for ops/backup.sh — "
        "the update-path table this test is about has moved or been rewritten"
    )
    start = anchor
    while start > 0 and lines[start - 1].startswith("|"):
        start -= 1
    end = anchor
    while end + 1 < len(lines) and lines[end + 1].startswith("|"):
        end += 1
    return lines[start : end + 1]


def test_skip_backup_appears_in_agents_md_update_table():
    rows = _update_path_table_rows()
    table_text = "\n".join(rows)
    assert "--skip-backup" in table_text, (
        "AGENTS.md's update-step table does not mention --skip-backup (D2). A "
        "mention elsewhere in the file does not count: this is the table a "
        "reader consults before running the upgrade."
    )
    # The safety condition must accompany the flag, not just the bare name.
    flag_rows = [row for row in rows if "--skip-backup" in row]
    assert any("only copy" in row for row in flag_rows), (
        "AGENTS.md's update table names --skip-backup but not its safety "
        "condition (never on a host holding the only copy of the data) in the "
        f"same row: {flag_rows!r}"
    )
