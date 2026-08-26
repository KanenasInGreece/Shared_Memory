"""Invariant D1 — every agent install receives every file in MANIFEST.txt.

This has now failed twice on this project, the same way both times, and neither
failure was visible: `sync_skills.sh` printed success on every run.

  * First SKILL.md. An install whose `scripts/` was a symlink hit a `continue`
    and was declared "already current" as a whole, so three of four agents
    served a SKILL.md many versions behind. The fix hoisted SKILL.md above the
    short-circuit — a per-FILE fix to a per-LOOP defect.
  * Then Documentation/schema.md, added to the manifest later, fell into the
    identical hole and was missing ENTIRELY from two of four installs.

The symlink made the SCRIPT auto-current. It said nothing about the capture
surface, the constitution snippet, the schema doc, or anything added to the
package next — which is exactly what a per-file fix cannot express.

⛔ AND THE SYMLINKS THEMSELVES ARE NOW GONE (Xenofon, 2026-08-04): every
installed file is a REAL COPY. Repo-linking bought auto-currency by binding
every agent on the machine to one checkout's path, so moving, renaming or
archiving the project breaks all of them at once — silently, discovered only by
an agent failing mid-task. Staleness is the lesser risk because it is
DETECTABLE: every file is content-compared on each sync. It also makes the local
dev path produce the same result as the shipped one, since update_skill.sh
fetches from GitHub and writes real files for everyone else already.

That policy creates its own hazard, which these tests pin: `cp` and `cmp` both
FOLLOW a symlink, so a naive implementation would write into the source tree and
would report a link to identical content as "already current" forever.

⚠ THESE TESTS RUN THE REAL SCRIPTS against a temporary tree, because the whole
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
    # ⚠ Phase 1 (source → tracked copy) is SKIPPED here, and that matters. These
    # tests run the real script, and phase 1 writes into the repo's own tracked
    # copy — so without this, a test run would silently REPAIR a genuine drift
    # and make test_every_manifest_file_is_byte_identical_across_both_tracked_copies
    # pass vacuously, decided by nothing but test order. A harness that repairs
    # what it is meant to detect is worse than no harness.
    env["SHARED_MEMORY_SYNC_SKIP_TRACKED"] = "1"
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


def test_an_installed_file_is_a_real_copy_never_a_symlink(tmp_path):
    """⛔ POLICY (Xenofon, 2026-08-04): installs hold REAL COPIES.

    Repo-linking made a file auto-current at the cost of binding every agent on
    the machine to one checkout's PATH — move, rename or archive the project and
    all of them break at once, silently, discovered only by an agent failing
    mid-task. Staleness is the lesser risk because it is DETECTABLE: every file
    is content-compared on each sync. So a symlink found in an install is
    replaced, not preserved.
    """
    install = tmp_path / "agent" / "skills" / "shared-memory"
    install.mkdir(parents=True)
    os.symlink(os.path.join(_SKILL_COPY, "scripts"), install / "scripts")
    os.symlink(os.path.join(_SKILL_COPY, "SKILL.md"), install / "SKILL.md")

    result = _run_sync([str(install)])
    assert result.returncode == 0, result.stdout + result.stderr
    for rel in ("SKILL.md", "scripts", "scripts/memory_bridge.py"):
        assert not (install / rel).is_symlink(), (
            f"{rel} is still a symlink into the source tree\n{result.stdout}")
    assert (install / "SKILL.md").read_text() == \
        open(os.path.join(_SKILL_COPY, "SKILL.md"), encoding="utf-8").read()


def test_writing_into_a_symlinked_subdirectory_never_touches_the_source(tmp_path):
    """The hazard the policy creates and must therefore close: `rm -f` inside a
    symlinked `scripts/` would delete the SOURCE tree's file, not the install's.
    The link is dissolved into a real directory before anything is written."""
    install = tmp_path / "agent" / "skills" / "shared-memory"
    install.mkdir(parents=True)
    os.symlink(os.path.join(_SKILL_COPY, "scripts"), install / "scripts")

    before = open(os.path.join(_SKILL_COPY, "scripts", "memory_bridge.py"), "rb").read()
    result = _run_sync([str(install)])
    assert result.returncode == 0, result.stdout + result.stderr
    after = open(os.path.join(_SKILL_COPY, "scripts", "memory_bridge.py"), "rb").read()
    assert before == after, "the sync wrote through a symlink into the source tree"
    assert (install / "scripts" / "memory_bridge.py").is_file()


def test_an_install_directory_that_is_itself_a_symlink_is_refused(tmp_path):
    """The worst shape: copying into it would make the source its own
    destination. Refused with an instruction, never silently followed."""
    real = tmp_path / "elsewhere"
    real.mkdir()
    install = tmp_path / "agent" / "skills" / "shared-memory"
    install.parent.mkdir(parents=True)
    os.symlink(real, install)

    result = _run_sync([str(install)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REFUSING" in result.stdout, result.stdout
    assert not (real / "SKILL.md").exists(), "it wrote through the directory link"


def test_update_skill_replaces_a_symlink_with_a_real_copy(tmp_path):
    """D1's mirror, on the OTHER delivery path.

    The two delivery paths must agree that an installed file is a real copy. The
    subtle half is `cmp`: it FOLLOWS a symlink, so a link pointing at identical
    content reports "already current" and survives every update forever. The
    link has to be detected, not compared.

    Runs the real script with a file:// source, so it exercises fetch, stage and
    apply exactly as a remote update from GitHub would.
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
    # ⚠ NOT asserting returncode 0. This test is about symlink replacement; the
    # script's LAST step is a compatibility check that contacts a gateway, so
    # requiring exit 0 quietly required a running gateway on the machine running
    # the tests. It passed on a developer box and would fail on any clean
    # checkout — and it did fail here the moment the gateway went down, pointing
    # at the wrong thing entirely. Assert what this test is actually about.
    assert "Applied:" in result.stdout, result.stdout + result.stderr

    for rel in ("SKILL.md", "scripts/memory_bridge.py"):
        assert not (install / rel).is_symlink(), (
            f"update_skill.sh left {rel} as a symlink into a checkout\n{result.stdout}")
        assert (install / rel).is_file()
    # And it still delivers everything else in the manifest.
    assert (install / "Documentation" / "schema.md").exists()


