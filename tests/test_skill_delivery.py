"""Invariant D1 — every agent install receives every file in MANIFEST.txt.

This has now failed twice on this project, the same way both times, and neither
failure was visible: `sync_skills.sh` printed success on every run.

  * First SKILL.md. An install whose `scripts/` was a symlink hit a `continue`
    and was declared "already current" as a whole, so three of four agents
    served a SKILL.md many versions behind. The fix hoisted SKILL.md above the
    short-circuit — a per-FILE fix to a per-LOOP defect.
  * Then Documentation/schema.md, added to the manifest later, fell into the
    identical hole and was missing ENTIRELY from two of four installs.

The symlink makes the SCRIPT auto-current. It says nothing about the capture
surface, the constitution snippet, the schema doc, or anything added to the
package next — which is exactly what a per-file fix cannot express.

⚠ THIS TEST RUNS THE REAL SCRIPT against a temporary tree, because the whole
defect class lives in the shell control flow. A test that read the source for a
filename would have PASSED throughout both failures above: the filename was
there, above a `continue` that skipped it.

No DB, no Neo4j, and nothing outside tmp_path is read or written.
"""
import os
import shutil
import subprocess

import pytest

_REPO = os.path.join(os.path.dirname(__file__), "..")
_SYNC = os.path.join(_REPO, "shared-memory", "scripts", "sync_skills.sh")
_SKILL_COPY = os.path.join(_REPO, "shared-memory-skill", "shared-memory")


def _manifest_entries():
    with open(os.path.join(_SKILL_COPY, "MANIFEST.txt"), encoding="utf-8") as fh:
        return [ln.strip() for ln in fh
                if ln.strip() and not ln.strip().startswith("#")]


# .env.example is MERGED into a live .env by update_skill.sh, never copied over
# it — a copy would overwrite the agent's own AGENT_TOKEN. update_skill.sh is
# refreshed by its own dedicated step. Neither is a counter-example to D1.
_NOT_COPIED = {".env.example", "scripts/update_skill.sh"}


def _run_sync(agent_dirs):
    env = dict(os.environ)
    env["SHARED_MEMORY_SYNC_AGENTS"] = ":".join(agent_dirs)
    return subprocess.run(["bash", _SYNC], capture_output=True, text=True,
                          env=env, cwd=_REPO, timeout=180)


def test_the_manifest_is_not_empty():
    """Guard against the whole suite passing vacuously if the manifest moves."""
    entries = _manifest_entries()
    assert entries, "MANIFEST.txt parsed to nothing"
    assert "Documentation/schema.md" in entries, (
        "the file whose absence this test exists for is no longer in the manifest")


def test_a_symlinked_install_still_receives_every_manifest_file(tmp_path):
    """THE REGRESSION. `scripts/` symlinked into the repo means the SCRIPT is
    auto-current; it must not short-circuit delivery of everything else."""
    install = tmp_path / "agent" / "skills" / "shared-memory"
    (install / "scripts").parent.mkdir(parents=True)
    # Exactly the shape of the .codex / .grok installs that were missing files.
    os.symlink(os.path.join(_SKILL_COPY, "scripts"), install / "scripts")

    result = _run_sync([str(install)])
    assert result.returncode == 0, result.stdout + result.stderr

    missing = [rel for rel in _manifest_entries()
               if rel not in _NOT_COPIED and not (install / rel).exists()]
    assert missing == [], (
        f"a symlinked install did not receive: {missing}. This is the defect "
        f"that shipped Documentation/schema.md to two of four agents.\n"
        f"{result.stdout}")


def test_a_plain_install_receives_every_manifest_file(tmp_path):
    """The mirror: D1 must not be satisfied only for the symlinked shape."""
    install = tmp_path / "agent" / "skills" / "shared-memory"
    (install / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(_SKILL_COPY, "scripts", "memory_bridge.py"),
                install / "scripts" / "memory_bridge.py")

    result = _run_sync([str(install)])
    assert result.returncode == 0, result.stdout + result.stderr

    missing = [rel for rel in _manifest_entries()
               if rel not in _NOT_COPIED and not (install / rel).exists()]
    assert missing == [], f"a plain install did not receive: {missing}\n{result.stdout}"


