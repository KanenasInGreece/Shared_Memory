"""D23: the gateway must not double /v1 when joining a backend base to the
incoming request path, and a 404 must never read as 'backend alive'.

Both defects shipped together and were invisible together: every OpenAI-
compatible cloud provider publishes a base ending in /v1 (our own
.env.example ships exactly that), the daemons address the gateway at
/v1/chat/completions, and the naive concat produced /v1/v1/chat/completions.
Providers answer that with 404 -- which the liveness probe then counted as
alive, so /health reported the backend ok while every real call failed and
nothing was ever billed.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import pytest


def _fn():
    from hive_mind_proxy import _upstream_url
    return _upstream_url


@pytest.mark.parametrize("base,rel,expected", [
    # The defect, exactly: a cloud base that already carries /v1.
    ("https://api.deepseek.com/v1", "/v1/chat/completions",
     "https://api.deepseek.com/v1/chat/completions"),
    # Covers every proxied route, not just chat.
    ("https://api.deepseek.com/v1", "/v1/embeddings",
     "https://api.deepseek.com/v1/embeddings"),
    # A local llama-server base has no /v1 -- the historically working case,
    # which must be left exactly as it was.
    ("http://localhost:5000", "/v1/chat/completions",
     "http://localhost:5000/v1/chat/completions"),
    # Trailing slash on the base must not produce a doubled separator.
    ("https://api.deepseek.com/v1/", "/v1/chat/completions",
     "https://api.deepseek.com/v1/chat/completions"),
    # A path that merely CONTAINS v1 elsewhere must not be rewritten.
    ("http://localhost:5000", "/health", "http://localhost:5000/health"),
    # Only a leading /v1/ segment is stripped -- never a bare /v1 suffix match
    # inside a longer first segment.
    ("https://example.test/v1", "/v1beta/models",
     "https://example.test/v1/v1beta/models"),
])
def test_upstream_url_never_doubles_v1(base, rel, expected):
    assert _fn()(base, rel) == expected


def test_the_measured_defect_is_gone():
    """The literal URL pair measured live on a fresh install: the doubled form
    returned 404, the correct form 200, with the same credential."""
    built = _fn()("https://api.deepseek.com/v1", "/v1/chat/completions")
    assert "/v1/v1/" not in built
    assert built == "https://api.deepseek.com/v1/chat/completions"


def test_join_is_pure_and_accepts_a_url_object():
    """request.rel_url is a yarl URL, not a str -- the join must not care."""
    class _RelLike:
        def __str__(self):
            return "/v1/chat/completions"
    assert _fn()("https://api.deepseek.com/v1", _RelLike()) == \
        "https://api.deepseek.com/v1/chat/completions"
