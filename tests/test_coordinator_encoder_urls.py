"""The coordinator's own embed/rerank calls honour EMBEDDER_URL / RERANKER_URL.

Before this, coordinator.py held two literals (localhost:8070 / :8071) while the
gateway's routing map read the env — so pointing EMBEDDER_URL at a remote host
moved only the raw /v1/embeddings passthrough, and every real save/search
embedding kept going to the local container. One env setting must move both.
"""
import importlib
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def _reloaded(monkeypatch, **env):
    for k in ("EMBEDDER_URL", "RERANKER_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Other tests evict `coordinator` from sys.modules; import fresh, then reload
    # so the module-level constants are re-evaluated under THIS env.
    mod = importlib.import_module("coordinator")
    return importlib.reload(mod)


def test_defaults_are_the_local_encoders(monkeypatch):
    mod = _reloaded(monkeypatch)
    assert mod.EMBED_URL == "http://localhost:8070/v1/embeddings"
    assert mod.RERANK_URL == "http://localhost:8071/v1/reranking"


def test_env_base_moves_the_coordinator_call_not_only_the_passthrough(monkeypatch):
    mod = _reloaded(monkeypatch, EMBEDDER_URL="http://embed.example:1234/",
                    RERANKER_URL="http://rerank.example:9/")
    assert mod.EMBED_URL == "http://embed.example:1234/v1/embeddings"
    assert mod.RERANK_URL == "http://rerank.example:9/v1/reranking"


def test_empty_env_value_falls_back_to_default(monkeypatch):
    mod = _reloaded(monkeypatch, EMBEDDER_URL="")
    assert mod.EMBED_URL == "http://localhost:8070/v1/embeddings"


def test_helper_is_pure():
    assert importlib.import_module("coordinator")._encoder_url("NO_SUCH_ENV_VAR_X", "http://h:1/", "/p") == "http://h:1/p"


@pytest.fixture(autouse=True)
def _restore_coordinator_module(monkeypatch):
    """Reloading `coordinator` rebinds the module object process-wide, while
    hive_mind_proxy holds `from coordinator import …` names bound at ITS import.
    Restore a clean-env reload after every test so the end state of this file
    does not depend on test order (review finding F4, PR #307)."""
    yield
    for k in ("EMBEDDER_URL", "RERANKER_URL"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(importlib.import_module("coordinator"))


def test_both_consumers_agree_on_an_empty_value(monkeypatch):
    """`EMBEDDER_URL=` (present but empty) means "the default" for the gateway's
    routing map AND for the coordinator — a split brain here sends the
    passthrough one way and every real save another."""
    for k in ("EMBEDDER_URL", "RERANKER_URL"):
        monkeypatch.setenv(k, "")
    coord = importlib.reload(importlib.import_module("coordinator"))
    proxy = importlib.reload(importlib.import_module("hive_mind_proxy"))
    assert proxy.EMBEDDER_URL == "http://localhost:8070"
    assert proxy.RERANKER_URL == "http://localhost:8071"
    assert coord.EMBED_URL == proxy.EMBEDDER_URL + "/v1/embeddings"
    assert coord.RERANK_URL == proxy.RERANKER_URL + "/v1/reranking"
    monkeypatch.setenv("EMBEDDER_URL", "  http://h:1/  ")
    coord = importlib.reload(importlib.import_module("coordinator"))
    proxy = importlib.reload(importlib.import_module("hive_mind_proxy"))
    assert proxy.EMBEDDER_URL == "http://h:1"
    assert coord.EMBED_URL == "http://h:1/v1/embeddings"


@pytest.mark.asyncio
async def test_embed_failure_message_never_carries_url_credentials(monkeypatch):
    """The 503 body a client sees is built from str(exc); httpx renders the
    full request URL including user:pass@ — the encoder URL is now operator-
    supplied, so that is a credential-leak path unless scrubbed."""
    import httpx
    monkeypatch.setenv("EMBEDDER_URL", "http://svc:s3cr3t-token@embedder.internal:8070")
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    coord = importlib.reload(importlib.import_module("coordinator"))

    class _Client:
        async def post(self, url, **kw):
            # A real 401: the coordinator's own r.raise_for_status() builds the
            # message, and THAT is what renders the full URL with userinfo.
            return httpx.Response(401, request=httpx.Request("POST", url))

    obj = coord.MemoryCoordinator.__new__(coord.MemoryCoordinator)
    with pytest.raises(RuntimeError) as ei:
        await coord.MemoryCoordinator._embed(obj, "hello", _Client())
    msg = str(ei.value)
    assert "s3cr3t" not in msg and "svc:" not in msg
    assert "embedder.internal:8070" in msg          # host survives — the operator can still locate the fault
    assert "EMBEDDER_URL" in msg


async def _no_sleep(_):
    return None


# ── Startup validation (review finding F6, PR #307) ───────────────────────────
#
# EMBED_URL/RERANK_URL are now fully operator-supplied via EMBEDDER_URL/
# RERANKER_URL, so a scheme typo or a leftover "/v1" base used to surface only
# as a confusing httpx/aiohttp exception on the FIRST real embed/rerank call.
# _encoder_url validates both at the same point they are derived (module
# import/reload) instead.

def test_bad_scheme_fails_startup_naming_the_variable(monkeypatch):
    monkeypatch.delenv("RERANKER_URL", raising=False)
    monkeypatch.setenv("EMBEDDER_URL", "ftp://embedder.internal:8070")
    with pytest.raises(ValueError) as ei:
        importlib.reload(importlib.import_module("coordinator"))
    assert "EMBEDDER_URL" in str(ei.value)


def test_bare_host_port_with_no_scheme_fails_startup(monkeypatch):
    """A bare 'host:port' (no http:// / https://) is the measured-common typo
    -- urlsplit reads it as scheme='host', which is caught by the same check."""
    monkeypatch.delenv("EMBEDDER_URL", raising=False)
    monkeypatch.setenv("RERANKER_URL", "embedder.internal:8071")
    with pytest.raises(ValueError) as ei:
        importlib.reload(importlib.import_module("coordinator"))
    assert "RERANKER_URL" in str(ei.value)


def test_valid_scheme_case_insensitive_is_accepted(monkeypatch):
    """urlsplit's scheme check is case-insensitive (HTTP:// is a valid
    scheme) -- this must not raise. The base's own casing is passed through
    verbatim (this validates, it does not normalize)."""
    mod = _reloaded(monkeypatch, EMBEDDER_URL="HTTP://embedder.internal:8070")
    assert mod.EMBED_URL == "HTTP://embedder.internal:8070/v1/embeddings"


def test_v1_suffix_warns_and_still_resolves_with_doubled_path(caplog, monkeypatch):
    """A base that already ends in /v1 is a footgun (doubles the /v1 segment
    this function appends) but must not fail startup -- it warns and
    continues, since a deployer proxying at that exact path is possible."""
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = _reloaded(monkeypatch, EMBEDDER_URL="http://embedder.internal:8070/v1")
    assert mod.EMBED_URL == "http://embedder.internal:8070/v1/v1/embeddings"
    assert "already carries a path" in caplog.text
    assert "EMBEDDER_URL" in caplog.text


def test_no_path_warning_for_a_normal_base(caplog, monkeypatch):
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        _reloaded(monkeypatch, EMBEDDER_URL="http://embedder.internal:8070")
    assert "already carries a path" not in caplog.text


def test_l4_full_endpoint_pasted_as_base_also_warns(caplog, monkeypatch):
    """L4 (PR #308 review): the ORIGINAL check only matched a base ending in
    exactly '/v1' -- a plausible copy-paste of the FULL endpoint as the base
    (http://h:8070/v1/embeddings, e.g. copied straight out of an error
    message or this file's own docstrings) carries a path that does NOT end
    in '/v1' and used to warn nothing, silently doubling the whole path."""
    with caplog.at_level(logging.WARNING, logger="coordinator"):
        mod = _reloaded(monkeypatch, EMBEDDER_URL="http://embedder.internal:8070/v1/embeddings")
    assert mod.EMBED_URL == "http://embedder.internal:8070/v1/embeddings/v1/embeddings"
    assert "already carries a path" in caplog.text
    assert "EMBEDDER_URL" in caplog.text


# ── log_encoder_endpoints() — scrubbing + idempotence (unit-level) ────────────
#
# Testing the FUNCTION's own logging behaviour with caplog is fine here (it
# is explicitly, directly invoked -- nothing about module-import ordering is
# in play). What caplog CANNOT prove is the M1 defect this function exists to
# fix: whether the line is actually visible under the real gateway's logging
# configuration. That needs a real subprocess with real basicConfig, below.

def test_log_encoder_endpoints_scrubs_and_fires_once(caplog, monkeypatch):
    with caplog.at_level(logging.INFO, logger="coordinator"):
        mod = _reloaded(
            monkeypatch,
            EMBEDDER_URL="http://svc:s3cr3t-token@embedder.internal:8070",
        )
        caplog.clear()  # module import/reload logged nothing -- start fresh
        mod.log_encoder_endpoints()
        mod.log_encoder_endpoints()  # second call: must NOT log again
    startup_lines = [r.message for r in caplog.records if "encoder endpoints resolved" in r.message]
    assert len(startup_lines) == 1
    line = startup_lines[0]
    assert "s3cr3t" not in line and "svc:" not in line
    assert "embedder.internal:8070" in line
    assert mod.EMBED_URL == "http://svc:s3cr3t-token@embedder.internal:8070/v1/embeddings"


def test_module_import_alone_never_logs_the_startup_line(caplog, monkeypatch):
    """The regression this whole fix round is about: importing/reloading
    coordinator must NOT by itself emit the startup INFO line any more --
    only an explicit log_encoder_endpoints() call (from
    MemoryCoordinator.start(), in real use) does."""
    with caplog.at_level(logging.INFO, logger="coordinator"):
        _reloaded(monkeypatch, EMBEDDER_URL="http://embedder.internal:8070")
    assert "encoder endpoints resolved" not in caplog.text


# ── M1 regression proof — real subprocess, real basicConfig, real stderr ──────

def test_m1_encoder_endpoints_line_visible_in_a_real_gateway_process(monkeypatch, tmp_path):
    """Import hive_mind_proxy (which imports coordinator, THEN calls
    logging.basicConfig()) in a fresh subprocess, then call
    log_encoder_endpoints() exactly the way MemoryCoordinator.start() does,
    and grep the subprocess's REAL stderr -- no caplog, no synthetic
    handler. This is the test the review's M1 finding asked for: the
    original test used caplog.at_level(), which installs its OWN handler
    and would have passed even under the old defect (the INFO call sitting
    at module import, before basicConfig ever ran)."""
    import subprocess
    import sys as _sys

    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    env = dict(os.environ)
    env["PYTHONPATH"] = scripts_dir
    env["SECURE_ENV_FILE"] = ""
    env["CREDENTIAL_AUDIT_LOG_PATH"] = str(tmp_path / "credential-audit.jsonl")
    env["CAPACITY_LOG_PATH"] = str(tmp_path / "capacity-derivations.jsonl")
    env["EMBEDDER_URL"] = "http://embedder.internal:8070"
    env["RERANKER_URL"] = "http://reranker.internal:8071"
    script = (
        "import hive_mind_proxy\n"
        "import coordinator\n"
        "coordinator.log_encoder_endpoints()\n"
    )
    proc = subprocess.run(
        [_sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "encoder endpoints resolved" in proc.stderr
    assert "embedder.internal:8070" in proc.stderr
    assert "reranker.internal:8071" in proc.stderr