def test_a_stale_file_is_actually_refreshed_not_merely_present(tmp_path):
    """Presence is not currency. The first failure was a file that EXISTED in
    every install and was many versions behind."""
    install = tmp_path / "agent" / "skills" / "shared-memory"
    install.mkdir(parents=True)
    os.symlink(os.path.join(_SKILL_COPY, "scripts"), install / "scripts")
    (install / "Documentation").mkdir()
    (install / "Documentation" / "schema.md").write_text("stale\n")
    (install / "SKILL.md").write_text("stale\n")

    result = _run_sync([str(install)])
    assert result.returncode == 0, result.stdout + result.stderr

    for rel in ("Documentation/schema.md", "SKILL.md"):
        shipped = (install / rel).read_text()
        source = open(os.path.join(_SKILL_COPY, rel), encoding="utf-8").read()
        assert shipped == source, f"{rel} was left stale\n{result.stdout}"


def test_a_symlinked_file_is_left_as_a_link(tmp_path):
    """Copying onto a symlink would replace the link with a file and freeze it
    at today's content — turning an auto-current path into a stale one."""
    install = tmp_path / "agent" / "skills" / "shared-memory"
    install.mkdir(parents=True)
    os.symlink(os.path.join(_SKILL_COPY, "scripts"), install / "scripts")
    os.symlink(os.path.join(_SKILL_COPY, "SKILL.md"), install / "SKILL.md")

    result = _run_sync([str(install)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "SKILL.md").is_symlink(), (
        "a repo-linked SKILL.md was replaced by a copy")


def test_update_skill_does_not_write_through_a_symlink(tmp_path):
    """D1's mirror, on the OTHER delivery path.

    `update_skill.sh` applied every staged file with `mv`, and `mv` onto a
    symlink REPLACES the link with a regular file. So the self-update path
    silently undid the arrangement the sync path relies on: a repo-linked file,
    auto-current by construction, became a frozen copy of that day's content —
    and the freeze is invisible until it has gone stale.

    Runs the real script with a file:// source, so it exercises fetch, stage and
    apply exactly as a remote update would.
    """
    install = tmp_path / "agent" / "skills" / "shared-memory"
    (install / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(_SKILL_COPY, "scripts", "update_skill.sh"),
                install / "scripts" / "update_skill.sh")
    os.chmod(install / "scripts" / "update_skill.sh", 0o755)
    os.symlink(os.path.join(_SKILL_COPY, "scripts", "memory_bridge.py"),
               install / "scripts" / "memory_bridge.py")
    os.symlink(os.path.join(_SKILL_COPY, "SKILL.md"), install / "SKILL.md")

    env = dict(os.environ)
    env["SHARED_MEMORY_UPDATE_RAW_BASE"] = f"file://{_SKILL_COPY}"
    env["SHARED_MEMORY_UPDATE_FORCE"] = "1"
    result = subprocess.run(["bash", str(install / "scripts" / "update_skill.sh")],
                            capture_output=True, text=True, env=env, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr

    assert (install / "SKILL.md").is_symlink(), (
        f"update_skill.sh replaced a repo-linked SKILL.md with a copy\n{result.stdout}")
    assert (install / "scripts" / "memory_bridge.py").is_symlink(), (
        f"update_skill.sh replaced a repo-linked memory_bridge.py with a copy\n"
        f"{result.stdout}")
    # And it still delivers the files that are NOT links.
    assert (install / "Documentation" / "schema.md").exists()


def test_a_missing_install_directory_is_skipped_not_created(tmp_path):
    """An agent that is not installed must not be conjured into existence —
    sync reports it and moves on."""
    absent = tmp_path / "not-installed" / "skills" / "shared-memory"
    result = _run_sync([str(absent)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert not absent.exists()
    assert "SKIP" in result.stdout
