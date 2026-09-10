"""Unit tests for migrate_retro_edges.py — the one-time legacy self-loop →
Retrospective-record conversion. Pure planning logic only (no live stores)."""

import importlib.util
import os
import sys


def load_migrator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "migrate_retro_edges.py")
    spec = importlib.util.spec_from_file_location("migrate_retro_edges", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate_retro_edges"] = mod
    spec.loader.exec_module(mod)
    return mod


mig = load_migrator()


def test_rating_map_targets_only_the_enum():
    from ontology import RETRO_RATINGS
    assert set(mig.RATING_MAP.values()) <= set(RETRO_RATINGS)
    assert mig.FALLBACK_RATING in RETRO_RATINGS


def test_build_plan_maps_and_flags():
    loops = [
        {"decision_id": 1, "rating": "high", "date": "2026-06-01",
         "notes": "held", "edge_id": "e1"},
        {"decision_id": 2, "rating": "totally-new-wording", "date": "",
         "notes": "odd", "edge_id": "e2"},
    ]
    plan = mig.build_plan(loops, {})
    assert plan[0]["mapped_rating"] == "validated" and not plan[0]["unmapped"]
    assert plan[0]["created_at"] == "2026-06-01"        # backdated from the edge
    assert plan[1]["mapped_rating"] == mig.FALLBACK_RATING and plan[1]["unmapped"]
    assert plan[1]["created_at"] is None                 # no date → now(), flagged
    assert plan[0]["source"] == "unknown"                # no surviving outbox row


def test_build_plan_recovers_provenance_from_legacy_rows():
    loops = [{"decision_id": 42, "rating": "good", "date": "2026-05-01",
              "notes": "held up well", "edge_id": "e1"}]
    legacy = {(42, "held up well"): {"source": "claude",
                                     "principal": "operator",
                                     "connected_from": {"uid": 1000}}}
    plan = mig.build_plan(loops, legacy)
    assert plan[0]["source"] == "claude"
    assert plan[0]["principal"] == "operator"
    assert plan[0]["connected_from"] == {"uid": 1000}


# --- W7/F10: the embedding endpoint derives from GATEWAY_URL ----------------
#
# Until W7 round 2/3 this script had its own private, undocumented EMBED_URL
# knob and never read the framework's own GATEWAY_URL at all — so an operator
# who pointed GATEWAY_URL at a non-default gateway got no effect here, and the
# tool silently embedded against localhost. The derivation that fixed it had
# NO test: neither GATEWAY_URL nor EMBED_URL appeared anywhere in this file,
# so reverting it would have been completely silent. These three pin it.
#
# They run the module in a SUBPROCESS because the endpoint is resolved at
# import time, which is exactly where the defect lived — monkeypatching after
# import would test a value nothing reads. SECURE_ENV_FILE points at an empty
# file so a developer's own shared-memory/.env can never decide the outcome.

import subprocess
import textwrap

_ENDPOINT_PROBE = textwrap.dedent(
    """
    import importlib.util, os, sys
    scripts = sys.argv[1]
    sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(
        "migrate_retro_edges", os.path.join(scripts, "migrate_retro_edges.py")
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["migrate_retro_edges"] = mod
    spec.loader.exec_module(mod)
    print("EMBED_URL=" + mod.EMBED_URL)
    print("GATEWAY_URL=" + mod.GATEWAY_URL)
    """
)


def _resolve_endpoint(tmp_path, **env_overrides):
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("")
    env = dict(os.environ)
    for name in ("GATEWAY_URL", "EMBED_URL"):
        env.pop(name, None)
    env["SECURE_ENV_FILE"] = str(empty_env)
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", _ENDPOINT_PROBE, scripts_dir],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr[-2000:]!r}"
    values = dict(
        line.split("=", 1)
        for line in proc.stdout.strip().split("\n")
        if "=" in line
    )
    return values, proc.stderr


def test_embedding_endpoint_derives_from_gateway_url(tmp_path):
    """GATEWAY_URL is the documented, framework-wide setting; this tool must
    follow it rather than a private default of its own."""
    values, stderr = _resolve_endpoint(tmp_path, GATEWAY_URL="http://gw.example:9999")
    assert values["EMBED_URL"] == "http://gw.example:9999/v1/embeddings", values
    assert "deprecated" not in stderr.lower()


def test_the_default_endpoint_is_the_default_gateway(tmp_path):
    """With nothing set, the derivation still has to produce the gateway's own
    embeddings route — asserted by VALUE, so the two halves of the expression
    cannot drift together to something wrong."""
    values, _ = _resolve_endpoint(tmp_path)
    assert values["GATEWAY_URL"] == "http://localhost:8888", values
    assert values["EMBED_URL"] == "http://localhost:8888/v1/embeddings", values


def test_embed_url_still_overrides_and_warns_on_stderr(tmp_path):
    """EMBED_URL is a one-release deprecated override kept for an install that
    already set it by hand. It must still WIN — silently dropping it would
    redirect that install's embeddings without telling anyone — and it must
    say so on stderr, so the operator learns to move to GATEWAY_URL."""
    values, stderr = _resolve_endpoint(
        tmp_path,
        GATEWAY_URL="http://gw.example:9999",
        EMBED_URL="http://legacy.example:7000/v1/embeddings",
    )
    assert values["EMBED_URL"] == "http://legacy.example:7000/v1/embeddings", values
    assert "EMBED_URL is deprecated" in stderr, stderr
    assert "GATEWAY_URL" in stderr, stderr