def test_a_missing_install_directory_is_skipped_not_created(tmp_path):
    """An agent that is not installed must not be conjured into existence —
    sync reports it and moves on."""
    absent = tmp_path / "not-installed" / "skills" / "shared-memory"
    result = _run_sync([str(absent)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert not absent.exists()
    assert "SKIP" in result.stdout


# ── The install-path registry drives sync's targets (v0.9.27) ────────────────
#
# EXECUTABLE, not source-reading: these run the real script against a temporary
# tree. v0.9.27 replaced a roster guessed from agent names with AGENT_INSTALLS,
# and sync has to follow the registry or the two halves disagree about where an
# agent lives — which is exactly the fresh-install trap (a mint with nowhere to
# write, and a sync that would never create the target) this closes.

def _run_sync_with_registry(env_file, extra_args=(), *, home):
    # HOME must be sandboxed here, not just inherited: whenever the registry
    # is non-empty sync_skills.sh's target list is a UNION of the registry
    # PLUS any of the four historical default installs
    # ($HOME/.claude|.codex|.gemini|.grok/skills/shared-memory) that already
    # exist on disk (~lines 218-236) — so a caller that leaves HOME pointed
    # at the real operator account silently writes the tracked skill copy
    # into that operator's REAL installs the moment one of those four exists
    # (fact:1640, measured: it moved
    # ~/.claude/skills/shared-memory/scripts/memory_bridge.py's mtime).
    # `home` is therefore a required keyword, always a tmp_path-rooted dir,
    # mirroring test_registry_does_not_orphan_installs_that_predate_it.
    env = dict(os.environ)
    env.pop("SHARED_MEMORY_SYNC_AGENTS", None)   # registry must be what decides
    env["SHARED_MEMORY_ENV_FILE"] = str(env_file)
    env["SHARED_MEMORY_SYNC_SKIP_TRACKED"] = "1"
    env["HOME"] = str(home)
    return subprocess.run(["bash", _SYNC, *extra_args], capture_output=True,
                          text=True, env=env, cwd=_REPO, timeout=180)


def test_run_sync_with_registry_sandboxes_home(tmp_path, monkeypatch):
    """fact:1640 regression, measured on a live host: without an explicit
    `env["HOME"]` override, `_run_sync_with_registry` inherited HOME from the
    ambient environment, and sync_skills.sh's registry branch UNIONS the
    registry with any of the four historical default installs
    ($HOME/.claude|.codex|.gemini|.grok/skills/shared-memory) that already
    exist on disk — so three "isolated" tests were actually overwriting the
    tracked skill copy into whichever of those four the machine running the
    suite happened to have installed (moved
    ~/.claude/skills/shared-memory/scripts/memory_bridge.py's mtime).

    Never touches the real operator account: this monkeypatches HOME to point
    at its OWN throwaway sentinel install (standing in for "whatever the
    ambient environment happens to carry"), then calls the helper with a
    DIFFERENT `home` for a registry naming a THIRD, unrelated install. If HOME
    is properly sandboxed, the helper's explicit override wins and the
    monkeypatched ambient HOME's sentinel install is never even looked at —
    its SKILL.md must come back byte-for-byte unchanged.
    """
    ambient_home = tmp_path / "ambient-home"
    sentinel_install = ambient_home / ".claude" / "skills" / "shared-memory"
    sentinel_install.mkdir(parents=True)
    sentinel_text = "SENTINEL — must not be touched by an isolated sync test\n"
    (sentinel_install / "SKILL.md").write_text(sentinel_text)
    monkeypatch.setenv("HOME", str(ambient_home))

    registered = tmp_path / "registered" / "shared-memory"
    registered.mkdir(parents=True)   # UPDATE-ONLY default: must pre-exist
    env_file = tmp_path / ".env"
    env_file.write_text(f"AGENT_INSTALLS=known:{registered}/.env\n")

    result = _run_sync_with_registry(env_file, home=tmp_path / "isolated-home")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (registered / "SKILL.md").is_file(), "the registered install was not synced"
    assert (sentinel_install / "SKILL.md").read_text() == sentinel_text, (
        "the ambient/inherited HOME's default install was rewritten — "
        "_run_sync_with_registry is not sandboxing HOME"
    )


def test_sync_targets_come_from_the_agent_installs_registry(tmp_path):
    """The registry records each agent's skill .env; the directory synced is that
    file's parent. An install listed in the registry is updated even though it is
    not one of the four historical hardcoded paths."""
    install = tmp_path / "somewhere" / "custom-agent" / "shared-memory"
    install.mkdir(parents=True)
    env_file = tmp_path / ".env"
    env_file.write_text(f"AGENT_INSTALLS=custom:{install}/.env\n")

    result = _run_sync_with_registry(env_file, home=tmp_path / "home")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AGENT_INSTALLS registry" in result.stdout
    assert (install / "SKILL.md").is_file(), (
        "a registered install was not synced — sync is still using the old "
        "hardcoded roster instead of the registry"
    )


def test_a_path_containing_a_colon_still_parses_whole(tmp_path):
    """AGENT_INSTALLS splits name:path on the FIRST colon only. A path carrying a
    colon of its own must survive intact, or the directory silently becomes a
    truncated prefix and the sync writes to the wrong place."""
    install = tmp_path / "od:d" / "shared-memory"
    install.mkdir(parents=True)
    env_file = tmp_path / ".env"
    env_file.write_text(f"AGENT_INSTALLS=weird:{install}/.env\n")

    result = _run_sync_with_registry(env_file, home=tmp_path / "home")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "SKILL.md").is_file(), (
        "a path containing a colon was truncated by the registry parser"
    )


def test_a_registered_directory_is_created_only_with_install(tmp_path):
    """Default stays UPDATE-ONLY — the standing rule that an absent install is
    never conjured into existence. --install is the explicit opt-in, and it acts
    only on a path the REGISTRY names, never on a guess."""
    absent = tmp_path / "fresh" / "shared-memory"
    env_file = tmp_path / ".env"
    env_file.write_text(f"AGENT_INSTALLS=fresh:{absent}/.env\n")
    home = tmp_path / "home"

    without = _run_sync_with_registry(env_file, home=home)
    assert without.returncode == 0, without.stdout + without.stderr
    assert not absent.exists(), "sync created a directory without --install"

    with_flag = _run_sync_with_registry(env_file, extra_args=("--install",), home=home)
    assert with_flag.returncode == 0, with_flag.stdout + with_flag.stderr
    assert (absent / "SKILL.md").is_file(), (
        "--install did not create and populate a REGISTERED target"
    )


def test_install_refuses_a_target_the_registry_does_not_name(tmp_path):
    """--install honours the registry; it does not license creating anything the
    operator never registered."""
    registered = tmp_path / "known" / "shared-memory"
    unregistered = tmp_path / "unknown" / "shared-memory"
    env_file = tmp_path / ".env"
    env_file.write_text(f"AGENT_INSTALLS=known:{registered}/.env\n")

    env = dict(os.environ)
    env["SHARED_MEMORY_SYNC_AGENTS"] = str(unregistered)
    env["SHARED_MEMORY_ENV_FILE"] = str(env_file)
    env["SHARED_MEMORY_SYNC_SKIP_TRACKED"] = "1"
    result = subprocess.run(["bash", _SYNC, "--install"], capture_output=True,
                            text=True, env=env, cwd=_REPO, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not unregistered.exists(), (
        "--install created a directory the registry never named"
    )


def test_registry_does_not_orphan_installs_that_predate_it(tmp_path, monkeypatch):
    """⛔ THE REGISTRY IS A UNION WITH WHAT IS INSTALLED, NEVER A REPLACEMENT.

    Measured regression, caught on a live host: the registry only starts existing
    when someone adds an agent, and it then names ONLY that agent. Treating it as
    the whole target list dropped every install that predates it — silently, with
    no SKIP line — leaving those agents pinned to whatever version they last
    received. Stale copies fail silently, which is the whole reason this project
    ships copies and reports every refresh.
    """
    home = tmp_path / "home"
    existing = home / ".claude" / "skills" / "shared-memory"
    existing.mkdir(parents=True)
    registered = tmp_path / "newagent" / "shared-memory"
    registered.mkdir(parents=True)
    env_file = tmp_path / ".env"
    env_file.write_text(f"AGENT_INSTALLS=newagent:{registered}/.env\n")

    env = dict(os.environ)
    env.pop("SHARED_MEMORY_SYNC_AGENTS", None)
    env["SHARED_MEMORY_ENV_FILE"] = str(env_file)
    env["SHARED_MEMORY_SYNC_SKIP_TRACKED"] = "1"
    env["HOME"] = str(home)
    result = subprocess.run(["bash", _SYNC], capture_output=True, text=True,
                            env=env, cwd=_REPO, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr

    assert (registered / "SKILL.md").is_file(), "registered target was not synced"
    assert (existing / "SKILL.md").is_file(), (
        "an install that predates the registry was ORPHANED — adding one agent "
        "silently stopped every existing agent from being updated"
    )
    assert not (home / ".codex").exists(), (
        "a default path that is not installed must still never be created"
    )
