"""GPU encoder entrypoint: flash attention on the first server, CPU without it
if that server exits on its own.

The failure this names: a GPU crash that either stays down, or comes back on
CPU still carrying --flash-attn (that flag raises CPU resident memory).
"""
import os
import stat
import subprocess


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(ROOT, "shared-memory", "ops", "encoder_gpu_or_cpu.sh")


def test_gpu_exit_restarts_on_cpu_without_flash_attn(tmp_path):
    calls = tmp_path / "calls"
    once = tmp_path / "once"
    stub = tmp_path / "llama-server"
    stub.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        f"if [ ! -f {once} ]; then touch {once}; exit 7; fi\n"
        "exit 0\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    proc = subprocess.run(
        ["/bin/sh", SCRIPT, "-m", "/models/m.gguf", "--embedding",
         "--port", "8070", "-ngl", "99", "--flash-attn", "on"],
        env={**os.environ, "LLAMA_SERVER": str(stub)},
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    lines = calls.read_text().splitlines()
    assert lines[0] == "-m /models/m.gguf --embedding --port 8070 -ngl 99 --flash-attn on"
    assert lines[1] == "-m /models/m.gguf --embedding --port 8070 -ngl 0"
    assert "CPU failover" in proc.stderr
