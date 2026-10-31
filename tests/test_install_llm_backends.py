"""install_llm_backends.sh asks for context instead of guessing it.

A defined backend with no n_ctx stops the gateway. The marker block queries
the URL and, when the URL does not say, asks. It never writes roles or private_ok.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

INSTALL_LLM_BACKENDS = (
    Path(__file__).parent.parent / "shared-memory" / "ops" / "install_llm_backends.sh"
)
BEGIN_MARKER = "# >>> BACKEND_ACCESS"
END_MARKER = "# <<< BACKEND_ACCESS"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed (this script's own hard prerequisite)"
)


def _extract_block() -> str:
    text = INSTALL_LLM_BACKENDS.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    match = re.search(pattern, text, re.S)
    assert match, "BACKEND_ACCESS markers missing"
    return match.group(1)


def _run(stdin_text: str, env_file: Path, *, token_env: str = "", curl: str | None = None,
         url: str = "http://backend.example:9/v1", extra_env: dict | None = None) -> subprocess.CompletedProcess:
    source = _extract_block()
    invocation = "build_backend_entry {} {} {} {} {}".format(
        shlex.quote(url), shlex.quote("1"), shlex.quote(""),
        shlex.quote(token_env), shlex.quote(str(env_file)),
    )
    script = "set -euo pipefail\n" + source + "\n" + invocation
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    if curl is not None:
        bindir = env_file.parent / "bin"
        bindir.mkdir(exist_ok=True)
        curl_path = bindir / "curl"
        curl_path.write_text(curl)
        curl_path.chmod(0o755)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        ["bash", "-c", script], input=stdin_text, capture_output=True, text=True,
        timeout=15, env=env,
    )


def test_markers_present_exactly_once():
    text = INSTALL_LLM_BACKENDS.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1
    block = _extract_block()
    assert "ask_backend_roles" not in block
    assert "private_ok" not in block
    assert "ROLE_VOCABULARY" not in text
    assert "--max-time" not in block
    assert "-K -" in block


def test_advertised_context_is_offered_and_not_guessed(tmp_path):
    env_file = tmp_path / "auth.env"
    env_file.write_text("AGENT_TOKENS=present\n")
    curl = textwrap.dedent("""\
        #!/bin/sh
        printf '%s' '{"default_generation_settings":{"n_ctx":4096}}'
    """)
    proc = _run("\n256\n", env_file, curl=curl)
    assert proc.returncode == 0, proc.stderr
    assert "advertised n_ctx=4096" in proc.stderr
    entry = json.loads(proc.stdout)
    assert entry["n_ctx"] == 4096
    assert entry["max_output_tokens"] == 256
    assert "roles" not in entry and "private_ok" not in entry


def test_declining_the_advertised_context_asks(tmp_path):
    env_file = tmp_path / "auth.env"
    env_file.write_text("AGENT_TOKENS=present\n")
    curl = "#!/bin/sh\nprintf '%s' '{\"default_generation_settings\":{\"n_ctx\":4096}}'\n"
    proc = _run("n\n2048\n128\n", env_file, curl=curl)
    assert proc.returncode == 0, proc.stderr
    entry = json.loads(proc.stdout)
    assert entry["n_ctx"] == 2048
    assert entry["max_output_tokens"] == 128


def test_unreadable_context_asks_and_never_defaults(tmp_path):
    env_file = tmp_path / "auth.env"
    env_file.write_text("AGENT_TOKENS=present\n")
    curl = "#!/bin/sh\nexit 1\n"
    proc = _run("1024\n64\n", env_file, curl=curl)
    assert proc.returncode == 0, proc.stderr
    assert "COULD NOT READ THE CONTEXT" in proc.stderr
    assert "Never guess" in proc.stderr
    entry = json.loads(proc.stdout)
    assert entry["n_ctx"] == 1024
    assert entry["max_output_tokens"] == 64


def test_eof_refuses_and_writes_nothing(tmp_path):
    env_file = tmp_path / "auth.env"
    env_file.write_text("AGENT_TOKENS=present\n")
    curl = "#!/bin/sh\nexit 1\n"
    proc = _run("", env_file, curl=curl)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "refusing to guess" in proc.stderr


def test_credentialed_auth_off_warns_without_claiming_any_caller_can_sign(tmp_path):
    env_file = tmp_path / "off.env"
    env_file.write_text("SOME_OTHER_KEY=1\n")
    curl = "#!/bin/sh\nexit 1\n"
    proc = _run("1024\n64\n", env_file, token_env="DEEPSEEK_API_KEY", curl=curl)
    assert proc.returncode == 0, proc.stderr
    assert "AGENT_TOKENS is not set" in proc.stderr
    assert "spawned daemon" in proc.stderr
    assert "any caller" not in proc.stderr


def test_provider_key_is_not_a_curl_argument(tmp_path):
    """The bearer is curl's stdin config. A process listing must not show it."""
    env_file = tmp_path / "auth.env"
    env_file.write_text("AGENT_TOKENS=present\n")
    argv_file = tmp_path / "argv"
    curl = textwrap.dedent(f"""\
        #!/bin/sh
        printf '%s\\n' "$@" > {shlex.quote(str(argv_file))}
        printf '%s' '{{"default_generation_settings":{{"n_ctx":4096}}}}'
    """)
    secret = "probe-token-value"
    proc = _run("\n64\n", env_file, token_env="PROVIDER_KEY", curl=curl,
                extra_env={"PROVIDER_KEY": secret})
    assert proc.returncode == 0, proc.stderr
    argv = argv_file.read_text()
    assert secret not in argv
    assert "-K" in argv.split()


def test_url_ending_v1_reads_max_context_length_from_models(tmp_path):
    """A url that already ends in /v1 must probe /v1/models, not /v1/v1/models. /props failing falls through to max_context_length."""
    env_file = tmp_path / "auth.env"
    env_file.write_text("AGENT_TOKENS=present\n")
    urls = tmp_path / "urls"
    curl = textwrap.dedent(f"""\
        #!/bin/sh
        last=
        for a in "$@"; do last=$a; done
        printf '%s\\n' "$last" >> {shlex.quote(str(urls))}
        case "$last" in
            */props) exit 1 ;;
        esac
        printf '%s' '{{"data":[{{"id":"m","max_context_length":8192}}]}}'
    """)
    proc = _run("\n64\n", env_file, curl=curl, url="http://backend.example:9/v1")
    assert proc.returncode == 0, proc.stderr
    seen = urls.read_text().splitlines()
    assert "http://backend.example:9/props" in seen
    assert "http://backend.example:9/v1/models" in seen
    assert not any("/v1/v1/" in line for line in seen)
    entry = json.loads(proc.stdout)
    assert entry["n_ctx"] == 8192
    assert entry["max_output_tokens"] == 64
