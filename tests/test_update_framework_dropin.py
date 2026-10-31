"""The update drop-in follows GATEWAY_UNIT and does not pretend a user file covers a system restart."""
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "shared-memory" / "scripts" / "update_framework.sh"
BEGIN = "# --- restart-prevent-dropin ---"
END = "# --- end restart-prevent-dropin ---"


def _function_text():
    text = SCRIPT.read_text()
    start = text.index(BEGIN) + len(BEGIN)
    stop = text.index(END)
    return text[start:stop]


def _run(tmp_path, restart_cmd, unit, systemctl_rc=0, show_text=""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "systemctl"
    stub.write_text(
        "#!/bin/sh\n"
        "echo \"$@\" >> \"$STUB_LOG\"\n"
        "cmd=\"$1\"\n"
        "[ \"$1\" = --user ] && cmd=\"$2\"\n"
        "if [ \"$cmd\" = show ]; then\n"
        "  printf '%s\\n' \"$STUB_SHOW\"\n"
        "fi\n"
        "exit \"$STUB_RC\"\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "systemctl.log"
    env = os.environ.copy()
    env.update({
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "GATEWAY_UNIT": unit,
        "GATEWAY_RESTART_CMD": restart_cmd,
        "STUB_LOG": str(log),
        "STUB_RC": str(systemctl_rc),
        "STUB_SHOW": show_text,
        "SECURE_ENV_FILE": "",
    })
    proc = subprocess.run(
        ["bash", "-c", _function_text() + "\ninstall_restart_prevent_dropin\n"],
        capture_output=True, text=True, env=env,
    )
    drop = tmp_path / "config" / "systemd" / "user" / f"{unit}.d" / "restart-prevent-78.conf"
    return proc, drop, log.read_text() if log.exists() else ""


def test_user_restart_writes_the_unit_dropin_and_reloads(tmp_path):
    unit = "sm-gateway-under-test.service"
    proc, drop, log = _run(tmp_path, f"systemctl --user restart {unit}", unit)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert drop.read_text() == "[Service]\nRestartPreventExitStatus=78\n"
    assert "--user" in log and "daemon-reload" in log
    assert not (tmp_path / "config" / "systemd" / "user" / "hive-mind-gateway.service.d").exists()


def test_system_restart_warns_and_continues_without_a_user_dropin(tmp_path):
    unit = "sm-gateway-under-test.service"
    proc, drop, log = _run(tmp_path, f"systemctl restart {unit}", unit)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "without --user" in proc.stdout
    assert "RestartPreventExitStatus=78" in proc.stdout
    assert unit in proc.stdout
    assert not drop.exists()
    assert "daemon-reload" not in log


def test_loaded_78_skips_the_write(tmp_path):
    unit = "sm-gateway-under-test.service"
    proc, drop, log = _run(
        tmp_path, f"systemctl --user restart {unit}", unit,
        show_text="LoadState=loaded\nRestartPreventExitStatus=78\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == ""
    assert not drop.exists()
    assert "show" in log and "daemon-reload" not in log


def test_system_unit_with_78_already_loaded_is_silent(tmp_path):
    unit = "sm-gateway-under-test.service"
    proc, drop, log = _run(
        tmp_path, f"systemctl restart {unit}", unit,
        show_text="LoadState=loaded\nRestartPreventExitStatus=78 1\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "without --user" not in proc.stdout
    assert not drop.exists()
    assert "--user" not in log


def test_readonly_config_dir_warns_and_continues(tmp_path):
    unit = "sm-gateway-under-test.service"
    config = tmp_path / "config"
    config.mkdir()
    config.chmod(0o555)
    try:
        proc, drop, _log = _run(tmp_path, f"systemctl --user restart {unit}", unit)
    finally:
        config.chmod(0o755)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RestartPreventExitStatus=78" in proc.stdout
    assert "could not be written" in proc.stdout
    assert not drop.exists()


def test_uninstall_removes_the_restart_dropin(tmp_path):
    """The drop-in is a file under XDG_CONFIG_HOME. Uninstall removes it and does not need a unit file."""
    import shutil
    root = tmp_path / "repo"
    (root / "shared-memory" / "scripts").mkdir(parents=True)
    (root / "shared-memory" / "ops").mkdir(parents=True)
    src = ROOT / "shared-memory" / "scripts" / "uninstall_framework.sh"
    shutil.copy(src, root / "shared-memory" / "scripts" / "uninstall_framework.sh")
    (root / "shared-memory" / ".env").write_text("NEO4J_HOST_DIR=\nPG_DATA_DIR=\n")
    home = tmp_path / "home"
    config = home / ".config"
    drop_dir = config / "systemd" / "user" / "hive-mind-gateway.service.d"
    drop_dir.mkdir(parents=True)
    drop = drop_dir / "restart-prevent-78.conf"
    drop.write_text("[Service]\nRestartPreventExitStatus=78\n")
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(config)

    def _invoke(args):
        return subprocess.run(
            ["bash", str(root / "shared-memory" / "scripts" / "uninstall_framework.sh"), *args],
            capture_output=True, text=True, timeout=60, env=env,
        )

    preview = _invoke(["--level", "service", "--dry-run"])
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert drop.is_file()
    assert "restart drop-in" in preview.stdout
    removed = _invoke(["--level", "service", "--yes"])
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not drop.exists()
    assert "restart drop-in removed" in removed.stdout


def test_daemon_reload_failure_warns_and_continues(tmp_path):
    unit = "sm-gateway-under-test.service"
    proc, drop, _log = _run(tmp_path, f"systemctl --user restart {unit}", unit, systemctl_rc=3)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "daemon-reload failed" in proc.stdout
    assert "RestartPreventExitStatus=78" in proc.stdout
    assert drop.is_file()
