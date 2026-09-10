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


def test_skip_backup_appears_in_agents_md_update_table():
    with open(AGENTS_MD, encoding="utf-8") as f:
        text = f.read()
    assert "--skip-backup" in text, (
        "AGENTS.md does not mention --skip-backup anywhere (D2: the update "
        "table at ~:1058-1070 omits it)"
    )
    # The safety condition must accompany the flag, not just the bare name --
    # find the line(s) mentioning the flag and check the condition is nearby.
    lines = text.split("\n")
    flag_lines = [i for i, line in enumerate(lines) if "--skip-backup" in line]
    assert flag_lines, "unreachable"
    window = set()
    for i in flag_lines:
        window.update(range(max(0, i - 2), min(len(lines), i + 3)))
    nearby_text = "\n".join(lines[i] for i in sorted(window))
    assert "only copy" in nearby_text, (
        "AGENTS.md mentions --skip-backup but not its safety condition "
        "(never on a host holding the only copy of the data) near that mention"
    )
